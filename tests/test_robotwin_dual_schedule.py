import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'example/robotwin-rsi-benchmark'))

from deploy_dual import ASSIGNMENTS, deploy
from monitor import snapshot


def test_dual_hosts_partition_four_tasks_without_splitting_arms():
    assigned = [row[0] for assignment in ASSIGNMENTS.values() for row in assignment['tasks']]
    assert len(assigned) == len(set(assigned)) == 4
    assert set(assigned) == {'turn_switch', 'handover_block', 'press_stapler', 'place_object_scale'}
    for assignment in ASSIGNMENTS.values():
        assert assignment['pilot'] in [row[0] for row in assignment['tasks']]


def test_monitor_counts_local_plan_not_global_plan(tmp_path):
    (tmp_path / 'MANIFEST.json').write_text(json.dumps({'formal_episodes': 48,
        'local_tasks': ['press_stapler', 'place_object_scale']}))
    directory = tmp_path / 'tasks/press_stapler/r01_none_a1'
    directory.mkdir(parents=True)
    (directory / 'SUMMARY.json').write_text(json.dumps({'development': False, 'success_rate': 0, 'arm': 'none'}))
    data = snapshot(tmp_path)
    assert data['planned_formal'] == 48
    assert data['formal_valid'] == 1
    assert data['tasks'][0]['successes']['none'] == 0


def test_deploy_refuses_overwriting_existing_experiment(tmp_path):
    with pytest.raises(RuntimeError, match='existing_batch'):
        deploy(tmp_path, tmp_path, tmp_path, 'liuy')
