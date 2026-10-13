import argparse
import json
from pathlib import Path
import time

from prescreen_runner import save_json


def active(pid, root):
    try:
        return str(root).encode() in Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return False


def snapshot(root):
    manifest = json.loads((root / 'MANIFEST.json').read_text())
    rows = []
    for directory in sorted((root / 'tasks').glob('*')):
        summaries = [json.loads(path.read_text()) for path in directory.glob('*/SUMMARY.json')]
        pid_path = root / (directory.name + '.pid')
        alive = pid_path.exists() and active(int(pid_path.read_text()), root)
        policy_pid = directory / 'POLICY_PID.json'
        policy_alive = policy_pid.exists() and active(json.loads(policy_pid.read_text())['pid'], root)
        rows.append({'task': directory.name, 'worker_alive': alive, 'policy_alive': policy_alive,
            'valid_formal': sum(not item['development'] for item in summaries),
            'valid_development': sum(item['development'] for item in summaries),
            'successes': {arm: sum(item['success_rate'] for item in summaries
                if not item['development'] and item['arm'] == arm) for arm in ['none', 'fixed']},
            'invalid_attempts': len(list(directory.glob('*/INVALID.json'))),
            'frozen': (directory / 'frozen/experience.json').exists(),
            'complete': (directory / 'COMPLETE.json').exists(),
            'abort': json.loads((directory / 'ABORT.json').read_text()) if (directory / 'ABORT.json').exists() else None})
    data = {'timestamp': time.time(), 'formal_valid': sum(row['valid_formal'] for row in rows),
        'planned_formal': manifest['formal_episodes'],
        'expected_tasks': manifest.get('local_tasks', manifest.get('tasks', [])),
        'pilot_passed': (root / 'PILOT_PASSED.json').exists(), 'tasks': rows}
    save_json(root / 'PROGRESS.json', data)
    return data


def main(root):
    while True:
        data = snapshot(root)
        rows = data['tasks']
        if {row['task'] for row in rows} == set(data['expected_tasks']) and all(not row['worker_alive'] for row in rows):
            data['state'] = 'complete' if data['formal_valid'] == data['planned_formal'] and all(row['complete'] for row in rows) else 'incomplete'
            data['owned_models_exited'] = all(not row['policy_alive'] for row in rows)
            save_json(root / 'FINAL_STATUS.json', data)
            return
        time.sleep(30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    main(parser.parse_args().root)
