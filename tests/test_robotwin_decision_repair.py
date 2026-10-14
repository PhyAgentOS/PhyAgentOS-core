import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest
from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from controller import DeepSeekDecider, RoboTwinAdapter, Runtime, validate_native_decision
from experiment import proposal_schema
from response_validation import decision_object
from supervision import Supervisor
from PhyAgentOS.forge.benchmark.client import SCOPE_KEYS
from PhyAgentOS.forge.interaction.contracts import digest


def context_and_decision():
    assessment = {'checkpoint_id': 'ckpt-001', 'observed_step': 0,
        'task_progress': {'verified_completed': [], 'currently_attempting': 'approach', 'remaining': ['press']},
        'current_subgoal': 'approach', 'execution_status': 'not_started', 'execution_evidence': 'initial image',
        'expected_next_intent': 'approach', 'predicted_next_intent': 'approach', 'intent_status': 'aligned',
        'intent_evidence': 'small candidate motion', 'lesson_applicability': 'none'}
    decision = {'kind': 'proposal', 'proposal': {'mode': 'student', 'steps': 10,
        'reason': 'current images support approach', 'assessment': assessment}}
    packet = {'checkpoint': {'checkpoint_id': 'ckpt-001'}, 'episode': {'steps': 0}}
    context = {'goal': {}, 'observation': {'packet': packet, 'artifacts': [{'artifact_id': 'head', 'sha256': 'hash'}]},
        'decision_schema': {'oneOf': [{'type': 'object', 'properties': {'kind': {'const': 'proposal'},
            'proposal': proposal_schema()}, 'required': ['kind', 'proposal'], 'additionalProperties': False}]},
        'episodic_experience': [], 'episodic_experience_digest': digest([]),
        'media': [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAAA'}}]}
    return context, decision


@pytest.mark.parametrize('content', [
    '{"kind":"proposal","kind":null}', '{"kind":"proposal","number":NaN}',
    '{"kind":"proposal","number":Infinity}'])
def test_invalid_json_authority_never_silently_normalized(content):
    context, _ = context_and_decision()
    with pytest.raises(ValueError):
        decision_object(content, 'stop', context['decision_schema'])


def test_missing_judgment_and_extra_fields_still_rejected():
    context, decision = context_and_decision()
    del decision['proposal']['assessment']['intent_status']
    with pytest.raises(ValidationError, match='intent_status'):
        decision_object(json.dumps(decision), 'stop', context['decision_schema'])
    context, decision = context_and_decision()
    decision['proposal']['assessment']['intent_evidence_note'] = 'extra'
    with pytest.raises(ValidationError, match='intent_evidence_note'):
        decision_object(json.dumps(decision), 'stop', context['decision_schema'])


def test_truncated_response_not_executed_even_if_json_is_parseable():
    context, decision = context_and_decision()
    with pytest.raises(ValueError, match='finish_reason'):
        decision_object(json.dumps(decision), 'length', context['decision_schema'])


def decider_fixture(tmp_path, outputs):
    context, valid = context_and_decision()
    supervisor = Supervisor(tmp_path)
    supervisor.observe_status({'checkpoint': {'checkpoint_id': 'ckpt-001'}, 'episode': {'steps': 0},
        'candidate': {'freshness': 'pending'}})
    episode = {'directory': tmp_path, 'supervisor': supervisor}
    calls = []
    async def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        content = outputs[min(len(calls) - 1, len(outputs) - 1)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')],
            usage=SimpleNamespace(model_dump=lambda: {'total_tokens': 100}), model='deepseek-flash')
    decider = DeepSeekDecider.__new__(DeepSeekDecider)
    decider.model = 'test-multimodal'
    decider.adapter = SimpleNamespace(active='one', episodes={'one': episode})
    decider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return context, valid, supervisor, calls, decider


@pytest.mark.asyncio
async def test_repair_receives_precise_error_and_same_images_without_fabricated_judgment(tmp_path):
    context, valid = context_and_decision()
    invalid = copy.deepcopy(valid)
    del invalid['proposal']['assessment']['intent_status']
    context, valid, supervisor, calls, decider = decider_fixture(tmp_path, [json.dumps(invalid), json.dumps(valid)])
    result = await decider(context)
    assert len(calls) == 2
    feedback = json.loads(calls[1]['messages'][-1]['content'])
    assert 'intent_status' in feedback['error']
    assert feedback['path'] == '$[proposal][assessment]'
    assert calls[1]['messages'][1] == calls[0]['messages'][1]
    assert calls[1]['messages'][2]['content'] == json.dumps(invalid)
    assert result['proposal'] == valid['proposal']
    assert result['usage'] == {'total_tokens': 200, 'provider_calls': 2}
    assert supervisor.delivered_binding == supervisor.binding()
    audit = [json.loads(line) for line in (tmp_path / 'REQUEST_AUDIT.jsonl').read_text().splitlines()]
    assert any(row['event'] == 'validation_error' for row in audit)
    assert [row['repair_feedback'] for row in audit if row['event'] == 'request'][1] == feedback


@pytest.mark.asyncio
async def test_exhaustion_bounded_without_default_release(tmp_path):
    context, _, supervisor, calls, decider = decider_fixture(tmp_path, ['{"type":"json_object","proposal":{}}'])
    with pytest.raises(RuntimeError, match='bounded feedback retries') as captured:
        await decider(context)
    assert len(calls) == 4
    assert captured.value.usage == {'total_tokens': 400, 'provider_calls': 4}
    assert supervisor.delivered_binding is None


@pytest.mark.asyncio
async def test_student_edit_conflict_regenerated_without_silent_field_removal(tmp_path):
    _, valid = context_and_decision()
    invalid = copy.deepcopy(valid)
    invalid['proposal']['edit'] = {'delta_joints': {}, 'left_gripper': 'keep', 'right_gripper': 'keep'}
    context, valid, supervisor, calls, decider = decider_fixture(tmp_path, [json.dumps(invalid), json.dumps(valid)])
    result = await decider(context)
    assert len(calls) == 2
    assert 'student takes no edit block' in calls[1]['messages'][-1]['content']
    assert json.loads(calls[1]['messages'][2]['content']) == invalid
    assert result['proposal'] == valid['proposal']
    assert supervisor.delivered_binding == supervisor.binding()


@pytest.mark.parametrize('edit', [{}, {'delta_joints': {}, 'left_gripper': 'keep'}, None])
def test_student_forbids_any_edit_block(edit):
    with pytest.raises(ValueError, match='student takes no edit block'):
        validate_native_decision({'mode': 'student', 'edit': edit})
    validate_native_decision({'mode': 'student'})
    validate_native_decision({'mode': 'edit', 'edit': {'delta_joints': {'fl_joint1': 0.03}}})


@pytest.mark.asyncio
async def test_native_http_error_preserves_body_without_retry(tmp_path, monkeypatch):
    body = b'{"ok":false,"error":{"code":"VLA_INVALID_ARGUMENT","message":"student takes no edit block"}}'
    calls = []
    def request_json(api, route, arguments):
        calls.append((api, route, arguments))
        raise HTTPError(api + route, 422, 'Unprocessable Entity', {}, BytesIO(body))
    monkeypatch.setattr('controller.request_json', request_json)
    adapter = RoboTwinAdapter(SimpleNamespace(root=tmp_path, api='http://127.0.0.1:19702'))
    with pytest.raises(RuntimeError, match='student takes no edit block'):
        await adapter.native('/tools/vla.bridge/decide:invoke', {'mode': 'student'})
    assert len(calls) == 1
    detail = json.loads((tmp_path / 'NATIVE_ERROR_AUDIT.jsonl').read_text())
    assert detail == {'route': '/tools/vla.bridge/decide:invoke', 'status': 422, 'body': body.decode()}
    assert not adapter.receipts


@pytest.mark.asyncio
async def test_semantic_edit_gate_remains_strict_during_repair(tmp_path):
    _, invalid = context_and_decision()
    invalid['proposal'].update(mode='edit', steps=5, edit={'delta_joints': {'fl_joint1': 0.03}})
    context, _, supervisor, calls, decider = decider_fixture(tmp_path, [json.dumps(invalid)])
    with pytest.raises(RuntimeError, match='observed execution failure'):
        await decider(context)
    assert len(calls) == 4
    assert supervisor.delivered_binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['stopping', 'completed'])
async def test_episode_boundary_stop_uses_verified_owned_shutdown(tmp_path, monkeypatch, phase):
    async def sleep(_):
        return None
    monkeypatch.setattr('controller.asyncio.sleep', sleep)
    proof = {'runtime_status': 'stopped', 'method': 'owned_skill_shutdown', 'remaining': []}
    runtime = SimpleNamespace(root=tmp_path, stop_executor=lambda: proof.copy())
    adapter = RoboTwinAdapter(runtime)
    scope = {name: 'scope_' + name for name in SCOPE_KEYS}
    episode = {'scope': scope, 'directory': tmp_path, 'native_invocation': 'native', 'history': [], 'gates': []}
    adapter.save(episode, state='ready', observation={'candidate': 'pending'})
    adapter.episodes[scope['subtask_id']] = episode
    adapter.active = scope['subtask_id']
    routes = []
    async def native(route, arguments=None):
        routes.append(route)
        return {'data': {'phase': phase}}
    adapter.native = native
    arguments = {**scope, 'operation': 'episode.stop', 'request_id': 'stop-one'}
    receipt = await adapter.submit(arguments)
    assert receipt['accepted']
    assert adapter.active is None
    assert episode['snapshot']['state'] == 'terminal' and episode['snapshot']['stopped']
    assert episode['snapshot']['outcome'] == 'infra_error'
    assert sum(route.endswith('/stop') for route in routes) == 1
    assert (tmp_path / 'STOP_PROOF.json').exists()
    assert (tmp_path / 'INVALID.json').exists()
    assert (await adapter.snapshot(episode))['state'] == 'terminal'
    before = len(routes)
    assert await adapter.submit(arguments) == receipt
    assert len(routes) == before


@pytest.mark.asyncio
async def test_unproven_executor_shutdown_cannot_settle_or_issue_receipt(tmp_path):
    def stop_executor():
        raise RuntimeError('owned_executor_processes_still_live')
    adapter = RoboTwinAdapter(SimpleNamespace(root=tmp_path, stop_executor=stop_executor))
    scope = {name: 'scope_' + name for name in SCOPE_KEYS}
    episode = {'scope': scope, 'directory': tmp_path, 'native_invocation': 'native', 'history': [], 'terminal': True}
    adapter.save(episode, state='terminal', observation={}, outcome='failure')
    adapter.episodes[scope['subtask_id']] = episode
    adapter.active = scope['subtask_id']
    with pytest.raises(RuntimeError, match='still_live'):
        await adapter.submit({**scope, 'operation': 'episode.stop', 'request_id': 'stop-one'})
    assert not adapter.receipts
    assert adapter.active is not None
    assert not episode['snapshot']['stopped']


def test_runtime_shutdown_requires_manager_stopped_state(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.state, runtime.skill = tmp_path, 'owned'
    runtime.policy_process = None
    runtime.executor_processes = lambda: []
    runtime.stop_skill = lambda: None
    directory = tmp_path / 'run/skills'
    directory.mkdir(parents=True)
    (directory / 'owned.json').write_text('{"status":"running"}')
    with pytest.raises(RuntimeError, match='not_confirmed'):
        runtime.stop_executor()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_route', ['stop', 'poll'])
@pytest.mark.parametrize('shutdown_proven', [True, False])
async def test_native_stop_connection_failure_still_requires_owned_shutdown(tmp_path, failure_route, shutdown_proven):
    shutdown_calls = []
    def stop_executor():
        shutdown_calls.append(True)
        if not shutdown_proven:
            raise RuntimeError('owned_executor_processes_still_live')
        return {'runtime_status': 'stopped', 'method': 'owned_skill_shutdown', 'remaining': []}
    adapter = RoboTwinAdapter(SimpleNamespace(root=tmp_path, stop_executor=stop_executor))
    scope = {name: 'scope_' + name for name in SCOPE_KEYS}
    episode = {'scope': scope, 'directory': tmp_path, 'native_invocation': 'native', 'history': []}
    adapter.save(episode, state='ready', observation={'candidate': 'pending'})
    adapter.episodes[scope['subtask_id']] = episode
    adapter.active = scope['subtask_id']
    routes = []
    async def native(route, arguments=None):
        routes.append(route)
        if failure_route == 'stop' or not route.endswith('/stop'):
            raise URLError(ConnectionRefusedError(111, 'Connection refused'))
        return {'data': {'status': 'requested'}}
    adapter.native = native
    arguments = {**scope, 'operation': 'episode.stop', 'request_id': 'stop-one'}
    if not shutdown_proven:
        with pytest.raises(RuntimeError, match='still_live'):
            await adapter.submit(arguments)
        assert not adapter.receipts and adapter.active == scope['subtask_id']
        assert not episode['snapshot']['stopped']
        assert not (tmp_path / 'STOP_PROOF.json').exists()
    else:
        receipt = await adapter.submit(arguments)
        proof = json.loads((tmp_path / 'STOP_PROOF.json').read_text())
        assert 'URLError' in proof['native_stop_error']
        assert 'Connection refused' in proof['native_stop_error']
        assert proof['native_status'] is None and not proof['native_terminal_before_shutdown']
        assert proof['runtime_status'] == 'stopped' and proof['remaining'] == []
        assert adapter.active is None and episode['snapshot']['stopped']
        assert episode['snapshot']['outcome'] == 'infra_error'
        count = len(routes)
        assert await adapter.snapshot(episode) == episode['snapshot']
        assert await adapter.submit(arguments) == receipt
        assert len(routes) == count
    assert shutdown_calls == [True]
    assert sum(route.endswith('/stop') for route in routes) == 1
