import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from deploy_shards import LANES, validate_partition
from experiment import Experiment, import_experience, selected_pairs
from fixed_runner import pair_schedule, sha256


def test_pair_partition_exact_and_arm_order_unchanged():
    validate_partition(LANES)
    assigned = [pair for lane in LANES for pair in selected_pairs('handover_block', lane['repetitions'])]
    assert assigned == pair_schedule('handover_block')
    assert sum(len(pair['arms']) for pair in assigned) == 24
    assert {lane['host'] for lane in LANES} == {'67', '9'}


@pytest.mark.parametrize('repetitions', [[], [1, 1], [0], [13]])
def test_invalid_pair_selection_rejected(repetitions):
    with pytest.raises(ValueError):
        selected_pairs('handover_block', repetitions)


def test_duplicate_pair_assignment_rejected():
    lanes = copy.deepcopy(LANES)
    lanes[-1]['repetitions'] = [10, 12]
    with pytest.raises(ValueError):
        validate_partition(lanes)


def source_experience(tmp_path):
    source = tmp_path / 'source'
    frozen = source / 'frozen'
    development = source / 'development_a1'
    frozen.mkdir(parents=True)
    development.mkdir()
    scene = {'scene_seed': 100000, 'instruction': 'handover'}
    identity = {'state': 'state', 'images': ['image'], 'prompt': 'prompt'}
    (development / 'benchmark.json').write_text('{}')
    summary = {'classification': 'valid', 'development': True, 'policy_seed': 3101,
        'scene': scene, 'initial_observation': identity}
    (development / 'SUMMARY.json').write_text(json.dumps(summary))
    (development / 'STOP_PROOF.json').write_text(json.dumps({'runtime_status': 'stopped', 'remaining': []}))
    experience = {'task_name': 'handover_block', 'source_result_sha256': sha256(development / 'benchmark.json')}
    (frozen / 'experience.json').write_text(json.dumps(experience))
    (frozen / 'PROVENANCE.json').write_text(json.dumps({'task': 'handover_block',
        'source': str(development), 'experience_sha256': sha256(frozen / 'experience.json')}))
    (source / 'RUNTIME_INITIAL_REFERENCE.json').write_text(json.dumps({
        'policy_independent_scene_reference': scene, 'initial_observation': identity}))
    destination = tmp_path / 'destination'
    destination.mkdir()
    return source, destination, scene, experience


def test_import_preserves_exact_experience_and_reference_bytes(tmp_path):
    source, destination, scene, experience = source_experience(tmp_path)
    assert import_experience(source, destination, 'handover_block', scene) == experience
    for name in ['frozen/experience.json', 'RUNTIME_INITIAL_REFERENCE.json']:
        assert (source / name).read_bytes() == (destination / name).read_bytes()


@pytest.mark.parametrize('tamper', ['hash', 'task', 'scene', 'proof'])
def test_import_refuses_unproven_or_changed_source(tmp_path, tamper):
    source, destination, scene, _ = source_experience(tmp_path)
    if tamper == 'hash':
        (source / 'frozen/experience.json').write_text('{}')
    elif tamper == 'task':
        (source / 'frozen/PROVENANCE.json').write_text(json.dumps({'task': 'turn_switch'}))
    elif tamper == 'scene':
        scene = {'scene_seed': 100001}
    else:
        (source / 'development_a1/STOP_PROOF.json').write_text(json.dumps({'runtime_status': 'running', 'remaining': []}))
    with pytest.raises(RuntimeError, match='provenance_invalid'):
        import_experience(source, destination, 'handover_block', scene)
    assert not (destination / 'frozen').exists()


@pytest.mark.asyncio
async def test_partial_pair_recovery_retains_valid_arm_and_retries_only_invalid_arm(tmp_path):
    source, destination, scene, experience = source_experience(tmp_path)
    (source / 'CLEANUP.json').write_text(json.dumps({'own_policy_exited': True, 'skill_stop_error': None}))
    none = source / 'r01_none_a1'
    none.mkdir()
    (none / 'SUMMARY.json').write_text(json.dumps({'classification': 'valid', 'development': False,
        'task': 'handover_block', 'arm': 'none', 'policy_seed': 2001}))
    (none / 'STOP_PROOF.json').write_text(json.dumps({'runtime_status': 'stopped', 'remaining': []}))
    (none / 'REQUEST_AUDIT.jsonl').write_text(json.dumps({'event': 'request',
        'context': {'episodic_experience': []}}) + '\n')
    fixed = source / 'r01_fixed_a1'
    fixed.mkdir()
    (fixed / 'STOP_PROOF.json').write_text(json.dumps({'runtime_status': 'stopped', 'remaining': []}))
    (fixed / 'INVALID.json').write_text('{}')
    experiment = Experiment.__new__(Experiment)
    experiment.args = SimpleNamespace(import_task_root=str(source), resume_task_root=str(source),
        task='handover_block', pilot_task='handover_block', repetitions=None, root=tmp_path)
    experiment.runtime = SimpleNamespace(root=destination, reference={'scene': scene}, event=lambda value: None)
    async def setup():
        return None
    experiment.setup = setup
    experiment.launch_policy = lambda: None
    submitted = []
    async def run_root(cases, phase, experiences):
        submitted.append((phase, copy.deepcopy(cases)))
        for case in cases:
            directory = destination / case['case_key']
            directory.mkdir()
            (directory / 'SUMMARY.json').write_text('{}')
            memory = [experience] if case['environment']['arm'] == 'fixed' else []
            (directory / 'REQUEST_AUDIT.jsonl').write_text(json.dumps({'event': 'request',
                'context': {'episodic_experience': memory}}) + '\n')
    experiment.run_root = run_root
    await experiment.run()
    assert [case['case_key'] for case in submitted[0][1]] == ['r01_fixed_a2']
    cases = [case for _, group in submitted for case in group]
    assert len(cases) == 23
    assert not any(case['case_key'] == 'r01_none_a1' for case in cases)
    assert cases[0]['environment']['infra_attempt'] == 2
    complete = json.loads((destination / 'COMPLETE.json').read_text())
    assert complete['valid_formal_trials'] == 23
    assert complete['retained_prior_formal_trials'] == 1
    assert (tmp_path / 'PILOT_PASSED.json').exists()
