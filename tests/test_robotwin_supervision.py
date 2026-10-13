import copy
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from experiment import proposal_schema
from supervision import Supervisor, status_outputs


def test_explicit_control_skill_catalog_without_global_config(tmp_path):
    from PhyAgentOS.agent.experience.activation import SkillActivationManager
    from PhyAgentOS.agent.experience.store import ExperienceStore
    installed = tmp_path / 'private_control_catalog'
    source = installed / 'robotwin-benchmark-control'
    source.mkdir(parents=True)
    (source / 'SKILL.md').write_text('---\nname: robotwin-benchmark-control\ndescription: control\n---\ncontrol')
    workspace = tmp_path / 'workspace'
    manager = SkillActivationManager(workspace=workspace, store=ExperienceStore(workspace),
        installed_skills_dir=installed)
    resolved = manager.skills.resolve_skill('robotwin-benchmark-control')
    assert Path(resolved['path']) == source / 'SKILL.md'


def test_malformed_status_is_not_trusted():
    for value in [{'ok': True, 'data': None}, {'ok': True, 'data': {'response': []}},
                  {'ok': True, 'data': {'response': {'result': None}}}]:
        assert status_outputs(value) == {}


def test_wrist_tool_id_and_compact_previous_boundary(tmp_path):
    supervisor = Supervisor(tmp_path)
    supervisor.observe_status({'episode': {'index': 1, 'steps': 0},
        'checkpoint': {'checkpoint_id': 'one'}, 'candidate': {'freshness': 'pending'}})
    frame = tmp_path / 'wrist.jpg'
    frame.write_bytes(b'frame')
    supervisor.observe_frame('vision.get_frame_left_wrist', {'image_path': str(frame)})
    assert 'left_wrist' in supervisor.frames
    supervisor.previous = {'packet': {'episode': {}, 'previous_reviewed_boundary': {'nested': 'old'},
        'previous_accepted_assessment': {'old': True}}, 'assessment': {}, 'frames': {}}
    assert 'previous_reviewed_boundary' not in supervisor.packet()['previous_reviewed_boundary']


def test_proposal_schema_rejects_wrong_gripper_and_extra_authority():
    schema = proposal_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert any(error.validator == 'additionalProperties' for error in validator.iter_errors({'run_id': 'forged'}))
    gripper = schema['properties']['edit']['properties']['left_gripper']['enum']
    assert 'closed' in gripper and 'close' not in gripper


@pytest.mark.asyncio
async def test_bridge_native_query_semantics_are_respected():
    from controller import RoboTwinAdapter
    adapter = RoboTwinAdapter(None)
    adapter.tools = [{'tool_id': 'vla.decide', 'semantics': 'query', 'endpoint_id': 'vla', 'operation': 'decide'}]
    routes = []
    async def native(route, arguments=None):
        routes.append(route)
        return {'data': {'response': {'result': {'status': 'succeeded', 'outputs': {'accepted': True}}}}}
    adapter.native = native
    assert await adapter.action('vla.decide', {'mode': 'student', 'steps': 10}) == {'accepted': True}
    assert routes == ['/tools/vla/decide:invoke']


@pytest.mark.asyncio
async def test_native_phase_only_status_does_not_break_observation(tmp_path):
    from types import SimpleNamespace
    from controller import RoboTwinAdapter
    runtime = SimpleNamespace(reference={'scene': {'instruction': 'turn left switch'}}, description='switch task')
    adapter = RoboTwinAdapter(runtime)
    episode = {'native_invocation': 'native', 'ready': True, 'snapshot': {'state': 'ready'}}
    routes = []
    async def native(route, arguments=None):
        routes.append(route)
        return {'data': {'status': 'pending'}} if route.endswith('/result') else {'data': {'phase': 'running'}}
    adapter.native = native
    assert await adapter.snapshot(episode) == {'state': 'ready'}
    assert routes == ['/invocations/native/result']
