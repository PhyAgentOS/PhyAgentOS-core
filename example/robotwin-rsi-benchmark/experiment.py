import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import yaml

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.experience.store import ExperienceStore
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.benchmark.private import PrivateBenchmarkDeployment, PrivateBenchmarkTransport
from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.forge.interaction.contracts import digest
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.installer import SkillInstaller
from PhyAgentOS.skill_runtime.integration import ActiveRuntimeRegistry, ActiveSkillRuntime, DynamicForgeToolClient
from PhyAgentOS.skill_runtime.state import RuntimeStateStore
from PhyAgentOS.verification.contracts import TaskVerificationContract
from scripts.package_skill import package

from controller import DeepSeekDecider, RoboTwinAdapter, Runtime
from fixed_runner import TASKS, pair_schedule, sha256
from prescreen_runner import save_json


def selected_pairs(task, repetitions=None):
    schedule = pair_schedule(task)
    if repetitions is None:
        return schedule
    if not repetitions or len(repetitions) != len(set(repetitions)) or not set(repetitions) <= set(range(1, 13)):
        raise ValueError('invalid_pair_partition')
    return [pair for pair in schedule if pair['repetition'] in repetitions]


def import_experience(source, destination, task, scene):
    frozen = source / 'frozen'
    experience = json.loads((frozen / 'experience.json').read_text())
    provenance = json.loads((frozen / 'PROVENANCE.json').read_text())
    reference = json.loads((source / 'RUNTIME_INITIAL_REFERENCE.json').read_text())
    if provenance.get('task') != task or not isinstance(provenance.get('source'), str):
        raise RuntimeError('imported_experience_provenance_invalid')
    development = Path(provenance['source'])
    if not development.resolve().is_relative_to(source.resolve()):
        raise RuntimeError('imported_experience_provenance_invalid')
    summary = json.loads((development / 'SUMMARY.json').read_text())
    proof = json.loads((development / 'STOP_PROOF.json').read_text())
    if (experience.get('task_name') != task or provenance.get('task') != task or
            sha256(frozen / 'experience.json') != provenance['experience_sha256'] or
            sha256(development / 'benchmark.json') != experience['source_result_sha256'] or
            summary.get('classification') != 'valid' or not summary.get('development') or
            summary.get('policy_seed') != 3101 or summary.get('scene') != scene or
            reference.get('policy_independent_scene_reference') != scene or
            summary.get('initial_observation') != reference.get('initial_observation') or
            proof.get('runtime_status') != 'stopped' or proof.get('remaining') != [] or
            len(json.dumps(experience, ensure_ascii=False)) > 6500):
        raise RuntimeError('imported_experience_provenance_invalid')
    (destination / 'frozen').mkdir()
    for name in ['experience.json', 'PROVENANCE.json']:
        shutil.copy2(frozen / name, destination / 'frozen' / name)
    shutil.copy2(source / 'RUNTIME_INITIAL_REFERENCE.json', destination / 'RUNTIME_INITIAL_REFERENCE.json')
    save_json(destination / 'IMPORTED_EXPERIENCE.json', {'source_task_root': str(source),
        'experience_sha256': sha256(frozen / 'experience.json'),
        'initial_reference_sha256': sha256(source / 'RUNTIME_INITIAL_REFERENCE.json'),
        'initial_identity_rule': 'Exact imported identity required in each actual formal episode; no new reference',
        'timestamp': time.time()})
    return experience


def proven_prior_trial(source, trial, task, arm, seed):
    candidates = [source / trial, source / (trial.removesuffix('_a1') + '_a2')]
    valid = [directory for directory in candidates if (directory / 'SUMMARY.json').exists()]
    if not valid:
        return None
    if len(valid) != 1:
        raise RuntimeError('duplicate_prior_valid_trial')
    directory = valid[0]
    summary = json.loads((directory / 'SUMMARY.json').read_text())
    proof = json.loads((directory / 'STOP_PROOF.json').read_text())
    if (summary.get('classification') != 'valid' or summary.get('development') or
            summary.get('task') != task or summary.get('arm') != arm or summary.get('policy_seed') != seed or
            proof.get('runtime_status') != 'stopped' or proof.get('remaining') != [] or
            (directory / 'INVALID.json').exists()):
        raise RuntimeError('prior_valid_trial_not_proven')
    return directory


