"""Exercise the real RoboTwin adapter through Core; only the native GPU gateway is simulated."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from controller import RoboTwinAdapter
from experiment import Experiment
from PhyAgentOS.forge.benchmark.evidence import checked_report
from PhyAgentOS.forge.benchmark.private import PrivateBenchmarkTransport
from PhyAgentOS.forge.tool_client import ForgeToolClient


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('defect', [None, 'incomplete', 'task-drift', 'native-error'])
async def test_real_adapter_core_native_outcome_and_stop(tmp_path, success, defect):
    task = 'press_stapler'
    description = 'Press the stapler'
    scene = {'task_id': 7, 'init_state_id': 0, 'scene_seed': 123, 'instruction': description}
    args = SimpleNamespace(task=task, max_steps=10, root=tmp_path, mode='smoke')
    runtime = SimpleNamespace(root=tmp_path, home=tmp_path / 'home', args=args,
        reference={'scene': scene, 'initial_observation': None}, description=description,
        event=lambda event: None, stopped=False)
    native_actions = []
    trial = 'smoke_a1'
    image = tmp_path / trial / 'frames' / 'image.png'

    def prepare(directory, arm, seed):
        image.parent.mkdir(parents=True)
        image.write_bytes(b'fake image bytes used only by a CPU model callback')
        events = [{'event': 'rng_reset', 'trial_id': trial, 'policy_seed': seed},
                  {'event': 'first_inference', 'trial_id': trial,
                   'state': 'qpos-digest', 'images': 'image-digests', 'prompt': description}]
        (tmp_path / 'policy_audit.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        return 7, ''

    def stop_executor():
        runtime.stopped = True
        return {'runtime_status': 'stopped', 'remaining': [], 'method': 'test_owned_executor'}

    runtime.prepare_trial, runtime.stop_executor = prepare, stop_executor
    adapter = RoboTwinAdapter(runtime)
    finished = False
    result_file = tmp_path / trial / 'native_result.json'
    raw = {'status': 'succeeded', 'total_episodes': 1, 'completed_episodes': 1,
           'success_rate': int(success), 'num_steps': 10, 'max_steps': 10, 'seed': 0,
           'metrics': {'scene_seed_ledger': [scene]},
           'episodes': [{'task_id': 7, 'init_state_id': 0, 'task_description': description,
                         'termination': 'success' if success else 'failed',
                         'success': success, 'num_steps': 10}]}
    if defect == 'incomplete':
        raw['completed_episodes'] = 0
    elif defect == 'task-drift':
        raw['episodes'][0]['task_id'] = 99
    elif defect == 'native-error':
        raw['episodes'][0]['error_code'] = 'GPU_ERROR'
    tools = [{'tool_id': name, 'semantics': semantics, 'endpoint_id': name, 'operation': 'query'}
             for name, semantics in [('vla.set_mode', 'action'), ('vla.decide', 'action'),
                 ('vla.get_status', 'query'), ('vision.get_frame', 'query'),
                 ('vision.get_frame_left_wrist', 'query'), ('vision.get_frame_right_wrist', 'query')]]

    async def native(route, arguments=None):
        nonlocal finished
        if route == '/tools':
            return {'data': {'tools': tools}}
        if route == '/tools/robotwin.benchmark.run:invoke':
            assert arguments['num_runs'] == 1 and arguments['seed'] == 0
            native_actions.append('start')
            return {'data': {'invocation_id': 'episode'}}
        if route in ['/tools/vla.set_mode:invoke', '/tools/vla.decide:invoke']:
            name = 'decide' if 'decide' in route else 'hold'
            native_actions.append(name)
            if name == 'decide':
                finished = True
                result_file.write_text(json.dumps(raw))
            return {'data': {'invocation_id': name}}
        if route == '/invocations/episode/result':
            return {'data': {'result': {'status': 'succeeded',
                'outputs': {**raw, 'result_file': str(result_file)}} if finished else None}}
        if route.endswith('/result'):
            return {'data': {'result': {'status': 'succeeded', 'outputs': {'accepted': True}}}}
        if route.startswith('/invocations/'):
            return {'data': {'status': 'succeeded'}}
        if route == '/tools/vla.get_status/query:invoke':
            output = {'episode': {'index': 0, 'steps': 0},
                'checkpoint': {'checkpoint_id': 'checkpoint-0'},
                'candidate': {'freshness': 'pending', 'action': [0.0] * 14}}
        elif route.startswith('/tools/vision.'):
            output = {'image_path': str(image), 'frame_seq': 1}
        else:
            raise AssertionError(route)
        return {'data': {'response': {'result': {'status': 'succeeded', 'outputs': output}}}}

    adapter.native = native
    captured = []

    async def decide(context):
        captured.append(context)
        assert context.get('tools') is None and context['episodic_experience'] == []
        assert len(context['media']) == 3
        assert all('data:image/png;' in item['image_url']['url'] for item in context['media'])
        episode = adapter.episodes[adapter.active]
        episode['supervisor'].delivered_binding = episode['supervisor'].binding()
        assessment = {'checkpoint_id': 'checkpoint-0', 'observed_step': 0,
            'task_progress': {'verified_completed': [], 'currently_attempting': 'approach', 'remaining': ['press']},
            'execution_status': 'not_started', 'intent_status': 'aligned'}
        assessment.update({key: 'current evidence' for key in ['current_subgoal', 'execution_evidence',
            'expected_next_intent', 'predicted_next_intent', 'intent_evidence', 'lesson_applicability']})
        return {'kind': 'proposal', 'proposal': {'mode': 'student', 'steps': 10,
            'reason': 'aligned', 'assessment': assessment},
            'usage': {'provider_calls': 2, 'total_tokens': None}}

    experiment = Experiment.__new__(Experiment)
    experiment.args, experiment.runtime, experiment.adapter = args, runtime, adapter
    experiment.decider = decide
    experiment.workspace = tmp_path / 'workspace'
    experiment.transport = PrivateBenchmarkTransport(adapter)
    experiment.client = ForgeToolClient('http://benchmark.private', transport=experiment.transport)
    experiment.supervisor = None
    try:
        await experiment.setup()
        if defect:
            with pytest.raises(RuntimeError, match='smoke_infrastructure_invalid'):
                await experiment.run_root([experiment.case(trial, 'none', 102)], 'smoke')
            report = json.loads((tmp_path / 'smoke_CORE_REPORT.json').read_text())
            assert report['children'][0]['outcome'] == 'infra_error'
            assert runtime.stopped
            assert native_actions == ['hold', 'start', 'decide']
            assert (tmp_path / trial / 'INVALID.json').is_file()
            return
        children = await experiment.run_root([experiment.case(trial, 'none', 102)], 'smoke')
        assert len(children) == 1
        assert children[0]['outcome'] == ('success' if success else 'failure')
        assert children[0]['settled'] and runtime.stopped
        assert native_actions == ['hold', 'start', 'decide']
        assert len(captured) == 1
        task_id = children[0]['task_id']
        assert experiment.coordinator.get_task(task_id).status.value == 'succeeded'
        report = checked_report(experiment.coordinator.benchmark, task_id)
        assert report['rounds'][0]['provider_calls'] == 2
        assert report['rounds'][0]['model_calls'] == 1
        assert report['rounds'][0]['provider_total_tokens'] is None
        assert (tmp_path / trial / 'STOP_PROOF.json').is_file()
    finally:
        if experiment.supervisor:
            await experiment.supervisor.shutdown()
        await experiment.client.close()


def test_recreated_adapter_refuses_existing_owner_before_native_access(tmp_path):
    runtime = SimpleNamespace(root=tmp_path)
    RoboTwinAdapter(runtime)
    with pytest.raises(FileExistsError):
        RoboTwinAdapter(runtime)
