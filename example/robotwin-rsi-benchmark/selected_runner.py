import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import yaml

from prescreen_runner import query_tool, request_json, resolve_task, save_json, validate_result
from session_audit import audit_session, checkpoint_evidence


BASE = Path('/data6/lvqinhan/rsi')
OLD = BASE / 'lesson_ab_formal_20261011_p1_flash'
PYTHON = '/data7/home/lvqinhan/workspace/phyagentos/.venv/bin/python'
XPOLICY = Path('/data7/home/lvqinhan/workspace/paos-benchmark-integration/src/robotwin-upstream/XPolicyLab')
POLICY_PYTHON = str(XPOLICY / 'policy/Pi_05/openpi/.venv/bin/python')
ARMS = ['none', 'native', 'eslp', 'dialogue']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_free_port(port):
    with socket.socket() as listener:
        if sys.platform != 'win32':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))


class SelectedRunner:
    def __init__(self, args):
        self.args = args
        self.root = args.root / 'tasks' / args.task
        self.root.mkdir(parents=True, exist_ok=False)
        self.skill = 'robotwin-rsi-' + args.lane.lower()
        self.source_home = BASE / 'parallel10/homes' / args.lane
        self.home = args.root / 'homes' / args.lane
        self.state = self.home / '.PhyAgentOS'
        self.skill_root = self.state / 'skills' / self.skill
        self.workspace = self.state / 'workspace'
        self.api = f'http://127.0.0.1:{args.gateway_port}'
        self.description = json.loads((XPOLICY.parent / 'description/task_instruction' / (args.task + '.json')).read_text())['full_description']
        self.policy_process = None
        self.env = dict(os.environ, HOME=str(self.home), CUDA_VISIBLE_DEVICES=str(args.gpu),
                        G05_SERVER_URL=f'ws://127.0.0.1:{args.policy_port}')
        self.env['PYTHONPATH'] = str(BASE) + os.pathsep + self.env.get('PYTHONPATH', '')
        self.prepare_home()
        self.freeze_carriers()
        save_json(self.root / 'MANIFEST.json', {
            **vars(args), 'root': str(args.root), 'model': 'deepseek-flash',
            'arms': ['pure_pi05', *ARMS], 'policy_seeds': [101, 102, 103],
            'benchmark_seed': 0, 'init_state_id': 0, 'frozen_carriers': True,
            'online_lesson_update': False, 'policy_rng_reset': True,
            'pure_baseline_rechecked_per_policy_seed': True,
            'native_lessons_exploratory_same_case_support_not_independent_case_support': True,
        })

    def event(self, message):
        line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message
        print(line, flush=True)
        with (self.root / 'events.log').open('a') as output:
            output.write(line + '\n')

    def command(self, command, log, timeout):
        with log.open('a') as output:
            result = subprocess.run(command, env=self.env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'command_failed exit={result.returncode} command={command[:4]}')

    def prepare_home(self):
        self.home.mkdir(parents=True, exist_ok=False)
        self.skill_root.parent.mkdir(parents=True)
        shutil.copytree(self.source_home / '.PhyAgentOS/skills' / self.skill, self.skill_root, symlinks=True)
        source_runtime = self.source_home / '.PhyAgentOS/forge_runtime'
        (self.state / 'forge_runtime').mkdir()
        for cache in ['nodes', 'bundles']:
            if (source_runtime / cache).exists():
                (self.state / 'forge_runtime' / cache).symlink_to(source_runtime / cache, target_is_directory=True)
        manifest_path = self.skill_root / 'skill.yaml'
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest['gateway_url'] = self.api
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        for path in self.skill_root.glob('profiles/*/gateway.yaml'):
            gateway = yaml.safe_load(path.read_text())
            gateway['port'] = self.args.gateway_port
            path.write_text(yaml.safe_dump(gateway, sort_keys=False))
        for path in self.skill_root.glob('profiles/*/benchmark.yaml'):
            config = yaml.safe_load(path.read_text())
            config['record_dir'] = str(self.root / 'videos')
            path.write_text(yaml.safe_dump(config, sort_keys=False))
        self.base_skill = (OLD / 'tasks' / self.args.task / 'states/base_skill.md').read_text()
        config = json.loads((self.source_home / '.PhyAgentOS/config.rsi.ds.json').read_text())
        config['agents']['defaults']['workspace'] = str(self.workspace)
        if config['agents']['defaults']['model'] != 'deepseek-flash':
            raise RuntimeError('wrong_model')
        config_path = self.state / 'config.rsi.ds.json'
        save_json(config_path, config)
        config_path.chmod(0o600)
        temporary = self.home / 'tmp'
        temporary.mkdir()
        self.env.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
        save_json(self.root / 'policy_control.json', {'trial_id': 'bootstrap', 'policy_seed': 101})

    def freeze_carriers(self):
        source = OLD / 'tasks' / self.args.task
        reports = source / 'reports'
        native = json.loads((reports / 'replay1_native_native.json').read_text())
        lessons = native.get('lessons') or [lesson for lesson in native.get('all_lessons', []) if lesson.get('status') == 'active']
        if not lessons:
            raise RuntimeError('native_carrier_empty')
        frozen = self.root / 'frozen'
        frozen.mkdir()
        shutil.copy2(reports / 'replay1_native_native.json', frozen / 'native_report.json')
        bootstrap = json.loads((reports / 'bootstrap_native.json').read_text())
        sources = [(Path(bootstrap['session']), Path(bootstrap['result_file'])),
                   (source / 'sessions/replay1_native.jsonl', source / 'results/replay1_native.json')]
        provenance = []
        for index, (session_path, result_path) in enumerate(sources, 1):
            raw = json.loads(result_path.read_text())
            episode = raw['episodes'][0]
            validate_result({'status': 'succeeded', 'outputs': raw}, raw,
                            episode['task_id'], self.args.max_steps, self.description)
            if raw['success_rate'] != 0:
                raise RuntimeError('source_must_be_real_failure')
            output = frozen / f'source{index}'
            output.mkdir()
            shutil.copy2(session_path, output / 'session.jsonl')
            shutil.copy2(result_path, output / 'result.json')
            self.command([PYTHON, str(BASE / 'remote_lesson_ab/lesson_materializer.py'),
                          '--task', self.args.task, '--session', str(session_path),
                          '--result', str(result_path), '--native-report', str(frozen / 'native_report.json'),
                          '--out-dir', str(output / 'carriers')], self.root / 'materialize.log', 90)
            provenance.append({'session': str(session_path), 'result': str(result_path),
                               'session_sha256': digest(session_path), 'result_sha256': digest(result_path),
                               'scene_seed_ledger': raw['metrics']['scene_seed_ledger']})
        self.carriers = {'none': ''}
        self.carriers['native'] = '\n## ACTIVE EXPERIENCE CARRIER: PAOS NATIVE LESSON\n' + '\n'.join(
            json.dumps(lesson, ensure_ascii=False) for lesson in lessons) + '\n'
        sys.path.insert(0, str(BASE / 'remote_lesson_ab'))
        sys.path.insert(0, str(BASE))
        from lesson_materializer import render_dialogue, render_eslp

        manifests = [checkpoint_evidence(frozen / f'source{index}/session.jsonl') for index in (1, 2)]
        save_json(frozen / 'SELECTED_EVIDENCE.json', manifests)
        facts = []
        visuals = []
        rationales = []
        for index, manifest in enumerate(manifests, 1):
            facts.extend({'source_episode': index, **fact} for fact in manifest['evidence'][:2])
            visuals.extend({'source_episode': index, **visual} for visual in manifest['visuals'][:1])
            rationales.extend({'source_episode': index, **rationale} for rationale in manifest['assistant_reasoning'][:1])
        terminal = json.loads((frozen / 'source2/result.json').read_text())
        self.carriers['eslp'] = render_eslp(self.args.task, terminal, facts, visuals, rationales, lessons[:1])
        self.carriers['dialogue'] = render_dialogue(self.args.task, terminal, facts, visuals, rationales)
        for arm in ARMS:
            if len(self.carriers[arm]) + len(self.base_skill) > 14000:
                raise RuntimeError(f'carrier_exceeds_delivery_budget: {arm}')
        for arm, body in self.carriers.items():
            (frozen / (arm + '.md')).write_text(body, encoding='utf-8')
        save_json(frozen / 'PROVENANCE.json', {'sources': provenance,
                  'native_report_sha256': digest(frozen / 'native_report.json'),
                  'active_native_count': len(lessons),
                  'warning': 'All methods use the same two historical failed same-task episodes. Same-case retries are not independent support cases.'})

    def launch_policy(self):
        require_free_port(self.args.policy_port)
        env = dict(self.env, XLA_PYTHON_CLIENT_PREALLOCATE='false')
        with (self.root / 'policy_server.log').open('a') as output:
            self.policy_process = subprocess.Popen([
                POLICY_PYTHON, str(Path(__file__).with_name('policy_seed_server.py')),
                '--xpolicy-root', str(XPOLICY), '--port', str(self.args.policy_port),
                '--control', str(self.root / 'policy_control.json'),
                '--audit', str(self.root / 'policy_audit.jsonl')],
                cwd=str(XPOLICY), env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        save_json(self.root / 'POLICY_PID.json', {'pid': self.policy_process.pid, 'owned_by_this_runner': True})
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self.policy_process.poll() is not None:
                raise RuntimeError('own_policy_server_failed')
            try:
                with socket.create_connection(('127.0.0.1', self.args.policy_port), timeout=2):
                    return
            except OSError:
                time.sleep(3)
        raise RuntimeError('own_policy_server_start_timeout')

    def stop_skill(self):
        if not (self.state / 'run/skills' / (self.skill + '.json')).exists():
            return
        self.command(['paos', 'skill', 'stop', self.skill, '--force'], self.root / 'lifecycle.log', 60)

    def prepare_trial(self, trial, arm, seed):
        self.stop_skill()
        if self.workspace.exists():
            archive = self.root / 'workspace_archives'
            archive.mkdir(exist_ok=True)
            self.workspace.rename(archive / f'before_{trial}')
        self.workspace.mkdir()
        self.skill_root.joinpath('SKILL.md').write_text(self.base_skill + self.carriers.get(arm, ''), encoding='utf-8')
        save_json(self.root / 'policy_control.json', {'trial_id': trial, 'policy_seed': seed})
        profile = 'g05_external' if arm == 'pure_pi05' else 'gpt6_pi05'
        require_free_port(self.args.gateway_port)
        self.command(['paos', 'skill', 'start', self.skill, '--profile', profile], self.root / 'lifecycle.log', 180)
        runtime = json.loads((self.state / 'run/skills' / (self.skill + '.json')).read_text())
        if runtime.get('status') != 'running' or runtime.get('profile') != profile or runtime.get('gateway_url') != self.api:
            raise RuntimeError('runtime_not_ready')
        tools = request_json(self.api, '/tools')['data']['tools']
        capabilities = query_tool(self.api, tools, 'robotwin.benchmark.describe')
        entry = resolve_task(capabilities['tasks'], self.args.task, self.description)
        policy = query_tool(self.api, tools, 'robotwin.policy.describe')
        if policy.get('ready') is not True or policy.get('action_dim') != 14:
            raise RuntimeError('policy_not_ready')
        return profile, entry['task_id']

    def run_pure(self, trial_dir, task_id):
        admitted = request_json(self.api, '/tools/robotwin.benchmark.run:invoke', {
            'suite': 'robotwin', 'task_ids': [task_id], 'init_state_ids': [0],
            'num_runs': 1, 'seed': 0, 'max_steps': self.args.max_steps})
        save_json(trial_dir / 'admission.json', admitted)
        invocation_id = admitted['data']['invocation_id']
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            payload = request_json(self.api, f'/invocations/{invocation_id}/result')
            if 'result' in payload['data']:
                save_json(trial_dir / 'invocation.json', payload)
                path = Path(payload['data']['result']['outputs']['result_file'])
                return path
            time.sleep(5)
        raise RuntimeError('pure_episode_timeout')

    def prompt(self, arm):
        prompt = (BASE / 'parallel10/prompts' / f'{self.args.lane}_{self.args.task}.txt').read_text()
        prompt = prompt.replace(f'193{int(self.args.lane[1:]):02d}', str(self.args.gateway_port))
        replacement = f'- 先 activate_skill("{self.skill}")。'
        if arm == 'none':
            replacement += '本轮没有历史经验载体，只根据当前观测监督；不要引用任何历史 Lesson。'
        else:
            replacement += '完整读取 ACTIVE EXPERIENCE CARRIER 中同任务的两次历史失败证据，仅在适用条件满足时参考。'
        prompt = '\n'.join(replacement if line.startswith('- 先 activate_skill(') else line for line in prompt.splitlines())
        return prompt + '\n所有实验臂使用同一决策要求：每次决策简短说明当前 checkpoint、候选动作、参考的历史证据（无或具体条目）和放行/修改原因；不要为证明经验有效而强行修改。\n'

    def run_supervised(self, trial_dir, arm, trial, profile):
        prompt = self.prompt(arm)
        (trial_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')
        session = 'p2-' + self.args.task + '-' + trial
        current = (self.state / 'forge_runtime/environments' / self.skill / profile / 'current').resolve()
        result_dir = current / 'launch/profiles' / profile / 'results'
        existing = set(result_dir.glob('robotwin_*.json'))
        self.command(['paos', 'agent', '--session', session, '-c', str(self.state / 'config.rsi.ds.json'),
                      '--no-markdown', '-m', prompt], trial_dir / 'agent.log', 1800)
        paths = [path for path in result_dir.glob('robotwin_*.json') if path not in existing]
        if len(paths) != 1:
            raise RuntimeError(f'expected_exactly_one_result: found={len(paths)}')
        session_path = self.workspace / 'sessions' / (session + '.jsonl')
        shutil.copy2(session_path, trial_dir / 'session.jsonl')
        audit = audit_session(session_path, self.carriers[arm])
        save_json(trial_dir / 'SESSION_AUDIT.json', audit)
        if arm != 'none' and not audit['carrier_delivered_verbatim']:
            raise RuntimeError('carrier_not_delivered_verbatim')
        if arm == 'none' and audit['unexpected_carrier_in_none']:
            raise RuntimeError('control_arm_contaminated')
        return paths[0]

    def trial(self, repetition, arm):
        trial = f'r{repetition}_{arm}'
        trial_dir = self.root / 'trials' / trial
        trial_dir.mkdir(parents=True)
        seed = 100 + repetition
        self.event(f'TRIAL_START trial={trial} seed={seed}')
        profile, task_id = self.prepare_trial(trial, arm, seed)
        path = self.run_pure(trial_dir, task_id) if arm == 'pure_pi05' else self.run_supervised(trial_dir, arm, trial, profile)
        raw = json.loads(path.read_text())
        validate_result({'status': 'succeeded', 'outputs': raw}, raw, task_id, self.args.max_steps, self.description)
        shutil.copy2(path, trial_dir / 'benchmark.json')
        audit_path = self.root / 'policy_audit.jsonl'
        records = [json.loads(line) for line in audit_path.read_text().splitlines()]
        events = [record for record in records if record['trial_id'] == trial]
        if not any(record['event'] == 'rng_reset' and record['policy_seed'] == seed for record in events):
            raise RuntimeError('no_rng_reset_evidence')
        if not any(record['event'] == 'first_inference' for record in events):
            raise RuntimeError('no_policy_inference_evidence')
        save_json(trial_dir / 'POLICY_AUDIT.json', events)
        self.stop_skill()
        save_json(trial_dir / 'SUMMARY.json', {
            'classification': 'valid', 'task': self.args.task, 'arm': arm,
            'repetition': repetition, 'policy_seed': seed, 'runtime_task_id': task_id,
            'success_rate': raw['success_rate'], 'steps': raw['num_steps'],
            'scene_seed_ledger': raw['metrics']['scene_seed_ledger'],
            'policy_rng_reset_verified': True,
        })
        self.event(f'TRIAL_DONE trial={trial} success={raw["success_rate"]} steps={raw["num_steps"]}')

    def run(self):
        trial = None
        try:
            self.launch_policy()
            self.event('OWN_POLICY_READY')
            for repetition in range(1, 4):
                order = ARMS[repetition - 1:] + ARMS[:repetition - 1]
                for arm in ['pure_pi05', *order]:
                    trial = f'r{repetition}_{arm}'
                    self.trial(repetition, arm)
            save_json(self.root / 'COMPLETE.json', {'valid_trials': 15})
            self.event('TASK_COMPLETE')
        except Exception as error:
            save_json(self.root / 'ABORT.json', {'trial': trial, 'classification': 'invalid', 'reason': str(error)})
            self.event(f'RUNNER_ABORT trial={trial} reason={error}')
            raise
        finally:
            try:
                self.stop_skill()
            finally:
                if self.policy_process and self.policy_process.poll() is None:
                    os.killpg(self.policy_process.pid, signal.SIGTERM)
                    try:
                        self.policy_process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.policy_process.pid, signal.SIGKILL)
                        self.policy_process.wait(timeout=10)
                save_json(self.root / 'CLEANUP.json', {'own_policy_exited': self.policy_process is None or self.policy_process.poll() is not None})


def main():
    def terminate(signum, frame):
        raise KeyboardInterrupt('termination_requested')

    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--max-steps', type=int, required=True)
    parser.add_argument('--gpu', type=int, required=True)
    parser.add_argument('--gateway-port', type=int, required=True)
    parser.add_argument('--policy-port', type=int, required=True)
    args = parser.parse_args()
    SelectedRunner(args).run()


if __name__ == '__main__':
    main()