def proposal_schema():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1000}
    strings = {'type': 'array', 'items': text, 'maxItems': 20}
    progress = {'type': 'object', 'properties': {'verified_completed': strings,
        'currently_attempting': text, 'remaining': strings},
        'required': ['verified_completed', 'currently_attempting', 'remaining'], 'additionalProperties': False}
    assessment_properties = {name: text for name in ('checkpoint_id', 'current_subgoal', 'execution_evidence',
        'expected_next_intent', 'predicted_next_intent', 'intent_evidence', 'lesson_applicability')}
    assessment_properties.update(observed_step={'type': 'integer', 'minimum': 0}, task_progress=progress,
        execution_status={'enum': ['not_started', 'progressing', 'failed', 'uncertain', 'recovered']},
        intent_status={'enum': ['aligned', 'misaligned', 'uncertain']})
    joint_properties = {f'{side}_joint{number}': {'type': 'number', 'minimum': -0.1, 'maximum': 0.1}
        for side in ['fl', 'fr'] for number in range(1, 7)}
    return {'type': 'object', 'properties': {'mode': {'enum': ['student', 'edit']},
        'steps': {'type': 'integer', 'minimum': 1, 'maximum': 10}, 'reason': text,
        'assessment': {'type': 'object', 'properties': assessment_properties,
            'required': list(assessment_properties), 'additionalProperties': False},
        'edit': {'type': 'object', 'properties': {
            'delta_joints': {'type': 'object', 'properties': joint_properties, 'additionalProperties': False},
            'left_gripper': {'enum': ['keep', 'open', 'closed']},
            'right_gripper': {'enum': ['keep', 'open', 'closed']}}, 'additionalProperties': False}},
        'required': ['mode', 'steps', 'reason', 'assessment'], 'additionalProperties': False,
        'allOf': [{'if': {'properties': {'mode': {'const': 'edit'}}}, 'then': {'required': ['edit']}}]}


