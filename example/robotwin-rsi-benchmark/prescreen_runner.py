import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

import yaml


class TrialError(RuntimeError):
    pass


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def request_json(api, route, arguments=None):
    body = None if arguments is None else json.dumps({'arguments': arguments}).encode()
    request = Request(api + route, data=body, headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get('ok') is not True:
        raise TrialError(f'gateway_rejected: {payload}')
    return payload


def normalize_description(value):
    return ' '.join(value.split()).casefold()


def resolve_task(tasks, task, description, requested_id=None):
    matches = [entry for entry in tasks
               if normalize_description(entry.get('language', '')) == normalize_description(description)
               and entry.get('name', task) == task]
    if not description.strip() or len(matches) != 1:
        raise TrialError('task_mapping_not_unique')
    entry = matches[0]
    if requested_id is not None and requested_id != entry['task_id']:
        raise TrialError(f'task_id_mismatch: requested={requested_id} live={entry["task_id"]}')
    return entry


def query_tool(api, tools, tool_id):
    matches = [tool for tool in tools if tool.get('tool_id') == tool_id
               and tool.get('available') and tool.get('semantics') == 'query']
    if len(matches) != 1:
        raise TrialError(f'query_tool_unavailable: {tool_id}')
    tool = matches[0]
    payload = request_json(api, f'/tools/{tool["endpoint_id"]}/{tool["operation"]}:invoke', {})
    response = payload['data']['response']
    if response.get('outcome') != 'completed' or response.get('result', {}).get('status') != 'succeeded':
        raise TrialError(f'query_failed: {tool_id}')
    return response['result']['outputs']


def validate_result(result, raw, task_id, max_steps, description):
    outputs = result.get('outputs', {})
    for value in (outputs, raw):
        if value.get('status') != 'succeeded':
            raise TrialError('benchmark_not_completed')
        if value.get('completed_episodes') != 1 or value.get('total_episodes') != 1:
            raise TrialError('incomplete_episode')
        if value.get('success_rate') not in (0, 1):
            raise TrialError('invalid_success_rate')
    if result.get('status') != 'succeeded':
        raise TrialError('invocation_failed')
    if outputs['success_rate'] != raw['success_rate']:
        raise TrialError('result_disagreement')
    episodes = raw.get('episodes', [])
    if len(episodes) != 1:
        raise TrialError('missing_episode')
    episode = episodes[0]
    if episode.get('task_id') != task_id or episode.get('init_state_id') != 0:
        raise TrialError('wrong_episode')
    if normalize_description(episode.get('task_description', '')) != normalize_description(description):
        raise TrialError('wrong_task_description')
    if episode.get('error_code') or episode.get('error_message'):
        raise TrialError('episode_runtime_error')
    if episode.get('termination') not in ('failed', 'success'):
        raise TrialError('invalid_termination')
    if raw.get('max_steps') != max_steps or raw.get('seed') != 0:
        raise TrialError('wrong_protocol')
    if not 0 < episode.get('num_steps', 0) <= max_steps:
        raise TrialError('invalid_steps')
    if episode.get('success') != (raw['success_rate'] == 1):
        raise TrialError('success_disagreement')


class Runner:
    def __init__(self, args):
        self.args = args
        self.root = args.root / args.task
        self.root.mkdir(parents=True, exist_ok=True)
        self.skill = 'robotwin-rsi-' + args.lane.lower()
        self.state_root = Path(os.environ['HOME']) / '.PhyAgentOS'
        self.skill_root = self.state_root / 'skills' / self.skill
        description_path = Path(os.environ['ROBOTWIN_ROOT']) / 'description/task_instruction' / (args.task + '.json')
        self.description = json.loads(description_path.read_text(encoding='utf-8'))['full_description']
        manifest = yaml.safe_load((self.skill_root / 'skill.yaml').read_text())
        self.api = manifest['gateway_url'].rstrip('/')
        gateway = yaml.safe_load((self.skill_root / 'profiles/g05_external/gateway.yaml').read_text())
        actual = f"http://{gateway['host']}:{gateway['port']}"
        if self.api != actual:
            raise TrialError(f'gateway_config_mismatch: {self.api} != {actual}')
        dataflow = yaml.safe_load((self.skill_root / 'profiles/g05_external/dataflow.yaml').read_text())
        nodes = {node['id']: node for node in dataflow['nodes']}
        if set(nodes) != {'gateway', 'robotwin_benchmark', 'robotwin_policy'}:
            raise TrialError('not_pure_pi05_profile')
        if nodes['robotwin_benchmark']['inputs']['action']['source'] != 'robotwin_policy/action':
            raise TrialError('policy_not_direct')
        save_json(self.root / 'MANIFEST.json', {
            **vars(args), 'root': str(args.root), 'skill': self.skill,
            'gateway_url': self.api, 'profile': 'g05_external',
            'policy_server': os.environ['G05_SERVER_URL'],
            'gpu': os.environ['CUDA_VISIBLE_DEVICES'], 'scene_request_seed': 0,
            'policy_rng_reset': False, 'lesson_enabled': False,
        })

    def event(self, message):
        line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message
        print(line, flush=True)
        with (self.root / 'events.log').open('a') as output:
            output.write(line + '\n')

    def cli(self, command, timeout=180):
        with (self.root / 'lifecycle.log').open('a') as output:
            result = subprocess.run(['paos', 'skill', *command, self.skill],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        return result.returncode

    def stop(self):
        self.cli(['stop', '--force'], timeout=60)

    def start(self):
        self.stop()
        with (self.root / 'lifecycle.log').open('a') as output:
            result = subprocess.run(['paos', 'skill', 'start', self.skill, '--profile', 'g05_external'],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode != 0:
            raise TrialError(f'skill_start_failed: exit={result.returncode}')
        state = json.loads((self.state_root / 'run/skills' / (self.skill + '.json')).read_text())
        if state.get('status') != 'running' or state.get('profile') != 'g05_external':
            raise TrialError(f'skill_not_ready: {state.get("status")}')
        if state.get('gateway_url') != self.api:
            raise TrialError('runtime_gateway_mismatch')
        tools = request_json(self.api, '/tools')['data']['tools']
        if not any(tool.get('tool_id') == 'robotwin.benchmark.run' and tool.get('available')
                   for tool in tools):
            raise TrialError('benchmark_tool_not_ready')
        capabilities = query_tool(self.api, tools, 'robotwin.benchmark.describe')
        policy = query_tool(self.api, tools, 'robotwin.policy.describe')
        if policy.get('ready') is not True or policy.get('action_dim') != 14:
            raise TrialError('policy_not_ready')
        entry = resolve_task(capabilities['tasks'], self.args.task, self.description, self.args.task_id)
        self.task_id = entry['task_id']
        save_json(self.root / 'TASK_MAPPING.json', {
            'task': self.args.task, 'resolved_task_id': self.task_id,
            'expected_description': self.description, 'live_entry': entry,
            'capabilities': capabilities, 'policy': policy,
        })
        save_json(self.root / 'READY.json', state)
        self.event('READY confirmed by PAOS and declared gateway')

    def trial(self, repetition, attempt):
        prefix = self.root / f'r{repetition}_a{attempt}'
        self.event(f'RUN_START repetition={repetition} attempt={attempt}')
        admission = request_json(self.api, '/tools/robotwin.benchmark.run:invoke', {
            'suite': 'robotwin', 'task_ids': [self.task_id], 'init_state_ids': [0],
            'num_runs': 1, 'seed': 0, 'max_steps': self.args.max_steps,
        })
        save_json(prefix.with_suffix('.admission.json'), admission)
        invocation_id = admission['data'].get('invocation_id')
        if not invocation_id:
            raise TrialError('missing_invocation_id')
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            payload = request_json(self.api, f'/invocations/{invocation_id}/result')
            data = payload['data']
            if 'result' in data:
                result = data['result']
                save_json(prefix.with_suffix('.invocation.json'), payload)
                result_path = result.get('outputs', {}).get('result_file')
                if not result_path or not Path(result_path).is_file():
                    raise TrialError('missing_native_result')
                raw = json.loads(Path(result_path).read_text())
                shutil.copyfile(result_path, prefix.with_suffix('.benchmark.json'))
                validate_result(result, raw, self.task_id, self.args.max_steps, self.description)
                summary = {
                    'task': self.args.task, 'resolved_task_id': self.task_id,
                    'repetition': repetition, 'attempt': attempt,
                    'classification': 'valid', 'invocation_id': invocation_id,
                    'success_rate': raw['success_rate'], 'num_steps': raw['num_steps'],
                    'scene_seed_ledger': raw['metrics']['scene_seed_ledger'],
                    'episode_seed': raw['episodes'][0]['episode_seed'],
                }
                save_json(self.root / f'r{repetition}.summary.json', summary)
                self.event(f'RUN_DONE repetition={repetition} success={raw["success_rate"]}')
                return
            time.sleep(5)
        raise TrialError('episode_wall_timeout')

    def run(self):
        try:
            self.start()
            for repetition in self.args.repetitions:
                for attempt in (1, 2):
                    try:
                        self.trial(repetition, attempt)
                        break
                    except Exception as error:
                        save_json(self.root / f'r{repetition}_a{attempt}.invalid.json', {
                            'task': self.args.task, 'repetition': repetition, 'attempt': attempt,
                            'classification': 'invalid', 'reason': str(error),
                        })
                        self.event(f'INVALID repetition={repetition} attempt={attempt} reason={error}')
                        if attempt == 2:
                            raise TrialError('retry_limit_reached') from error
                        self.start()
            self.event('LANE_COMPLETE')
        except Exception as error:
            save_json(self.root / 'ABORT.json', {'reason': str(error)})
            self.event(f'RUNNER_ABORT reason={error}')
            raise
        finally:
            self.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--task-id', type=int)
    parser.add_argument('--max-steps', type=int, required=True)
    parser.add_argument('--repetitions', type=int, nargs='+', required=True)
    Runner(parser.parse_args()).run()


if __name__ == '__main__':
    main()
