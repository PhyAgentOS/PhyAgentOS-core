import asyncio
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from controller import Runtime
from experiment import case_experiences, proposal_schema
from model_config import ProviderUsage
from reservation import WorkerReservation
from smoke import CORE, SOURCE, load_settings, preflight, run_smoke
from supervision import validate_assessment
from test_robotwin_decision_repair import context_and_decision, decider_fixture


def prerequisites(tmp_path):
    settings = json.loads((SOURCE / 'smoke.example.json').read_text())
    for name in ['source_home', 'xpolicy', 'policy_python', 'policy_config', 'model_config', 'lease_dir']:
        settings[name] = str(tmp_path / name)
    profile = Path(settings['source_home']) / '.PhyAgentOS/skills' / settings['source_skill'] / 'profiles' / settings['profile']
    profile.mkdir(parents=True)
    skill = profile.parent.parent
    (skill / 'skill.yaml').write_text(yaml.safe_dump({'profiles': {settings['profile']: {'dataflow': 'flow.yaml'}}}))
    for name in ['gateway', 'benchmark', 'dataflow', 'image_vision_head', 'image_vision_left_wrist', 'image_vision_right_wrist']:
        (profile / (name + '.yaml')).write_text('nodes: []\noutputs: {}\n')
    for name in ['nodes', 'bundles']:
        (Path(settings['source_home']) / '.PhyAgentOS/forge_runtime' / name).mkdir(parents=True)
    for name in ['vla_bridge', 'image_vision']:
        path = skill / 'nodes' / name / '.venv/bin/python'
        path.parent.mkdir(parents=True)
        path.write_text('placeholder executable')
    source_launcher = tmp_path / 'source-launcher.sh'
    source_launcher.write_text('original launcher')
    (skill / 'nodes/vla_bridge/vla_bridge.sh').symlink_to(source_launcher)
    source_bridge = tmp_path / 'source-bridge'
    source_bridge.mkdir()
    (source_bridge / 'unchanged.py').write_text('original source')
    (skill / 'nodes/vla_bridge/src').symlink_to(source_bridge, target_is_directory=True)
    model = tmp_path / 'weights'
    model.mkdir()
    policy = {key: 'test' for key in ['task_name', 'env_cfg_type', 'train_config_name', 'repo_id']}
    policy.update(model_path=str(model), checkpoint_num=29999, policy_name='Pi_05', action_type='joint')
    Path(settings['policy_config']).write_text(json.dumps(policy))
    Path(settings['policy_python']).write_text('placeholder executable')
    Path(settings['model_config']).write_text(json.dumps({'agents': {'defaults': {'model': 'test-vlm'}},
        'providers': {'custom': {'apiKey': 'test-only', 'apiBase': 'http://example.invalid/v1'}}}))
    model_source = Path(settings['xpolicy']) / 'policy/Pi_05/model.py'
    model_source.parent.mkdir(parents=True)
    model_source.write_text('')
    description = Path(settings['xpolicy']).parent / 'description/task_instruction' / (settings['task'] + '.json')
    description.parent.mkdir(parents=True)
    description.write_text(json.dumps({'full_description': 'Press the stapler'}))
    path = tmp_path / 'smoke.json'
    path.write_text(json.dumps(settings))
    return path, settings


def test_smoke_cli_can_be_launched_from_outside_checkout(tmp_path):
    import os
    import subprocess

    env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP='True')
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, str(SOURCE / 'smoke.py'), '--help'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--config' in result.stdout and '--run' in result.stdout


def test_preflight_never_starts_or_creates_batch(tmp_path, monkeypatch):
    config, settings = prerequisites(tmp_path)
    monkeypatch.setattr('smoke.sys.platform', 'linux')
    monkeypatch.setattr('smoke.require_free_port', lambda port: None)
    root = tmp_path / 'new-run'
    assert load_settings(config) == settings
    assert preflight(settings, root)['ready']
    assert not root.exists() and not Path(settings['lease_dir']).exists()
    Path(settings['policy_config']).unlink()
    assert not preflight(settings, root)['ready']


