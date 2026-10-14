import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'experiments/robotwin-eslp'))

from assert_idle import assert_idle
from robotwin_evidence import benchmark_succeeded, require_valid_episode
from rsi_evolve import RoboTwinOutcomeSource


def result(success):
    return {'status': 'succeeded', 'total_episodes': 1, 'completed_episodes': 1,
        'success_rate': int(success), 'episodes': [{'success': success,
        'termination': 'success' if success else 'failed'}]}


@pytest.mark.parametrize('raw', [{}, {'status': 'failed', 'total_episodes': 1,
    'completed_episodes': 0, 'success_rate': 0}, {**result(False), 'episodes': []},
    {**result(False), 'episodes': [{'success': False, 'termination': 'failed', 'error_code': 'CUDA_ERROR'}]}])
def test_invalid_evidence_never_becomes_learnable_failure(raw):
    with pytest.raises(ValueError):
        RoboTwinOutcomeSource([], raw, 'press_stapler').build('root')


@pytest.mark.parametrize('success', [True, False])
def test_completed_native_outcomes_remain_distinguishable(success):
    assert benchmark_succeeded(result(success)) == success


def test_legacy_guard_preserves_unresolved_database(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE agent_tasks(status TEXT, record_json TEXT)')
        connection.execute('INSERT INTO agent_tasks VALUES (?, ?)', ('cancelled', json.dumps({
            'revisions': [{'execution_records': [{'status': 'unknown'}]}]})))
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match='unresolved'):
        assert_idle(path)
    assert path.read_bytes() == original