class Experiment:
    def __init__(self, args):
        self.args = args
        self.runtime = Runtime(args)
        self.adapter = RoboTwinAdapter(self.runtime)
        self.decider = DeepSeekDecider(self.runtime, self.adapter)
        self.workspace = self.runtime.root / 'core_workspace'
        self.transport = PrivateBenchmarkTransport(self.adapter)
        self.client = ForgeToolClient('http://benchmark.private', transport=self.transport, timeout_s=300)
        self.supervisor = None

    def launch_policy(self):
        minimum = int(os.environ.get('RSI_POLICY_MIN_FREE_MIB', '0'))
        if minimum:
            free = int(subprocess.check_output(['nvidia-smi', '-i', str(self.args.policy_gpu),
                '--query-gpu=memory.free', '--format=csv,noheader,nounits'], text=True).strip())
            if free < minimum:
                raise RuntimeError(f'insufficient_gpu_headroom_before_policy:{free}<{minimum}')
        self.runtime.launch_policy()

    async def setup(self):
        root = self.runtime.root / 'control_source' / 'robotwin-benchmark-control'
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text('---\nname: robotwin-benchmark-control\ndescription: Private RoboTwin experiment\n---\n'
            'Immutable experiment control bundle. Physical runtime is host-owned; memory is external and task-scoped.')
        (root / 'flow.yaml').write_text('nodes: []\n')
        (root / 'skill.yaml').write_text(yaml.safe_dump({'manifest_version': 2,
            'name': 'robotwin-benchmark-control', 'version': '1.0.0', 'description': 'Private RoboTwin experiment',
            'skill_document': 'SKILL.md', 'gateway_url': 'http://benchmark.private',
            'required_tools': ['benchmark.run', 'benchmark.snapshot', 'benchmark.submit'],
            'profiles': {'local': {'dataflow': 'flow.yaml'}}}))
        (root / 'benchmark.yaml').write_text(yaml.safe_dump({'protocol': 'benchmark_contract_v1',
            'session_tool': 'benchmark.run', 'snapshot_tool': 'benchmark.snapshot',
            'submission_tool': 'benchmark.submit', 'proposal_schema_file': 'proposal.json',
            'snapshot_views': ['root', 'episode', 'receipt', 'history'], 'access_policy': 'private_control_plane'}))
        save_json(root / 'proposal.json', proposal_schema())
        installed = self.runtime.root / 'control_skills'
        SkillInstaller(installed, state_store=RuntimeStateStore(self.runtime.root / 'control_runtime')).install(
            package(root, self.runtime.root / 'control_archives'))
        runtime = ActiveSkillRuntime('robotwin-benchmark-control', '1.0.0', 'local',
            'private_' + self.args.task, 'http://benchmark.private', 'host_' + self.args.task,
            self.client, set(), set(), set())
        registry = ActiveRuntimeRegistry(runtime)
        resolver = ForgeSkillBindingResolver(registry, catalog=SkillCatalog(installed))
        self.activation = SkillActivationManager(workspace=self.workspace,
            store=ExperienceStore(self.workspace), runtime_availability_provider=lambda name: True,
            binding_resolver=resolver, installed_skills_dir=installed)
        if self.activation.skills.resolve_skill('robotwin-benchmark-control') is None:
            raise RuntimeError('private_control_skill_not_registered')
        self.coordinator = AgentTaskCoordinator(workspace=self.workspace, config=ForgeConfig(benchmark={'enabled': True}),
            client=DynamicForgeToolClient(registry), binding_resolver=resolver, activation_manager=self.activation,
            runtime_invocation_ids=runtime.invocation_ids, runtime_session_ids=runtime.session_ids,
            runtime_task_binding_ids=runtime.task_binding_ids)

    async def run_root(self, cases, phase, experiences=None, attempt=1):
        session = 'benchmark:' + self.args.task + ':' + phase
        self.activation.begin_turn(session, phase)
        activated, _, _ = await self.activation.activate(session_key=session, name='robotwin-benchmark-control', role='primary')
        task = await self.coordinator.create_task(task_description='RoboTwin ' + phase,
            activation_id=activated.activation_id, origin_session_key=session,
            benchmark_plan={'cases': cases, 'rounds': 1, 'parallelism': 1,
                'max_decisions': self.args.max_steps + 20,
                'episode_timeout_s': 7200, 'model_timeout_s': 600},
            verification=TaskVerificationContract(mode='enforce', goal='Complete evaluation with authoritative native results',
                success_criteria=['evaluation_complete'], evidence_policy={
                    'profile': 'benchmark_evaluation', 'required_kinds': ['benchmark_report']}))
        if self.supervisor:
            await self.supervisor.shutdown()
        self.coordinator.benchmark.closing = False
        self.supervisor = self.coordinator.benchmark.enable_private_adapter(PrivateBenchmarkDeployment(
            transport=self.transport, workers=[self.args.task], decide=self.decider,
            artifacts=self.adapter.artifacts, experiences=experiences))
        save_json(self.runtime.root / 'CURRENT.json', {'phase': phase, 'task_id': task.task_id,
            'episodes': len(cases), 'started_at': time.time()})
        await self.coordinator.start_session(task.task_id, 'benchmark.run', {})
        await self.supervisor.monitors[task.task_id]
        store = self.coordinator.benchmark.store
        run = store.get('run', task.task_id)
        children = store.list('subtask', task.task_id)
        save_json(self.runtime.root / (phase + '_CORE_REPORT.json'), {'run': run, 'children': children,
            'agent_task': self.coordinator.get_task(task.task_id).model_dump(mode='json')})
        if not run['settled'] or any(not child['settled'] for child in children):
            raise RuntimeError('core_root_not_valid:' + json.dumps({'state': run['state'], 'reason': run.get('reason'),
                'outcomes': [{'outcome': child['outcome'], 'reason': child.get('reason')} for child in children]}))
        invalid = [child for child in children if child['outcome'] not in {'success', 'failure'}]
        if invalid:
            if attempt == 2 or any(child['case']['environment'].get('infra_attempt', 1) >= 2 for child in invalid):
                raise RuntimeError('infrastructure_retry_limit_reached')
            retry_cases = []
            for child in invalid:
                case = json.loads(json.dumps(child['case']))
                case['case_key'] = case['case_key'].removesuffix('_a1') + '_a2'
                case['environment']['trial'] = case['case_key']
                case['environment']['infra_attempt'] = 2
                retry_cases.append(case)
            children += await self.run_root(retry_cases, phase + '_infra_retry', experiences, attempt=2)
        return children

    def valid_directory(self, trial):
        first = self.runtime.root / trial
        return first if (first / 'SUMMARY.json').exists() else self.runtime.root / (trial.removesuffix('_a1') + '_a2')

    def case(self, trial, arm, seed, repetition=0, development=False):
        return {'case_key': trial, 'environment': {'task_name': self.args.task, 'trial': trial,
            'task_instruction': self.runtime.reference['scene']['instruction'],
            'arm': arm, 'seed': seed, 'repetition': repetition, 'development': development}}

    async def freeze(self, source):
        frozen = self.runtime.root / 'frozen'
        frozen.mkdir()
        gates = [json.loads(line) for line in (source / 'GATE_AUDIT.jsonl').read_text().splitlines()]
        selected = [gates[0], *[gate for gate in gates if gate['proposal']['mode'] == 'edit'][:3],
            *gates[len(gates)//2:len(gates)//2+1], *gates[-3:]]
        evidence = {'task': self.args.task, 'goal': self.runtime.reference['scene']['instruction'],
            'development_result': json.loads((source / 'SUMMARY.json').read_text()),
            'selected_decisions': selected[:8], 'caveat': 'Assessments are model judgments, not ground truth.'}
        reflection = await self.runtime.reflect(evidence)
        experience = {'kind': 'frozen_task_local_episodic_experience', 'task_name': self.args.task,
            'source_result_sha256': sha256(source / 'benchmark.json'), 'source_seed': 3101,
            'reflection': reflection, 'not_generalized_lesson': True,
            'use_rule': 'Conditional hypotheses; current images/candidate must support applicability.'}
        if len(json.dumps(experience, ensure_ascii=False)) > 6500:
            raise RuntimeError('frozen_experience_over_budget')
        save_json(frozen / 'experience.json', experience)
        save_json(frozen / 'PROVENANCE.json', {'task': self.args.task,
            'source': str(source), 'experience_sha256': sha256(frozen / 'experience.json'),
            'source_result_sha256': experience['source_result_sha256'], 'immutable': True,
            'injection': 'PAOS EpisodeRunner task-scoped episodic_experience; not SKILL mutation'})
        self.runtime.event('EXPERIENCE_FROZEN ' + digest(experience))
        return experience

    async def run(self):
        await self.setup()
        imported = getattr(self.args, 'import_task_root', None)
        if imported:
            source = Path(imported)
            self.runtime.event('WAITING_FOR_VERIFIED_FROZEN_EXPERIENCE')
            deadline = time.monotonic() + 10800
            while not all((source / name).exists() for name in ['frozen/experience.json',
                    'frozen/PROVENANCE.json', 'RUNTIME_INITIAL_REFERENCE.json']):
                if (source / 'ABORT.json').exists() or time.monotonic() > deadline:
                    raise RuntimeError('experience_source_failed_or_timed_out')
                await asyncio.sleep(5)
            experience = import_experience(source, self.runtime.root, self.args.task, self.runtime.reference['scene'])
            await asyncio.to_thread(self.launch_policy)
        else:
            await asyncio.to_thread(self.launch_policy)
            await self.run_root([self.case('development_a1', 'none', 3101, development=True)], 'development')
            experience = await self.freeze(self.valid_directory('development_a1'))
        experiences = {self.args.task: experience}
        pairs = selected_pairs(self.args.task, getattr(self.args, 'repetitions', None))
        cases = [self.case(f'r{pair["repetition"]:02d}_{arm}_a1', arm, pair['seed'], pair['repetition'])
            for pair in pairs for arm in pair['arms']]
        planned_cases = list(cases)
        resume = getattr(self.args, 'resume_task_root', None)
        prior_trials = {}
        if resume:
            source = Path(resume)
            cleanup = json.loads((source / 'CLEANUP.json').read_text())
            if not cleanup['own_policy_exited'] or cleanup['skill_stop_error'] is not None:
                raise RuntimeError('prior_worker_cleanup_not_proven')
            pending = []
            for case in cases:
                environment = case['environment']
                prior = proven_prior_trial(source, case['case_key'], self.args.task, environment['arm'], environment['seed'])
                if prior:
                    prior_trials[case['case_key']] = prior
                    continue
                old_trial = source / case['case_key']
                if old_trial.exists():
                    proof = json.loads((old_trial / 'STOP_PROOF.json').read_text())
                    if proof.get('runtime_status') != 'stopped' or proof.get('remaining') != []:
                        raise RuntimeError('prior_invalid_trial_shutdown_not_proven')
                    if (source / (case['case_key'].removesuffix('_a1') + '_a2')).exists():
                        raise RuntimeError('prior_infrastructure_retry_budget_exhausted')
                    case['case_key'] = case['case_key'].removesuffix('_a1') + '_a2'
                    case['environment']['trial'] = case['case_key']
                    case['environment']['infra_attempt'] = 2
                pending.append(case)
            cases = pending
            save_json(self.runtime.root / 'RESUME_ASSIGNMENT.json', {'source': str(source),
                'retained_valid_trials': sorted(prior_trials), 'pending_trials': [case['case_key'] for case in cases],
                'partial_pairs_remain_on_original_host': True, 'timestamp': time.time()})
        new_formal_count = len(cases)
        gate = self.args.root / 'PILOT_PASSED.json'
        if self.args.task == self.args.pilot_task or imported:
            pilot_repetition = cases[0]['environment']['repetition'] if cases else pairs[0]['repetition']
            pilot_cases = [case for case in cases if case['environment']['repetition'] == pilot_repetition]
            if pilot_cases:
                await self.run_root(pilot_cases, 'pilot_pair', experiences)
            audited = []
            for case in [case for case in planned_cases if case['environment']['repetition'] == pilot_repetition]:
                original_key = f'r{pilot_repetition:02d}_{case["environment"]["arm"]}_a1'
                directory = prior_trials.get(original_key) or self.valid_directory(original_key)
                requests = [json.loads(line) for line in (directory / 'REQUEST_AUDIT.jsonl').read_text().splitlines()
                    if json.loads(line)['event'] == 'request']
                expected = [experience] if case['environment']['arm'] == 'fixed' else []
                if not requests or any(record['context']['episodic_experience'] != expected for record in requests):
                    raise RuntimeError('pilot_experience_exposure_mismatch')
                audited.append({'trial': directory.name, 'arm': case['environment']['arm'], 'actual_requests': len(requests),
                    'experience_digest': digest(expected)})
            save_json(gate, {'passed': True, 'timestamp': time.time(), 'task': self.args.task,
                'checks': ['native_result', 'scoped_core_trajectory', 'persisted_reflection',
                    'fresh_session_frozen_exposure', 'none_arm_clean', 'identical_initial_observation', 'owned_skill_stop'],
                'audited': audited})
            self.runtime.event('PILOT_PASSED')
            cases = [case for case in cases if case['environment']['repetition'] != pilot_repetition]
        else:
            self.runtime.event('WAITING_FOR_PILOT')
            deadline = time.monotonic() + 10800
            while not gate.exists():
                if (self.args.root / 'tasks' / self.args.pilot_task / 'ABORT.json').exists() or time.monotonic() > deadline:
                    raise RuntimeError('pilot_failed_or_timed_out')
                await asyncio.sleep(5)
        if cases:
            await self.run_root(cases, 'formal', experiences)
        save_json(self.runtime.root / 'COMPLETE.json', {'valid_formal_trials': new_formal_count,
            'retained_prior_formal_trials': len(prior_trials),
            'development_trials': 0 if imported else 1, 'repetitions': [pair['repetition'] for pair in pairs],
            'frozen_experience_sha256': sha256(self.runtime.root / 'frozen/experience.json')})


async def main(args):
    experiment = None
    try:
        experiment = Experiment(args)
        await experiment.run()
    except BaseException as error:
        root = args.root / 'tasks' / args.task
        root.mkdir(exist_ok=True, parents=True)
        save_json(root / 'ABORT.json', {'classification': 'infra_or_validation_failure', 'reason': str(error),
            'timestamp': time.time()})
        raise
    finally:
        if experiment:
            try:
                if experiment.supervisor:
                    await experiment.supervisor.shutdown()
            finally:
                await experiment.decider.client.close()
                await experiment.client.close()
                await asyncio.to_thread(experiment.runtime.close)


if __name__ == '__main__':
    def terminate(signum, frame):
        raise KeyboardInterrupt('termination_requested')
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--pilot-task', choices=TASKS, default='turn_switch')
    parser.add_argument('--task', choices=TASKS, required=True)
    parser.add_argument('--max-steps', type=int, required=True)
    parser.add_argument('--policy-gpu', type=int, required=True)
    parser.add_argument('--simulation-gpu', type=int, required=True)
    parser.add_argument('--gateway-port', type=int, required=True)
    parser.add_argument('--policy-port', type=int, required=True)
    parser.add_argument('--repetitions', type=int, nargs='+')
    parser.add_argument('--import-task-root')
    parser.add_argument('--resume-task-root')
    asyncio.run(main(parser.parse_args()))
