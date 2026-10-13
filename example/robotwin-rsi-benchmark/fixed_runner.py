import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time

import yaml

from prescreen_runner import query_tool, request_json, resolve_task, save_json, validate_result
from selected_runner import SelectedRunner, require_free_port
from session_audit import audit_session, outputs_of, records_and_calls
from response_validation import reflection_object
from supervision import VERSION


BASE = Path(os.environ.get('RSI_BASE', '/data6/lvqinhan/rsi'))
PYTHON = os.environ.get('RSI_AGENT_PYTHON', '/data7/home/lvqinhan/workspace/phyagentos/.venv/bin/python')
XPOLICY = Path(os.environ.get('RSI_XPOLICY', '/data7/home/lvqinhan/workspace/paos-benchmark-integration/src/robotwin-upstream/XPolicyLab'))
POLICY_PYTHON = str(XPOLICY / 'policy/Pi_05/openpi/.venv/bin/python')
PROFILE = 'gpt6_pi05'
TASKS = ['place_object_scale', 'handover_block', 'press_stapler', 'turn_switch']


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair_schedule(task):
    generator = random.Random(20261012 + TASKS.index(task))
    rows = []
    for repetition in range(1, 13):
        arms = ['none', 'fixed']
        generator.shuffle(arms)
        rows.append({'repetition': repetition, 'seed': 2000 + repetition, 'arms': arms})
    return rows


def initial_identity(events):
    first = next(record for record in events if record['event'] == 'first_inference')
    return {key: first[key] for key in ('state', 'images', 'prompt')}


def message_contains_carrier(message, carrier):
    content = message.get('content')
    if not isinstance(content, str):
        return False
    if carrier in content:
        return True
    try:
        payload = json.loads(content)
    except ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get('skill'), str) and carrier in payload['skill']


def process_evidence(session_path, request_path, frames_root, carrier):
    records, calls = records_and_calls(session_path)
    audit = audit_session(session_path, carrier)
    visuals = []
    candidates = []
    latest = {}
    for call in calls:
        tool_id = call['arguments'].get('tool_id', '')
        output = outputs_of(call)
        if tool_id == 'vla.get_status':
            latest = output
            candidate = output.get('candidate') or {}
            if candidate.get('freshness') == 'pending':
                candidates.append({'step': (output.get('episode') or {}).get('steps'), 'candidate': candidate})
        if tool_id.startswith('vision.get_frame') and output.get('image_path'):
            path = Path(output['image_path']).resolve()
            if not path.is_relative_to(frames_root.resolve()) or not path.is_file():
                raise RuntimeError('frame_path_not_owned_by_current_trial')
            visuals.append({'tool': tool_id, 'image_path': str(path), 'sha256': sha256(path),
                'frame_seq': output.get('frame_seq'), 'frame_age_ms': output.get('frame_age_ms'),
                'checkpoint': latest.get('checkpoint'), 'candidate': latest.get('candidate')})
    requests = [json.loads(line) for line in request_path.read_text().splitlines() if line.strip()]
    actual_inputs = [row for row in requests if row['event'] == 'request']
    errors = [row for row in requests if row['event'] == 'response' and row.get('finish_reason') == 'error']
    image_requests = []
    for row in actual_inputs:
        encoded = json.dumps(row['messages'], ensure_ascii=False)
        if 'image_sha256' in encoded:
            image_requests.append(row['request_id'])
    main_inputs = [row for row in actual_inputs if row.get('tools')]
    delivered = any(any(message_contains_carrier(message, carrier) for message in row['messages'])
                    for row in main_inputs) if carrier else None
    polluted = not carrier and any('ACTIVE EXPERIENCE CARRIER:' in json.dumps(row['messages'], ensure_ascii=False)
                                  for row in main_inputs)
    audit.update(actual_request_count=len(actual_inputs), actual_image_request_count=len(image_requests),
                 image_request_ids=image_requests, actual_main_input_contains_carrier=delivered,
                 no_lesson_control_contaminated=polluted, cameras=visuals, pending_candidates=candidates,
                 provider_error_count=len(errors))
    if not visuals or not image_requests or not candidates:
        raise RuntimeError('missing_visual_or_pending_candidate_evidence')
    if carrier and (not audit['carrier_delivered_verbatim'] or not delivered):
        raise RuntimeError('frozen_lesson_not_in_actual_model_context')
    if polluted or audit['unexpected_carrier_in_none']:
        raise RuntimeError('no_lesson_control_contaminated')
    if errors:
        raise RuntimeError('provider_error_during_episode')
    return audit