def test_smoke_runtime_needs_no_screening_or_development_artifacts(tmp_path):
    _, settings = prerequisites(tmp_path)
    root = tmp_path / 'run'
    root.mkdir()
    shutil.copytree(CORE / 'example/robotwin-vla-bridge/src', root / 'bridge_src')
    shutil.copy2(SOURCE / 'BASE_SKILL.md', root / 'BASE_SKILL.md')
    runtime = Runtime(SimpleNamespace(**settings, root=root, mode='smoke', core=str(CORE)))
    assert runtime.reference['initial_observation'] is None
    assert runtime.schedule == []
    assert runtime.vlm_model == settings['vlm_model']
    assert json.loads((runtime.root / 'MANIFEST.json').read_text())['development_seed'] is None
    assert not (root / 'screening_reference.json').exists()
    assert (tmp_path / 'source-launcher.sh').read_text() == 'original launcher'
    assert list((tmp_path / 'source-bridge').iterdir()) == [tmp_path / 'source-bridge/unchanged.py']


def test_smoke_starts_with_valid_profile_then_resolves_live_task_id(tmp_path, monkeypatch):
    _, settings = prerequisites(tmp_path)
    root = tmp_path / 'run'
    root.mkdir()
    shutil.copytree(CORE / 'example/robotwin-vla-bridge/src', root / 'bridge_src')
    shutil.copy2(SOURCE / 'BASE_SKILL.md', root / 'BASE_SKILL.md')
    runtime = Runtime(SimpleNamespace(**settings, root=root, mode='smoke', core=str(CORE)))
    profile = runtime.skill_root / 'profiles' / settings['profile'] / 'benchmark.yaml'
    profile.write_text(yaml.safe_dump({'task_ids': [3]}))
    monkeypatch.setattr(runtime, 'stop_skill', lambda: None)
    monkeypatch.setattr('fixed_runner.require_free_port', lambda port: None)
    def start(*args):
        assert yaml.safe_load(profile.read_text())['task_ids'] == [3]
        state = runtime.state / 'run/skills' / (runtime.skill + '.json')
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'status': 'running', 'gateway_url': runtime.api}))
    monkeypatch.setattr(runtime, 'command', start)
    monkeypatch.setattr('fixed_runner.request_json', lambda *args: {'data': {'tools': []}})
    responses = {
        'robotwin.benchmark.describe': {'tasks': [{'task_id': 17, 'name': settings['task'],
                                                  'language': 'Press  the stapler'}]},
        'robotwin.policy.describe': {'ready': True, 'action_dim': 14},
        'vla.get_status': {'candidate': {}},
    }
    monkeypatch.setattr('fixed_runner.query_tool', lambda api, tools, tool: responses[tool])
    trial = runtime.root / 'smoke_a1'
    trial.mkdir()
    task_id, carrier = runtime.prepare_trial(trial, 'none', 102)
    assert task_id == 17 and carrier == ''
    assert runtime.reference['scene']['instruction'] == 'Press  the stapler'


@pytest.mark.asyncio
async def test_client_close_error_still_cleans_owned_runtime(tmp_path, monkeypatch):
    from experiment import main

    root = tmp_path / 'batch'
    task_root = root / 'tasks/example'
    task_root.mkdir(parents=True)
    calls = []
    async def run():
        pass
    async def close_client():
        raise RuntimeError('client_close_error')
    def cleanup():
        calls.append('physical_cleanup')
        (task_root / 'CLEANUP.json').write_text(json.dumps({'own_policy_exited': True,
            'skill_stop_error': None, 'executor_shutdown_proven': True}))
    experiment = SimpleNamespace(run=run, supervisor=None,
        decider=SimpleNamespace(client=SimpleNamespace(close=close_client)),
        runtime=SimpleNamespace(close=cleanup, root=task_root))
    monkeypatch.setattr('experiment.Experiment', lambda args: experiment)
    args = SimpleNamespace(root=root, task='example', lease_dir=tmp_path / 'leases',
                           gateway_port=19791, policy_port=18991)
    with pytest.raises(RuntimeError, match='client_close_error'):
        await main(args)
    assert calls == ['physical_cleanup']
    assert not list((tmp_path / 'leases').iterdir())


