"""One real, governed RoboTwin episode; no development run, reflection or historical inputs."""

import argparse
import asyncio

from owned_io import owned_call
import json
from pathlib import Path
import re
import shutil
import signal
import sys
from types import SimpleNamespace

import yaml

SOURCE = Path(__file__).resolve().parent
CORE = SOURCE.parents[1]
# Direct script execution must use this checkout's Core and packaging helpers,
# including when launched from outside the repository.
sys.path.insert(0, str(CORE))

from experiment import Experiment
from prescreen_runner import save_json
from reservation import WorkerReservation
from selected_runner import require_free_port


REQUIRED = {'source_home', 'source_skill', 'profile', 'xpolicy', 'policy_python',
            'policy_config', 'model_config', 'vlm_model', 'task', 'max_steps',
            'policy_seed', 'gateway_port', 'policy_port', 'policy_gpu', 'simulation_gpu', 'lease_dir'}


def load_settings(path):
    settings = json.loads(Path(path).read_text())
    if not isinstance(settings, dict) or set(settings) != REQUIRED:
        raise ValueError('smoke config must contain exactly: ' + ', '.join(sorted(REQUIRED)))
    for key in ['source_skill', 'profile', 'task']:
        if not isinstance(settings[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', settings[key]):
            raise ValueError('invalid identifier: ' + key)
    for key in ['source_home', 'xpolicy', 'policy_python', 'policy_config', 'model_config', 'lease_dir']:
        if not isinstance(settings[key], str) or not Path(settings[key]).is_absolute():
            raise ValueError('absolute path required: ' + key)
    for key in ['max_steps', 'policy_seed', 'gateway_port', 'policy_port', 'policy_gpu', 'simulation_gpu']:
        if type(settings[key]) is not int or settings[key] < 0:
            raise ValueError('nonnegative integer required: ' + key)
    if settings['max_steps'] < 1 or any(not 1024 <= settings[key] <= 65535 for key in ['gateway_port', 'policy_port']):
        raise ValueError('invalid step limit or port (1024..65535)')
    if settings['gateway_port'] == settings['policy_port']:
        raise ValueError('gateway and policy ports must differ')
    if not isinstance(settings['vlm_model'], str) or not settings['vlm_model'].strip():
        raise ValueError('explicit multimodal model ID required')
    return settings


def preflight(settings, root):
    """Static/local checks only: never import a policy, start a worker or contact a model."""
    errors = []
    root = Path(root).resolve()
    if sys.platform != 'linux':
        errors.append('The real runner requires Linux (/proc executor ownership checks).')
    if root.exists():
        errors.append('Use a new run root. Existing runs are never resumed automatically.')
    source_home = Path(settings['source_home'])
    skill = source_home / '.PhyAgentOS/skills' / settings['source_skill']
    profile = skill / 'profiles' / settings['profile']
    required = [skill / 'skill.yaml', Path(settings['policy_python']), Path(settings['policy_config']),
                Path(settings['model_config']), Path(settings['xpolicy']) / 'policy/Pi_05/model.py',
                Path(settings['xpolicy']).parent / 'description/task_instruction' / (settings['task'] + '.json')]
    required += [profile / name for name in ['gateway.yaml', 'dataflow.yaml', 'benchmark.yaml',
                 'image_vision_head.yaml', 'image_vision_left_wrist.yaml', 'image_vision_right_wrist.yaml']]
    required += [skill / 'nodes' / name / '.venv/bin/python' for name in ['vla_bridge', 'image_vision']]
    for path in required:
        if not path.is_file():
            errors.append('Missing prerequisite: ' + str(path))
    for name in ['nodes', 'bundles']:
        if not (source_home / '.PhyAgentOS/forge_runtime' / name).is_dir():
            errors.append('Missing Forge runtime cache: ' + name)
    try:
        manifest = yaml.safe_load((skill / 'skill.yaml').read_text())
        if settings['profile'] not in manifest['profiles']:
            errors.append('Requested physical profile is not installed.')
        policy = json.loads(Path(settings['policy_config']).read_text())
        keys = {'task_name', 'env_cfg_type', 'action_type', 'model_path', 'checkpoint_num',
                'train_config_name', 'repo_id', 'policy_name'}
        if not keys.issubset(policy) or policy.get('policy_name') != 'Pi_05' or policy.get('action_type') != 'joint':
            errors.append('Policy config must specify the complete Pi_05 joint checkpoint configuration.')
        if not Path(policy.get('model_path', '')).is_absolute() or not Path(policy.get('model_path', '')).exists():
            errors.append('Policy model_path must be an existing absolute checkpoint path.')
        config = json.loads(Path(settings['model_config']).read_text())
        provider = config['providers']['custom']
        if not provider.get('apiKey', provider.get('api_key')) or not provider.get('apiBase', provider.get('api_base')):
            errors.append('Model config requires custom apiKey/apiBase (never put them in the smoke manifest).')
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append('Prerequisite metadata invalid: ' + type(error).__name__)
    for key in ['gateway_port', 'policy_port']:
        port = settings[key]
        if (Path(settings['lease_dir']) / f'port-{port}').exists():
            errors.append(f'Unresolved durable reservation for port {port}; reconcile its original owner.')
        try:
            require_free_port(port)
        except OSError:
            errors.append(f'Port {port} is not available.')
    return {'ready': not errors, 'errors': errors, 'task': settings['task'],
            'episodes': 1, 'arm': 'none', 'policy_seed': settings['policy_seed'],
            'model': settings['vlm_model'], 'run_root': str(root),
            'scope': 'Static prerequisites only; CUDA/asset compatibility and model loading require a real run.'}


async def run_smoke(experiment):
    await experiment.setup()
    await owned_call(experiment.launch_policy)
    case = experiment.case('smoke_a1', 'none', experiment.args.policy_seed)
    children = await experiment.run_root([case], 'smoke')
    if len(children) != 1 or children[0]['outcome'] not in {'success', 'failure'}:
        raise RuntimeError('smoke_requires_one_valid_native_outcome')
    save_json(experiment.runtime.root / 'SMOKE_RESULT.json', {
        'evaluation_complete': True, 'native_outcome': children[0]['outcome'],
        'native_score': children[0]['native_score'], 'episodes': 1, 'arm': 'none',
        'reflection': False, 'automatic_retry': False,
        'core_report': 'smoke_CORE_REPORT.json', 'stop_proof': 'smoke_a1/STOP_PROOF.json'})


async def execute(settings, root):
    root = Path(root).resolve()
    lease = WorkerReservation(settings['lease_dir'], root, [settings['gateway_port'], settings['policy_port']])
    lease.acquire()
    experiment = None
    try:
        root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(CORE / 'example/robotwin-vla-bridge/src', root / 'bridge_src',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ['BASE_SKILL.md', 'policy_seed_server.py', 'policy_seed_server_gpu_safe.py']:
            shutil.copy2(SOURCE / name, root / name)
        save_json(root / 'SMOKE_CONFIG.json', settings)
        args = SimpleNamespace(**settings, root=root, mode='smoke', core=str(CORE),
                               agent_python=sys.executable, repetitions=None)
        experiment = Experiment(args)
        await run_smoke(experiment)
    except BaseException as error:
        if root.exists():
            save_json(root / 'SMOKE_ABORT.json', {'error_type': type(error).__name__, 'reason': str(error),
                'classification': 'infrastructure_or_validation_failure', 'automatic_retry': False})
        raise
    finally:
        if experiment is None:
            # Constructor/packaging never launches an executor or model process.
            lease.release()
        else:
            try:
                if experiment.supervisor:
                    await experiment.supervisor.shutdown()
            finally:
                try:
                    await experiment.decider.client.close()
                    await experiment.client.close()
                finally:
                    await owned_call(experiment.runtime.close)
                    cleanup = json.loads((experiment.runtime.root / 'CLEANUP.json').read_text())
                    lease.release_after_cleanup(cleanup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='Read-only static preflight (default)')
    action.add_argument('--run', action='store_true', help='Start one real GPU episode and paid model requests')
    args = parser.parse_args()
    settings = load_settings(args.config)
    report = preflight(settings, args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['ready']:
        raise SystemExit(2)
    if args.run:
        # Preserve async finally blocks on SIGTERM; no physical-action retries.
        def terminate(signum, frame):
            raise KeyboardInterrupt('termination_requested')
        signal.signal(signal.SIGTERM, terminate)
        asyncio.run(execute(settings, args.root))


if __name__ == '__main__':
    main()