class FixedRunner(SelectedRunner):
    def __init__(self, args):
        self.args = args
        self.root = args.root / 'tasks' / args.task
        self.root.mkdir(parents=True, exist_ok=False)
        self.home = args.root / 'homes' / args.task
        self.state = self.home / '.PhyAgentOS'
        self.workspace = self.state / 'workspace'
        self.skill = 'robotwin-rsi-fixed-' + args.task.replace('_', '-')
        self.skill_root = self.state / 'skills' / self.skill
        self.api = f'http://127.0.0.1:{args.gateway_port}'
        self.source_home = BASE / 'parallel10/homes/L02'
        self.policy_process = None
        self.reference = json.loads((args.root / 'screening_reference.json').read_text())[args.task]
        self.description = json.loads((XPOLICY.parent / 'description/task_instruction' / (args.task + '.json')).read_text())['full_description']
        self.env = dict(os.environ, HOME=str(self.home), CUDA_VISIBLE_DEVICES=str(args.simulation_gpu),
            G05_SERVER_URL=f'ws://127.0.0.1:{args.policy_port}')
        self.env['PYTHONPATH'] = str(args.root) + os.pathsep + '/data7/home/lvqinhan/workspace/paos-smoke-tree'
        temporary = self.home / 'tmp'
        temporary.mkdir(parents=True)
        self.env.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
        self.base_skill = (args.root / 'BASE_SKILL.md').read_text().replace('EXPERIMENT_SKILL', self.skill)
        self.carrier = ''
        self.prepare_home()
        self.schedule = pair_schedule(args.task)
        save_json(self.root / 'MANIFEST.json', {**vars(args), 'root': str(args.root), 'model': 'deepseek-flash',
            'arms': ['none', 'fixed'], 'planned_formal_trials': 2 * len(args.repetitions) if getattr(args, 'repetitions', None) else 24, 'development_seed': 3101,
            'schedule': self.schedule, 'online_lesson_update': False, 'profile_name_is_legacy_not_model': PROFILE,
            'reference_scene': self.reference['scene'], 'initial_observation': self.reference['initial_observation'],
            'lesson_injection': 'frozen_task_local_skill_document', 'automatic_evolution_enabled': False,
            'supervision_version': VERSION, 'raw_images_in_main_decision': True,
            'unbounded_release': False, 'full_future_chunk_visible': False, 'eef_supported': False})

    def prepare_home(self):
        source = self.source_home / '.PhyAgentOS/skills/robotwin-rsi-l02'
        self.skill_root.mkdir(parents=True)
        profile_dir = self.skill_root / 'profiles' / PROFILE
        shutil.copytree(source / 'profiles' / PROFILE, profile_dir)
        for name in ['nodes', 'scripts']:
            if (source / name).exists():
                shutil.copytree(source / name, self.skill_root / name, symlinks=True)
        shutil.copytree(self.args.root / 'bridge_src', self.skill_root / 'nodes/vla_bridge/src', dirs_exist_ok=True)
        manifest = yaml.safe_load((source / 'skill.yaml').read_text())
        manifest.update(name=self.skill, gateway_url=self.api, description='DeepSeek candidate-aware pi0.5 Lesson ablation')
        manifest['profiles'] = {PROFILE: manifest['profiles'][PROFILE]}
        manifest['profiles'][PROFILE]['startup_timeout_s'] = 150
        (self.skill_root / 'skill.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False))
        (self.skill_root / 'SKILL.md').write_text(self.base_skill)
        gateway_path = profile_dir / 'gateway.yaml'
        gateway = yaml.safe_load(gateway_path.read_text())
        gateway.update(host='127.0.0.1', port=self.args.gateway_port)
        gateway_path.write_text(yaml.safe_dump(gateway, sort_keys=False))
        flow_path = profile_dir / 'dataflow.yaml'
        flow = yaml.safe_load(flow_path.read_text())
        for node in flow['nodes']:
            node.setdefault('env', {})['HOME'] = str(self.home)
            if node['id'] == 'vla_bridge':
                node['env']['PYTHONPATH'] = str(self.skill_root / 'nodes/vla_bridge/src')
            if node['id'].startswith('image_vision'):
                node['env']['PYTHONPATH'] = str(self.skill_root / 'nodes/image_vision/src')
        flow_path.write_text(yaml.safe_dump(flow, sort_keys=False))
        cache = self.state / 'forge_runtime'
        cache.mkdir()
        for name in ['nodes', 'bundles']:
            (cache / name).symlink_to(self.source_home / '.PhyAgentOS/forge_runtime' / name, target_is_directory=True)
        config = json.loads((self.source_home / '.PhyAgentOS/config.rsi.ds.json').read_text())
        config['agents']['defaults'].update(workspace=str(self.workspace), model='deepseek-flash',
                                           max_tokens=8192, maxToolIterations=600, temperature=0.3)
        config['agents']['evolution']['enabled'] = False
        save_json(self.state / 'config.rsi.ds.json', config)
        (self.state / 'config.rsi.ds.json').chmod(0o600)
        save_json(self.root / 'policy_control.json', {'trial_id': 'bootstrap', 'policy_seed': 3101})

    def launch_policy(self):
        require_free_port(self.args.policy_port)
        env = dict(self.env, CUDA_VISIBLE_DEVICES=str(self.args.policy_gpu), XLA_PYTHON_CLIENT_PREALLOCATE='false')
        with (self.root / 'policy_server.log').open('a') as output:
            self.policy_process = subprocess.Popen([POLICY_PYTHON, str(self.args.root / 'policy_seed_server_gpu_safe.py'),
                '--xpolicy-root', str(XPOLICY), '--port', str(self.args.policy_port),
                '--control', str(self.root / 'policy_control.json'), '--audit', str(self.root / 'policy_audit.jsonl')],
                cwd=XPOLICY, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        save_json(self.root / 'POLICY_PID.json', {'pid': self.policy_process.pid, 'owned_by_this_runner': True})
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            if self.policy_process.poll() is not None:
                raise RuntimeError('own_policy_failed')
            import socket

            try:
                with socket.create_connection(('127.0.0.1', self.args.policy_port), timeout=2):
                    self.event('OWN_POLICY_READY')
                    return
            except OSError:
                time.sleep(3)
        raise RuntimeError('own_policy_start_timeout')

    def prepare_trial(self, trial_dir, arm, seed):
        self.stop_skill()
        if self.workspace.exists():
            archive = self.root / 'workspace_archives'
            archive.mkdir(exist_ok=True)
            self.workspace.rename(archive / trial_dir.name)
        self.workspace.mkdir()
        carrier = self.carrier if arm == 'fixed' else ''
        if arm == 'fixed' and sha256(self.root / 'frozen/lesson.md') != self.carrier_hash:
            raise RuntimeError('frozen_lesson_changed')
        (self.skill_root / 'SKILL.md').write_text(self.base_skill + carrier, encoding='utf-8')
        (trial_dir / 'activated_skill.md').write_text(self.base_skill + carrier, encoding='utf-8')
        save_json(self.root / 'policy_control.json', {'trial_id': trial_dir.name, 'policy_seed': seed})
        profile_dir = self.skill_root / 'profiles' / PROFILE
        benchmark_path = profile_dir / 'benchmark.yaml'
        benchmark = yaml.safe_load(benchmark_path.read_text())
        benchmark.update(record_dir=str(trial_dir / 'videos'), result_dir=str(trial_dir / 'native_results'),
                         task_ids=[self.reference['scene']['task_id']], max_steps=self.args.max_steps)
        benchmark_path.write_text(yaml.safe_dump(benchmark, sort_keys=False))
        for camera in ['head', 'left_wrist', 'right_wrist']:
            path = profile_dir / ('image_vision_' + camera + '.yaml')
            config = yaml.safe_load(path.read_text())
            config['outputs']['frames_dir'] = str(trial_dir / 'frames' / camera)
            config['outputs']['keep_last'] = 512
            path.write_text(yaml.safe_dump(config, sort_keys=False))
        require_free_port(self.args.gateway_port)
        self.command(['paos', 'skill', 'start', self.skill, '--profile', PROFILE], trial_dir / 'lifecycle.log', 210)
        runtime = json.loads((self.state / 'run/skills' / (self.skill + '.json')).read_text())
        if runtime.get('status') != 'running' or runtime.get('gateway_url') != self.api:
            raise RuntimeError('runtime_not_ready')
        tools = request_json(self.api, '/tools')['data']['tools']
        capabilities = query_tool(self.api, tools, 'robotwin.benchmark.describe')
        entry = resolve_task(capabilities['tasks'], self.args.task, self.description)
        policy = query_tool(self.api, tools, 'robotwin.policy.describe')
        if policy.get('ready') is not True or policy.get('action_dim') != 14:
            raise RuntimeError('policy_not_ready')
        bridge = query_tool(self.api, tools, 'vla.get_status')
        if 'candidate' not in bridge:
            raise RuntimeError('running_bridge_is_not_candidate_aware')
        save_json(trial_dir / 'READY.json', {'runtime': runtime, 'task_entry': entry, 'policy': policy,
            'bridge': bridge, 'bridge_source_sha256': sha256(self.skill_root / 'nodes/vla_bridge/src/vla_bridge/service.py')})
        return entry['task_id'], carrier

    def prompt(self, task_id):
        return f'''Use only activate_skill and PAOS Forge tools to run exactly ONE supervised RoboTwin episode.
Activate skill {self.skill} completely. Model is DeepSeek; legacy profile gpt6_pi05 does NOT mean use GPT.
Task name: {self.args.task}. Match benchmark.describe language exactly after whitespace/case normalization to:
{self.description}
Benchmark arguments must be exactly: {json.dumps({'suite': 'robotwin', 'task_ids': [task_id], 'init_state_ids': [0], 'num_runs': 1, 'seed': 0, 'max_steps': self.args.max_steps})}.
task_ids is a LIST; singular task_id inside benchmark arguments is invalid. Runtime has resolved ID {task_id}.
Create a Forge task using verification mode="audit", goal="Complete one supervised episode and report the official result truthfully",
success_criteria=["Official benchmark result retrieved"], evidence_policy={{"required_kinds":["benchmark_result"]}}.
Never use verification mode=enforce. A failed robot episode is still a completed experiment; do not retry it.
Only use any ACTIVE EXPERIENCE CARRIER actually returned by activate_skill. If absent, use current observations only.
Call policy.describe, benchmark.describe, set_mode(stop), start benchmark.run Session and keep its handle.
At every open C0/C1/C2 checkpoint first get_status and wait for candidate.freshness=pending before judging the NEXT action.
At C0 this trial has an EMPTY NEW frame directory, so get the current head frame after episode start; do not reuse previous files.
For camera queries always pass max_age_ms=600000: simulator is frozen at checkpoints so wall-clock age is not simulation age.
At EACH checkpoint query the head frame AND the relevant wrist frame via vision.get_frame tools.
The controller attaches the original images to your MAIN multimodal request together with the previous reviewed images.
Inspect the images directly; do not call image(mode=vision) or infer images from filenames or qpos.
If frame retrieval fails twice, report infrastructure error; do not continue pretending you saw the scene.
Compare task stage, image, current qpos, candidate.dominant_joints and gripper_targets; explain the evidence briefly.
Separate two questions: what actually happened in the LAST executed segment, and what the NEXT candidate is trying to do.
Track task_progress={{"verified_completed":[],"currently_attempting":"visible current stage","remaining":[]}}.
Retract completed subgoals if later visual evidence contradicts them. A closed gripper command is not proof of a grasp.
Every vla.decide MUST include reason and assessment with checkpoint_id, observed_step, task_progress, current_subgoal,
execution_status, execution_evidence, expected_next_intent, predicted_next_intent, intent_status, intent_evidence,
and lesson_applicability (specific applicable clause and observed evidence, or none).
execution_status is not_started ONLY at step 0, otherwise progressing/failed/uncertain/recovered.
intent_status is aligned/misaligned/uncertain. edit requires execution_status=failed OR intent_status=misaligned.
Uncertainty alone is not a takeover reason. A failure need not cause edit if the student is visibly retrying appropriately.
Only a SINGLE pending action is visible; never pretend to have seen a complete future chunk or FK trajectory.
For sensible motion decide(student,steps=10). For a visually supported correctable mistake pass mode="edit", steps=5,
edit={{"delta_joints":{{"fl_joint1":0.03}},"left_gripper":"keep","right_gripper":"keep"}} as a SCHEMA EXAMPLE ONLY.
Choose joint signs from actual evidence; delta_joints is an OBJECT keyed fl_joint1..6 / fr_joint1..6, values -0.1..0.1 rad.
All deltas and gripper changes belong under edit, never top level. assessment is a mandatory object as specified above.
Follow the actual tool schema. Bridge clamps edits to its existing segment and episode limits; inspect accepted/applied results.
Never force an edit just to demonstrate Lesson use. No shell, web searches, reading credentials, extra episodes or writing new Lessons.
Never use set_mode(student): unbounded release is disabled. Continue bounded decisions throughout the episode.
After each segment query fresh status, fetch current head/wrist frames and assess again until native termination.
At a stall use task stage and visible evidence to choose a bounded correction, not motion magnitude alone.
There is no 16-decision soft limit. Do not stop early or invent success; use the official native horizon.
Use concise reasoning and avoid forge_tool_context. Poll at sensible intervals; do not busy-loop.
Retrieve official Session result JSON and report success_rate/num_steps/result_file, failures truthfully. Only that result defines success.
All groups follow these same rules. Historical advice is conditional, not proven causal; note the applicable clause or none at decisions.
'''

    def run_episode(self, repetition, arm, seed, development=False):
        label = 'development' if development else f'r{repetition:02d}_{arm}'
        parent = self.root / ('development' if development else 'trials')
        parent.mkdir(exist_ok=True)
        last_error = None
        for attempt in (1, 2):
            trial_dir = parent / f'{label}_a{attempt}'
            trial_dir.mkdir()
            save_json(self.root / 'CURRENT.json', {'trial': trial_dir.name, 'arm': arm,
                'policy_seed': seed, 'stage': 'preparing', 'started_epoch': time.time()})
            self.event(f'TRIAL_START {trial_dir.name} seed={seed}')
            try:
                task_id, carrier = self.prepare_trial(trial_dir, arm, seed)
                request_path = trial_dir / 'ACTUAL_REQUEST_AUDIT.jsonl'
                self.env['EXPERIMENT_REQUEST_AUDIT'] = str(request_path)
                self.env.update(EXPERIMENT_TASK_ID=str(task_id), EXPERIMENT_MAX_STEPS=str(self.args.max_steps),
                    EXPERIMENT_TOOL_AUDIT=str(trial_dir / 'TOOL_AUDIT.jsonl'),
                    EXPERIMENT_SESSION_AUDIT=str(trial_dir / 'session.jsonl'),
                    EXPERIMENT_FRAMES_ROOT=str(trial_dir / 'frames'),
                    EXPERIMENT_GATE_AUDIT=str(trial_dir / 'GATE_AUDIT.jsonl'))
                prompt = self.prompt(task_id)
                (trial_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')
                session = 'fixed4-' + self.args.task + '-' + trial_dir.name
                save_json(self.root / 'CURRENT.json', {'trial': trial_dir.name, 'arm': arm,
                    'policy_seed': seed, 'stage': 'agent_episode', 'started_epoch': time.time()})
                self.command([PYTHON, str(self.args.root / 'audited_paos.py'), 'agent', '--session', session,
                    '-c', str(self.state / 'config.rsi.ds.json'), '--no-markdown', '-m', prompt], trial_dir / 'agent.log', 7200)
                result_paths = list((trial_dir / 'native_results').glob('robotwin_*.json'))
                current = (self.state / 'forge_runtime/environments' / self.skill / PROFILE / 'current').resolve()
                result_paths.extend((current / 'launch/profiles' / PROFILE / 'results').glob('robotwin_*.json'))
                result_paths = list({path.resolve() for path in result_paths})
                if len(result_paths) != 1:
                    raise RuntimeError('expected_exactly_one_native_result')
                raw = json.loads(result_paths[0].read_text())
                shutil.copy2(result_paths[0], trial_dir / 'benchmark.json')
                validate_result({'status': 'succeeded', 'outputs': raw}, raw, task_id, self.args.max_steps, self.description)
                ledger = raw['metrics']['scene_seed_ledger']
                actual_scene = {key: ledger[0][key] for key in ('task_id', 'init_state_id', 'scene_seed', 'instruction')}
                if len(ledger) != 1 or actual_scene != self.reference['scene']:
                    raise RuntimeError('scene_or_instruction_drift')
                session_path = self.workspace / 'sessions' / (session + '.jsonl')
                if session_path.is_file():
                    shutil.copy2(session_path, trial_dir / 'PAOS_SESSION.jsonl')
                events = [json.loads(line) for line in (self.root / 'policy_audit.jsonl').read_text().splitlines()]
                events = [event for event in events if event.get('trial_id') == trial_dir.name]
                if not any(event['event'] == 'rng_reset' and event['policy_seed'] == seed for event in events):
                    raise RuntimeError('missing_actual_rng_reset')
                identity = initial_identity(events)
                if identity != self.reference['initial_observation']:
                    raise RuntimeError('initial_observation_differs_from_screening')
                save_json(trial_dir / 'POLICY_AUDIT.json', events)
                evidence = process_evidence(trial_dir / 'session.jsonl', request_path, trial_dir / 'frames', carrier)
                gates = [json.loads(line) for line in (trial_dir / 'GATE_AUDIT.jsonl').read_text().splitlines() if line.strip()]
                if not gates or len(gates) != sum(decision['accepted'] is True for decision in evidence['decisions']):
                    raise RuntimeError('missing_accepted_structured_gate_evidence')
                actual_requests = [json.loads(line) for line in request_path.read_text().splitlines() if line.strip()]
                bindings = {row['supervision_binding']['binding'] for row in actual_requests
                            if row['event'] == 'request' and row.get('tools') and row.get('supervision_binding')}
                if any(gate['binding'] not in bindings for gate in gates):
                    raise RuntimeError('gate_evidence_missing_from_actual_multimodal_request')
                evidence.update(supervision_version=VERSION, accepted_gate_count=len(gates))
                save_json(trial_dir / 'SESSION_AUDIT.json', evidence)
                self.stop_skill()
                summary = {'classification': 'valid', 'task': self.args.task, 'arm': arm, 'development': development,
                    'repetition': repetition, 'attempt': attempt, 'policy_seed': seed, 'success_rate': raw['success_rate'],
                    'steps': raw['num_steps'], 'scene': actual_scene, 'initial_observation': identity,
                    'lesson_sha256': self.carrier_hash if arm == 'fixed' else None,
                    'decision_count': evidence['decision_count'], 'accepted_edits': evidence['accepted_edit_decisions'],
                    'applied_edit_actions': evidence['final_counters'].get('edit_actions_biased'),
                    'actual_image_requests': evidence['actual_image_request_count']}
                save_json(trial_dir / 'SUMMARY.json', summary)
                self.event(f'TRIAL_DONE {trial_dir.name} success={raw["success_rate"]} steps={raw["num_steps"]}')
                return trial_dir
            except Exception as error:
                last_error = error
                save_json(trial_dir / 'INVALID.json', {'classification': 'invalid', 'reason': str(error),
                    'seed': seed, 'arm': arm, 'attempt': attempt, 'repetition': repetition})
                self.event(f'TRIAL_INVALID {trial_dir.name} reason={error}')
                self.stop_skill()
                if self.policy_process.poll() is not None:
                    break
        raise RuntimeError(f'two_attempts_failed: {last_error}')

    async def reflect(self, evidence):
        from openai import AsyncOpenAI
        import httpx

        config = json.loads((self.state / 'config.rsi.ds.json').read_text())['providers']['custom']
        client = AsyncOpenAI(api_key=config.get('apiKey', config.get('api_key')),
            base_url=config.get('apiBase', config.get('api_base')), max_retries=1,
            http_client=httpx.AsyncClient(trust_env=False, timeout=180))
        messages = [{'role': 'system', 'content': 'Summarize task-local robot experience as one JSON object. '
            'Use only supplied evidence, distinguish observed facts from untested recovery hypotheses. '
            'No invented object geometry, causal claims or joint signs. Include task_steps, failure_step_or_unknown, '
            'observed_evidence, conditional_recovery, applicability, uncertainty. Under 900 Chinese characters.'},
            {'role': 'user', 'content': json.dumps(evidence, ensure_ascii=False)}]
        save_json(self.root / 'frozen/REFLECTION_INPUT.json', {'model': 'deepseek-flash', 'messages': messages})
        try:
            last_error = None
            for attempt, token_budget in enumerate((8192, 16384), 1):
                response = await client.chat.completions.create(model='deepseek-flash', messages=messages,
                    max_tokens=token_budget, temperature=0.3, response_format={'type': 'json_object'})
                choice = response.choices[0]
                save_json(self.root / f'frozen/REFLECTION_RAW_a{attempt}.json', {
                    'returned_model': response.model, 'finish_reason': choice.finish_reason,
                    'usage': response.usage.model_dump() if response.usage else None,
                    'content': choice.message.content,
                    'reasoning_chars': len(getattr(choice.message, 'reasoning_content', None) or '')})
                try:
                    report = reflection_object(choice.message.content, choice.finish_reason)
                except ValueError as error:
                    last_error = str(error)
                    continue
                save_json(self.root / 'frozen/REFLECTION_RESPONSE.json', {'returned_model': response.model,
                    'attempt': attempt, 'reflection': report})
                return report
            raise RuntimeError('reflection_failed_after_bounded_retries: ' + str(last_error))
        finally:
            await client.close()

    def freeze_lesson(self, source):
        frozen = self.root / 'frozen'
        frozen.mkdir()
        summary = json.loads((source / 'SUMMARY.json').read_text())
        audit = json.loads((source / 'SESSION_AUDIT.json').read_text())
        records, calls = records_and_calls(source / 'session.jsonl')
        image_analyses = [str(call.get('output'))[:1400] for call in calls if call['name'] == 'image']
        evidence = {'task': self.args.task, 'goal': self.reference['scene']['instruction'],
            'development_result': summary, 'decisions': audit['decisions'],
            'visual_analyses': image_analyses, 'final_counters': audit['final_counters'],
            'caveat': 'Visual analysis and reasoning can be wrong. No privileged expert trajectory is supplied.'}
        if (source / 'GATE_AUDIT.jsonl').is_file():
            evidence['structured_outcome_intent'] = [json.loads(line) for line in
                (source / 'GATE_AUDIT.jsonl').read_text().splitlines() if line.strip()]
        report = asyncio.run(self.reflect(evidence))
        provenance = {'task': self.args.task, 'development_only': True, 'policy_seed': 3101,
            'source_dir': str(source), 'source_session_sha256': sha256(source / 'session.jsonl'),
            'source_result_sha256': sha256(source / 'benchmark.json'), 'source_success_rate': summary['success_rate']}
        self.carrier = '\n\n## ACTIVE EXPERIENCE CARRIER: FROZEN TASK LOCAL LESSON\n' + json.dumps({
            'provenance': provenance, 'reflection': report,
            'use_rule': 'Only apply when current image and candidate support the condition. Recovery advice is untested.'},
            ensure_ascii=False, indent=2) + '\n'
        if len(self.carrier) > 6500:
            raise RuntimeError('lesson_exceeds_frozen_context_budget')
        (frozen / 'lesson.md').write_text(self.carrier, encoding='utf-8')
        self.carrier_hash = sha256(frozen / 'lesson.md')
        save_json(frozen / 'PROVENANCE.json', {**provenance, 'lesson_sha256': self.carrier_hash,
            'chars': len(self.carrier), 'online_updates': False, 'injection': 'activate_skill full document'})
        self.event('LESSON_FROZEN ' + self.carrier_hash)

    def run(self):
        try:
            self.launch_policy()
            source = self.run_episode(0, 'none', 3101, development=True)
            self.freeze_lesson(source)
            for pair in self.schedule:
                for arm in pair['arms']:
                    self.run_episode(pair['repetition'], arm, pair['seed'])
            save_json(self.root / 'COMPLETE.json', {'valid_formal_trials': 24, 'development_trials': 1})
            self.event('TASK_COMPLETE')
        except BaseException as error:
            save_json(self.root / 'ABORT.json', {'classification': 'infra_or_validation_failure', 'reason': str(error)})
            self.event('TASK_ABORT ' + str(error))
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
                save_json(self.root / 'CLEANUP.json', {'own_policy_exited': self.policy_process is None or
                    self.policy_process.poll() is not None, 'shared_models_untouched': True})


def main():
    def terminate(signum, frame):
        raise KeyboardInterrupt('termination_requested')

    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--task', choices=TASKS, required=True)
    parser.add_argument('--max-steps', type=int, required=True)
    parser.add_argument('--policy-gpu', type=int, required=True)
    parser.add_argument('--simulation-gpu', type=int, required=True)
    parser.add_argument('--gateway-port', type=int, required=True)
    parser.add_argument('--policy-port', type=int, required=True)
    FixedRunner(parser.parse_args()).run()


if __name__ == '__main__':
    main()