@pytest.mark.asyncio
async def test_single_smoke_skips_reflection_and_fixed_arm(tmp_path):
    calls = []
    async def setup():
        calls.append('setup')
    async def root(cases, phase):
        assert cases == [{'trial': 'smoke_a1', 'arm': 'none', 'seed': 102}]
        assert phase == 'smoke'
        return [{'outcome': 'failure', 'native_score': 0}]
    experiment = SimpleNamespace(setup=setup, launch_policy=lambda: calls.append('policy'),
        case=lambda trial, arm, seed: {'trial': trial, 'arm': arm, 'seed': seed}, run_root=root,
        runtime=SimpleNamespace(root=tmp_path), args=SimpleNamespace(policy_seed=102))
    await run_smoke(experiment)
    assert calls == ['setup', 'policy']
    assert json.loads((tmp_path / 'SMOKE_RESULT.json').read_text())['native_outcome'] == 'failure'
    assert not (tmp_path / 'frozen').exists()


def test_crash_tombstone_blocks_even_when_no_port_is_listening(tmp_path):
    lease = WorkerReservation(tmp_path / 'leases', tmp_path / 'first', [19791, 18991])
    lease.acquire()
    with pytest.raises(RuntimeError, match='unresolved_worker'):
        WorkerReservation(tmp_path / 'leases', tmp_path / 'second', [19791, 18991]).acquire()
    with pytest.raises(RuntimeError, match='not_proven'):
        lease.release_after_cleanup({'own_policy_exited': True, 'skill_stop_error': None})
    lease.release_after_cleanup({'own_policy_exited': True, 'skill_stop_error': None, 'executor_shutdown_proven': True})
    assert not list((tmp_path / 'leases').iterdir())


def test_experience_cannot_cross_tasks_or_arms():
    cases = [{'case_key': 'one', 'environment': {'task_name': 'task-a', 'arm': 'fixed'}},
             {'case_key': 'two', 'environment': {'task_name': 'task-a', 'arm': 'none'}}]
    assert case_experiences(cases, {'task-a': {'task_name': 'task-a'}}) == {'one': [{'task_name': 'task-a'}]}
    with pytest.raises(ValueError, match='task-local'):
        case_experiences(cases, {'task-a': {'task_name': 'task-b'}})


@pytest.mark.parametrize('steps', [6, 10])
def test_edit_limit_rejected_before_native_dispatch(steps):
    from jsonschema import ValidationError, validate
    _, decision = context_and_decision()
    proposal = decision['proposal']
    proposal.update(mode='edit', steps=steps, edit={'delta_joints': {'fl_joint1': 0.03}})
    proposal['assessment']['intent_status'] = 'misaligned'
    with pytest.raises(ValidationError):
        validate(proposal, proposal_schema())
    with pytest.raises(ValueError, match='five'):
        validate_assessment(proposal, {'checkpoint': {'checkpoint_id': 'ckpt-001'},
            'candidate': {'freshness': 'pending'}, 'episode': {'steps': 0}})


@pytest.mark.asyncio
async def test_missing_usage_and_transport_failure_keep_actual_request_accounting(tmp_path):
    context, valid, _, calls, decider = decider_fixture(tmp_path, [])
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(valid)),
            finish_reason='stop')], usage=None, model='test')
    decider.client.chat.completions.create = create
    result = await decider(context)
    assert result['usage'] == {'provider_calls': 1, 'total_tokens': None}
    async def fail(**kwargs):
        raise ConnectionError('delivery unknown')
    decider.client.chat.completions.create = fail
    with pytest.raises(ConnectionError) as error:
        await decider(context)
    assert error.value.usage == {'provider_calls': 1, 'total_tokens': None}


def test_unknown_usage_stays_unknown_after_a_later_known_response():
    usage = ProviderUsage()
    usage.start()
    usage.record(None)
    usage.start()
    usage.record({'total_tokens': 42})
    assert usage.snapshot() == {'provider_calls': 2, 'total_tokens': None}


@pytest.mark.asyncio
async def test_cancellation_drains_a_dispatched_native_call_before_cleanup():
    import threading
    from owned_io import owned_call

    started, finish = threading.Event(), threading.Event()
    calls = []
    def dispatch():
        calls.append('dispatch')
        started.set()
        finish.wait(timeout=5)
        calls.append('delivered')
    task = asyncio.create_task(owned_call(dispatch))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ['dispatch', 'delivered']
