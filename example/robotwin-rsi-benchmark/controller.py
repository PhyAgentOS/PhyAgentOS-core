import asyncio

from owned_io import owned_call
import copy
import json
import os
from pathlib import Path
import signal
import socket
import time
from urllib.error import HTTPError

import httpx
from jsonschema import ValidationError, validate
from openai import AsyncOpenAI

from PhyAgentOS.agent.benchmark.media import ObservationArtifacts
from PhyAgentOS.forge.benchmark.client import SCOPE_KEYS
from PhyAgentOS.forge.interaction.contracts import digest

from fixed_runner import FixedRunner, PYTHON, initial_identity, sha256
from prescreen_runner import request_json, save_json, validate_result
from supervision import Supervisor
from response_validation import decision_object, decision_template, validation_feedback
from model_config import ProviderUsage, model_config


def validate_native_decision(proposal):
    if proposal['mode'] == 'edit' and proposal.get('steps', 5) > 5:
        raise ValueError('Edit segments are limited to five native steps')
    if proposal['mode'] == 'student' and 'edit' in proposal:
        raise ValueError('student takes no edit block; regenerate without edit rather than submitting it')


class Runtime(FixedRunner):
    def prepare_home(self):
        super().prepare_home()
        for name, entrypoint in [('vla_bridge', 'vla_bridge.main'), ('image_vision', 'image_vision.main')]:
            script = self.skill_root / 'nodes' / name / (name + '.sh')
            script.unlink(missing_ok=True)  # Do not follow a copied launcher symlink into the source installation.
            script.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
                'exec "$ROOT/.venv/bin/python" -c "from ' + entrypoint + ' import main; main()" "$@"\n')
            script.chmod(0o755)

    def __init__(self, args):
        super().__init__(args)
        self.env['PYTHONPATH'] = str(getattr(args, 'core', None) or os.environ.get('RSI_CORE') or Path(__file__).resolve().parents[2]) + os.pathsep + str(args.root)

    def command(self, command, log, timeout):
        if command[0] == 'paos':
            command = [getattr(self.args, 'agent_python', None) or PYTHON, '-m', 'PhyAgentOS.cli.commands', *command[1:]]
        return super().command(command, log, timeout)

    def executor_processes(self):
        processes = []
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if ('HOME=' + str(self.home)).encode() not in (directory / 'environ').read_bytes().split(b'\0'):
                    continue
                pid = int(directory.name)
                if self.policy_process and os.getpgid(pid) == self.policy_process.pid:
                    continue
                stat = (directory / 'stat').read_text().split(') ', 1)[1].split()
                if stat[0] != 'Z':
                    processes.append({'pid': pid, 'start_ticks': stat[19],
                        'name': (directory / 'comm').read_text().strip()})
            except (OSError, IndexError):
                continue
        return processes

    def stop_executor(self):
        before = self.executor_processes()
        self.stop_skill()
        state_path = self.state / 'run/skills' / (self.skill + '.json')
        state = json.loads(state_path.read_text())
        if state.get('status') != 'stopped':
            raise RuntimeError('owned_skill_shutdown_not_confirmed')
        deadline = time.monotonic() + 10
        while True:
            remaining = self.executor_processes()
            if not remaining:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('owned_executor_processes_still_live:' + json.dumps(remaining))
            time.sleep(0.2)
        return {'method': 'owned_skill_shutdown', 'runtime_status': state['status'],
            'runtime_state_sha256': sha256(state_path), 'before': before, 'remaining': remaining,
            'policy_group_excluded': self.policy_process.pid if self.policy_process else None,
            'timestamp': time.time()}

    def close(self):
        stop_error = None
        executor_proven = False
        policy_error = None
        try:
            state_path = self.state / 'run/skills' / (self.skill + '.json')
            if state_path.exists():
                self.stop_executor()
            elif self.executor_processes():
                raise RuntimeError('owned_executor_has_no_runtime_state')
            executor_proven = True
        except Exception as error:
            stop_error = str(error)
        try:
            if self.policy_process and self.policy_process.poll() is None:
                os.killpg(self.policy_process.pid, signal.SIGTERM)
                try:
                    self.policy_process.wait(timeout=30)
                except Exception:
                    os.killpg(self.policy_process.pid, signal.SIGKILL)
                    self.policy_process.wait(timeout=10)
        except Exception as error:
            policy_error = str(error)
        finally:
            save_json(self.root / 'CLEANUP.json', {'own_policy_exited': self.policy_process is None or
                self.policy_process.poll() is not None, 'skill_stop_error': stop_error,
                'policy_stop_error': policy_error, 'executor_shutdown_proven': executor_proven,
                'shared_models_untouched': True})


class RoboTwinAdapter:
    def __init__(self, runtime):
        self.runtime = runtime
        # A new process cannot reconstruct native invocation authority from the
        # diagnostic files. Refuse reuse before any native request, even after
        # an apparently clean prior run; reconciliation is an operator action.
        owner = runtime.root / 'ADAPTER_OWNER.json'
        with owner.open('x') as output:
            json.dump({'pid': os.getpid(), 'host': socket.gethostname(),
                'started_at': time.time(), 'automatic_resume': False,
                'recovery': 'Stop and reconcile the original owned runtime before a new batch.'}, output)
        self.artifacts = ObservationArtifacts()
        self.roots, self.episodes, self.receipts, self.invocations = {}, {}, {}, {}
        self.active = None
        self.specs = {f'benchmark.{operation}': {
            'tool_id': f'benchmark.{operation}', 'endpoint_id': 'benchmark', 'operation': operation,
            'semantics': semantics, 'available': True, 'input_schema': {'type': 'object'}}
            for operation, semantics in [('run', 'session'), ('snapshot', 'query'), ('submit', 'action')]}
        self.lock = asyncio.Lock()

    async def native(self, route, arguments=None):
        try:
            return await owned_call(request_json, self.runtime.api, route, arguments)
        except HTTPError as error:
            body = error.read().decode('utf-8', errors='replace')
            detail = {'route': route, 'status': error.code, 'body': body}
            with (self.runtime.root / 'NATIVE_ERROR_AUDIT.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(detail, ensure_ascii=False) + '\n')
            raise RuntimeError('native_http_error:' + json.dumps(detail, ensure_ascii=False)) from error

    async def query(self, tool, arguments=None):
        spec = next(item for item in self.tools if item['tool_id'] == tool)
        reply = await self.native(f'/tools/{spec["endpoint_id"]}/{spec["operation"]}:invoke', arguments or {})
        record = reply['data']['response']['result']
        if record.get('status') != 'succeeded':
            raise RuntimeError('native_query_failed:' + tool)
        return record['outputs']

    async def action(self, tool, arguments):
        spec = next(item for item in self.tools if item['tool_id'] == tool)
        if spec['semantics'] == 'query':
            return await self.query(tool, arguments)
        reply = await self.native('/tools/' + tool + ':invoke', arguments)
        invocation = reply['data']['invocation_id']
        for _ in range(100):
            status = await self.native('/invocations/' + invocation)
            if status['data']['status'] in {'succeeded', 'failed', 'stopped'}:
                break
            await asyncio.sleep(0.1)
        result = (await self.native('/invocations/' + invocation + '/result'))['data'].get('result')
        if not result or result.get('status') != 'succeeded':
            raise RuntimeError('native_action_failed:' + tool)
        return result['outputs']

    def save(self, episode, *, state, observation, outcome=None, score=None, stopped=False):
        prior = episode.get('snapshot')
        value = {**episode['scope'], 'observation_version': 0 if prior is None else prior['observation_version'],
            'observation': observation, 'state': state, 'outcome': outcome, 'native_score': score, 'stopped': stopped}
        if not prior or value != {key: item for key, item in prior.items() if key != 'history_digest'}:
            if prior:
                value['observation_version'] += 1
            episode['history'].append(copy.deepcopy(value))
        value['history_digest'] = digest(episode['history'])
        episode['snapshot'] = value
        save_json(episode['directory'] / 'ADAPTER_STATE.json', {
            'snapshot': value, 'native_invocation': episode['native_invocation'],
            'history': episode['history'], 'receipts': self.receipts})
        return value

    async def begin(self, arguments):
        if self.active is not None:
            raise RuntimeError('worker already owns an episode')
        environment = arguments['case']['environment']
        if environment['task_name'] != self.runtime.args.task or environment['arm'] not in {'none', 'fixed'}:
            raise RuntimeError('cross-task episode refused')
        directory = self.runtime.root / environment['trial']
        directory.mkdir()
        task_id, _ = await owned_call(self.runtime.prepare_trial, directory, 'none', environment['seed'])
        self.tools = (await self.native('/tools'))['data']['tools']
        await self.action('vla.set_mode', {'mode': 'stop'})
        native_arguments = {'suite': 'robotwin', 'task_ids': [task_id], 'init_state_ids': [0],
            'num_runs': 1, 'seed': 0, 'max_steps': self.runtime.args.max_steps}
        admitted = await self.native('/tools/robotwin.benchmark.run:invoke', native_arguments)
        episode = {'scope': {key: arguments[key] for key in SCOPE_KEYS}, 'environment': environment,
            'directory': directory, 'native_invocation': admitted['data']['invocation_id'],
            'supervisor': Supervisor(directory / 'frames'), 'history': [], 'gates': [], 'ready': False}
        self.active = arguments['subtask_id']
        self.episodes[self.active] = episode
        save_json(directory / 'START.json', {'scope': episode['scope'], 'environment': environment,
            'native_arguments': native_arguments, 'admission': admitted})
        self.runtime.event('EPISODE_START ' + environment['trial'])
        return self.save(episode, state='running', observation={'stage': 'native_episode_start'})

    def validate_native_result(self, episode, result):
        directory = episode['directory']
        result_path = Path(result['outputs'].get('result_file', '')).resolve()
        if not result_path.is_file() or not (result_path.is_relative_to(self.runtime.home.resolve())
                or result_path.is_relative_to(self.runtime.root.resolve())):
            raise RuntimeError('native_result_file_not_owned')
        raw = json.loads(result_path.read_text())
        validate_result(result, raw, self.runtime.reference['scene']['task_id'],
                        self.runtime.args.max_steps, self.runtime.description)
        ledger = raw['metrics']['scene_seed_ledger']
        scene = {key: ledger[0][key] for key in ('task_id', 'init_state_id', 'scene_seed', 'instruction')}
        if len(ledger) != 1:
            raise RuntimeError('scene_or_instruction_drift')
        if getattr(self.runtime.args, 'mode', None) == 'smoke':
            expected = self.runtime.reference['scene']
            if any(scene[key] != expected[key] for key in ('task_id', 'init_state_id', 'instruction')):
                raise RuntimeError('scene_or_instruction_drift')
            # A smoke establishes its own observed identity; it is not a matched screening comparison.
            self.runtime.reference['scene'] = scene
            save_json(self.runtime.root / 'SMOKE_SCENE.json', scene)
        elif scene != self.runtime.reference['scene']:
            raise RuntimeError('scene_or_instruction_drift')
        events = [json.loads(line) for line in (self.runtime.root / 'policy_audit.jsonl').read_text().splitlines()]
        events = [event for event in events if event.get('trial_id') == directory.name]
        if not any(event['event'] == 'rng_reset' and event['policy_seed'] == episode['environment']['seed'] for event in events):
            raise RuntimeError('missing_actual_rng_reset')
        identity = initial_identity(events)
        reference_path = self.runtime.root / 'RUNTIME_INITIAL_REFERENCE.json'
        if not reference_path.exists():
            if not episode['environment']['development'] and getattr(self.runtime.args, 'mode', None) != 'smoke':
                raise RuntimeError('formal_episode_cannot_establish_runtime_reference')
            save_json(reference_path, {'initial_observation': identity,
                'screening_equal': identity == self.runtime.reference['initial_observation'],
                'policy_independent_scene_reference': self.runtime.reference['scene'],
                'runtime': os.environ.get('RSI_RUNTIME_LABEL', 'host-local runtime; paired arms remain on this worker')})
        if identity != json.loads(reference_path.read_text())['initial_observation']:
            raise RuntimeError('initial_observation_differs_from_runtime_reference')
        if not episode['gates']:
            raise RuntimeError('no_actual_multimodal_decisions')
        save_json(directory / 'benchmark.json', raw)
        save_json(directory / 'POLICY_AUDIT.json', events)
        environment = episode['environment']
        summary = {'classification': 'valid', 'task': self.runtime.args.task, 'arm': environment['arm'],
            'development': environment['development'], 'policy_seed': environment['seed'],
            'repetition': environment['repetition'], 'success_rate': raw['success_rate'], 'steps': raw['num_steps'],
            'scene': scene, 'initial_observation': identity, 'decision_count': len(episode['gates']),
            'accepted_edits': sum(gate['proposal']['mode'] == 'edit' for gate in episode['gates']),
            'core_scope': episode['scope']}
        save_json(directory / 'SUMMARY.json', summary)
        self.runtime.event('EPISODE_VALID ' + directory.name + ' success=' + str(raw['success_rate']))
        return raw

    async def snapshot(self, episode):
        if episode.get('terminal'):
            return episode['snapshot']
        result_data = (await self.native('/invocations/' + episode['native_invocation'] + '/result'))['data']
        result = result_data.get('result')
        if result is not None:
            try:
                raw = self.validate_native_result(episode, result)
                outcome, score = ('success' if raw['success_rate'] else 'failure'), raw['success_rate']
                observation = {'native_result': raw, 'result_sha256': digest(raw)}
            except Exception as error:
                outcome, score = 'infra_error', None
                observation = {'infra_error': str(error), 'native_result': result}
                save_json(episode['directory'] / 'INVALID.json', observation)
            episode['terminal'] = True
            return self.save(episode, state='terminal', observation=observation, outcome=outcome, score=score)
        if episode['ready']:
            return episode['snapshot']
        status = await self.query('vla.get_status')
        if not (status.get('checkpoint') or {}).get('checkpoint_id') or (status.get('candidate') or {}).get('freshness') != 'pending':
            return self.save(episode, state='running', observation={'stage': 'waiting_for_checkpoint',
                'step': (status.get('episode') or {}).get('steps')})
        supervisor = episode['supervisor']
        supervisor.observe_status(status)
        for tool in ['vision.get_frame', 'vision.get_frame_left_wrist', 'vision.get_frame_right_wrist']:
            frame = await self.query(tool, {'max_age_ms': 600000})
            supervisor.observe_frame(tool, frame)
        if 'head' not in supervisor.frames or len(supervisor.frames) < 2:
            raise RuntimeError('missing_current_head_or_wrist_image')
        observation = {'task_instruction': self.runtime.reference['scene']['instruction'],
            'task_description': self.runtime.description,
            'packet': supervisor.packet(), 'candidate_binding': supervisor.binding()}
        scope = episode['scope']
        references = []
        frame_groups = [('current', supervisor.frames)]
        if supervisor.previous:
            frame_groups.insert(0, ('previous', supervisor.previous['frames']))
        for label, frames in frame_groups:
            for camera, frame in frames.items():
                references.append(self.artifacts.register(scope, observation, label + '_' + camera,
                    Path(frame['path']).read_bytes(), frame['mime_type']))
        observation['artifacts'] = references
        episode['ready'] = True
        return self.save(episode, state='ready', observation=observation)

    async def submit(self, arguments):
        request_id = arguments['request_id']
        if request_id in self.receipts:
            if self.receipts[request_id]['payload_digest'] != digest(arguments):
                raise RuntimeError('request id reused with different payload')
            return self.receipts[request_id]
        operation, key = arguments['operation'], arguments['subtask_id']
        if operation == 'episode.start':
            await self.begin(arguments)
        else:
            episode = self.episodes[key]
            if any(arguments.get(name) != episode['scope'][name] for name in SCOPE_KEYS):
                raise RuntimeError('cross-episode submission')
            if operation == 'decision.submit':
                snapshot = episode['snapshot']
                if snapshot['state'] != 'ready' or arguments['observation_version'] != snapshot['observation_version'] or arguments['observation_digest'] != digest(snapshot['observation']):
                    raise RuntimeError('stale decision')
                supervisor = episode['supervisor']
                fresh = await self.query('vla.get_status')
                supervisor.observe_status(fresh)
                proposal = arguments['proposal']
                validate_native_decision(proposal)
                supervisor.check('forge_tool_call', {'tool_id': 'vla.decide', 'arguments': proposal})
                allowed = {name: value for name, value in proposal.items() if name != 'assessment'}
                applied = await self.action('vla.decide', allowed)
                if applied.get('accepted') is not True:
                    raise RuntimeError('bridge_rejected_decision:' + json.dumps(applied))
                accepted = supervisor.finish_decision(applied)
                gate = {'scope': episode['scope'], 'proposal': proposal, 'applied': applied,
                    'binding': accepted['binding'], 'observation_digest': arguments['observation_digest']}
                episode['gates'].append(gate)
                with (episode['directory'] / 'GATE_AUDIT.jsonl').open('a') as output:
                    output.write(json.dumps(gate, ensure_ascii=False) + '\n')
                episode['ready'] = False
                self.save(episode, state='running', observation={'stage': 'segment_executing',
                    'previous_binding': gate['binding']})
            elif operation == 'episode.stop':
                native_status = None
                native_stop_error = None
                if not episode.get('terminal'):
                    try:
                        await self.native('/invocations/' + episode['native_invocation'] + '/stop', {})
                        for _ in range(6):
                            native_status = (await self.native('/invocations/' + episode['native_invocation']))['data']
                            if native_status.get('phase', native_status.get('status')) in {'completed', 'succeeded', 'failed', 'stopped', 'cancelled'}:
                                break
                            await asyncio.sleep(0.5)
                    except Exception as error:
                        native_stop_error = f'{type(error).__name__}: {error}'
                proof = await owned_call(self.runtime.stop_executor)
                proof.update(native_invocation=episode['native_invocation'], native_status=native_status,
                    native_stop_error=native_stop_error,
                    native_terminal_before_shutdown=bool(episode.get('terminal')) or bool(native_status and
                        native_status.get('phase', native_status.get('status')) in
                        {'completed', 'succeeded', 'failed', 'stopped', 'cancelled'}))
                save_json(episode['directory'] / 'STOP_PROOF.json', proof)
                previous = episode['snapshot']
                observation = {**previous['observation'], 'executor_stop_proof': proof}
                if previous['outcome'] is None:
                    observation['infra_error'] = 'episode_interrupted_by_owned_executor_shutdown'
                    save_json(episode['directory'] / 'INVALID.json', observation)
                self.save(episode, state='terminal', observation=observation,
                    outcome=previous['outcome'] or 'infra_error', score=previous['native_score'], stopped=True)
                episode['terminal'] = True
                self.active = None
            else:
                raise ValueError('unsupported operation')
        invocation = 'submission_' + digest(arguments)[:24]
        receipt = {**{name: arguments[name] for name in SCOPE_KEYS}, 'request_id': request_id,
            'transport_invocation_id': invocation, 'payload_digest': digest(arguments), 'accepted': True}
        self.receipts[request_id] = receipt
        self.invocations[invocation] = receipt
        save_json(self.runtime.root / 'ADAPTER_RECEIPTS.json', self.receipts)
        return receipt

    async def handle(self, request):
        async with self.lock:
            path = request.url.path
            body = json.loads(request.content) if request.content else {}
            arguments = body.get('arguments', {})
            def response(data, code=200):
                return httpx.Response(code, json={'ok': True, 'data': data})
            if path == '/tools':
                return response({'tools': list(self.specs.values())})
            if path.startswith('/tools/') and request.method == 'GET':
                return response({'ready': True, 'binding_error': None} if path.endswith('/context')
                    else self.specs[path.split('/')[2]])
            if path == '/tools/benchmark.run:invoke':
                key = arguments['task_id']
                if key in self.roots:
                    raise RuntimeError('root already admitted')
                invocation = 'root_' + digest(arguments)[:24]
                self.roots[key] = {**arguments, 'start_digest': digest(arguments),
                    'session_invocation_id': invocation, 'stopped': False}
                self.invocations[invocation] = key
                return response({'invocation_id': invocation, 'attempt_id': invocation}, 202)
            if path == '/tools/benchmark.submit:invoke':
                receipt = await self.submit(arguments)
                return response({'invocation_id': receipt['transport_invocation_id'],
                    'attempt_id': receipt['transport_invocation_id']}, 202)
            if path == '/tools/benchmark/snapshot:invoke':
                root = self.roots[arguments['task_id']]
                if any(root.get(name) != arguments.get(name) for name in ('task_id', 'run_id', 'binding_digest')):
                    raise RuntimeError('cross-root query')
                view = arguments['view']
                if view == 'root':
                    value = root
                else:
                    episode = self.episodes[arguments['subtask_id']]
                    if any(arguments.get(name) != episode['scope'][name] for name in SCOPE_KEYS):
                        raise RuntimeError('cross-episode query')
                    if view == 'episode':
                        value = await self.snapshot(episode)
                    elif view == 'history':
                        value = {'complete': bool(episode['snapshot']['stopped']), 'records': episode['history']}
                    elif view == 'receipt':
                        receipt = self.receipts.get(arguments['request_id'])
                        value = {'status': 'found', 'receipt': receipt} if receipt else {'status': 'unknown'}
                    else:
                        raise ValueError('unsupported view')
                return response({'response': {'result': {'status': 'succeeded', 'outputs': value}}})
            if path.startswith('/invocations/'):
                invocation = path.split('/')[2]
                value = self.invocations[invocation]
                if isinstance(value, str):
                    root = self.roots[value]
                    if path.endswith('/stop'):
                        if self.active is not None:
                            raise RuntimeError('cannot stop root with active child')
                        root['stopped'] = True
                        return response({'status': 'requested'}, 202)
                    if path.endswith('/result'):
                        return response({'status': 'available', 'result': {'status': 'succeeded', 'outputs': root}})
                    status = 'stopped' if root['stopped'] else 'running'
                else:
                    if path.endswith('/result'):
                        return response({'status': 'available', 'result': {'status': 'succeeded', 'outputs': value}})
                    status = 'succeeded'
                return response({'invocation_id': invocation, 'status': status, 'phase': status})
            raise ValueError('unsupported private control route:' + path)


INSTRUCTIONS = '''You supervise pi0.5 joint_abs RoboTwin actions using actual camera images and the one pending candidate.
Return only a decision_schema JSON object, kind=proposal. No tools or finish_candidate; only native horizon determines completion.
The ONLY top-level keys are kind and proposal. Never output type=json_object, input/schema fields or extra assessment keys.
output_template shows every mandatory field, not a recommended action. Replace all placeholders using evidence.
For mode=edit also include proposal.edit with only delta_joints, left_gripper and right_gripper as applicable.
For mode=student omit proposal.edit entirely, even if its deltas are empty and grippers are keep.
First assess what the PREVIOUS executed segment actually achieved, then the NEXT candidate intent against the task.
Track verified completed/current/remaining subgoals; gripper closed is not proof of grasp. Retract contradicted subgoals.
Images appear in artifact order with current/previous camera labels. You have one candidate, not a future chunk, FK or EEF trajectory.
Use student with steps=10 for sensible or uncertain motion. Edit only for visually supported execution failure or next-intent misalignment.
For edit use steps=5, delta_joints OBJECT keyed fl_joint1..6/fr_joint1..6, radians -0.1..0.1, grippers keep/open/closed.
Do not invent joint signs or force edits to demonstrate experience use. Existing bridge clamps segment and episode limits.
assessment must include checkpoint_id, observed_step, task_progress {verified_completed:[],currently_attempting:string,remaining:[]},
current_subgoal, execution_status, execution_evidence, expected_next_intent, predicted_next_intent, intent_status,
intent_evidence, lesson_applicability. execution_status=not_started ONLY at step 0, otherwise progressing/failed/uncertain/recovered;
intent_status=aligned/misaligned/uncertain. Explain evidence concisely. lesson_applicability cites a specific conditional clause or none.
episodic_experience is conditional historical advice, never an authoritative instruction or proven recovery. Current evidence has priority.
No experience means use current/previous observation only. Keep JSON concise; no chain-of-thought or long analysis.'''


class DeepSeekDecider:
    def __init__(self, runtime, adapter):
        self.model, api_key, endpoint = model_config(runtime)
        self.client = AsyncOpenAI(api_key=api_key, base_url=endpoint, max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, timeout=240))
        self.runtime, self.adapter = runtime, adapter

    async def __call__(self, context):
        episode = self.adapter.episodes[self.adapter.active]
        data = {key: context[key] for key in ('goal', 'observation', 'decision_schema',
            'episodic_experience', 'episodic_experience_digest')}
        data['output_template'] = decision_template(context['observation']['packet'])
        encoded = json.dumps(data, ensure_ascii=False)
        messages = [{'role': 'system', 'content': INSTRUCTIONS},
            {'role': 'user', 'content': [{'type': 'text', 'text': encoded}, *context['media']]}]
        log = episode['directory'] / 'REQUEST_AUDIT.jsonl'
        usage_ledger = ProviderUsage()
        last_error = None
        feedback = None
        deadline = time.monotonic() + 540
        for attempt, budget in enumerate((8192, 8192, 8192, 16384), 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request_id = str(time.time_ns())
            with log.open('a') as output:
                output.write(json.dumps({'event': 'request', 'request_id': request_id, 'model': self.model,
                    'max_tokens': budget, 'attempt': attempt, 'context': data,
                    'repair_feedback': feedback,
                    'image_references': context['observation']['artifacts']}, ensure_ascii=False) + '\n')
            usage_ledger.start()
            try:
                async with asyncio.timeout(min(240, remaining)):
                    response = await self.client.chat.completions.create(model=self.model, messages=messages,
                        max_tokens=budget, temperature=0.3, response_format={'type': 'json_object'})
            except BaseException as error:
                error.usage = usage_ledger.snapshot()
                with log.open('a') as output:
                    output.write(json.dumps({'event': 'request_error', 'request_id': request_id,
                        'error_type': type(error).__name__, 'usage': error.usage}) + '\n')
                raise
            choice = response.choices[0]
            usage = response.usage.model_dump() if response.usage else None
            usage_ledger.record(usage)
            with log.open('a') as output:
                output.write(json.dumps({'event': 'response', 'request_id': request_id,
                    'returned_model': response.model, 'finish_reason': choice.finish_reason,
                    'content': choice.message.content, 'usage': usage}, ensure_ascii=False) + '\n')
            try:
                decision = decision_object(choice.message.content, choice.finish_reason, context['decision_schema'])
                from supervision import validate_assessment
                validate_assessment(decision['proposal'], episode['supervisor'].status)
                validate_native_decision(decision['proposal'])
                episode['supervisor'].delivered_binding = episode['supervisor'].binding()
                decision['usage'] = usage_ledger.snapshot()
                return decision
            except (ValueError, KeyError, TypeError, ValidationError) as error:
                feedback = validation_feedback(error)
                last_error = feedback['error']
                with log.open('a') as output:
                    output.write(json.dumps({'event': 'validation_error', 'request_id': request_id,
                        'feedback': feedback}, ensure_ascii=False) + '\n')
                messages = [messages[0], messages[1],
                    {'role': 'assistant', 'content': (choice.message.content or '<empty response>')[:12000]},
                    {'role': 'user', 'content': json.dumps(feedback, ensure_ascii=False)}]
        error = RuntimeError('Model output invalid after bounded feedback retries:' + str(last_error))
        error.usage = usage_ledger.snapshot()
        raise error
