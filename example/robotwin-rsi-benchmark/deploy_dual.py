import argparse
import hashlib
import json
from pathlib import Path
import shutil


ASSIGNMENTS = {
    '67': {'pilot': 'turn_switch', 'tasks': [
        ['turn_switch', 400, 3, 3, 19701, 18901],
        ['handover_block', 800, 1, 1, 19702, 18902]],
        'runtime': 'Ubuntu22.04 A100 relocated runtime'},
    'liuy': {'pilot': 'press_stapler', 'tasks': [
        ['press_stapler', 400, 4, 1, 19703, 18903],
        ['place_object_scale', 400, 0, 3, 19704, 18904]],
        'runtime': 'Ubuntu20.04 RTX3090 source runtime'},
}


def deploy(core, root, base, host):
    if root.exists():
        raise RuntimeError('refuse_existing_batch')
    assignment = ASSIGNMENTS[host]
    root.mkdir(parents=True)
    source = core / 'example/robotwin-rsi-benchmark'
    for path in source.glob('*.py'):
        shutil.copy2(path, root / path.name)
    for name in ['BASE_SKILL.md', 'launch_dual.sh']:
        shutil.copy2(source / name, root / name)
    shutil.copytree(core / 'example/robotwin-vla-bridge/src', root / 'bridge_src')
    for name in ['screening_reference.json', 'CHECKPOINT_REFERENCE.json']:
        shutil.copy2(base / 'lesson_fixed4_20261012_v4' / name, root / name)
    manifest = {
        'version': 'senior_core_private_adapter_outcome_intent_v10_dual',
        'core_source': 'feature/benchmark@cbf956b0e5e176a185cda9264f93c2850e4e1c3d plus private adapter and episodic scope patch',
        'global_formal_episodes': 96, 'formal_episodes': 48,
        'development_episodes': 2, 'local_tasks': [row[0] for row in assignment['tasks']],
        'host': host, 'assignment': assignment,
        'model': 'deepseek-flash', 'arms': ['none', 'fixed'],
        'formal_policy_seeds': list(range(2001, 2013)), 'development_seed': 3101,
        'frozen_before_formal': True, 'cross_task_memory': False,
        'online_lesson_updates': False, 'paired_arms_same_host': True,
        'first_pair_gate': 'independent actual none/fixed pilot on each runtime',
        'memory': 'task-local frozen episodic experience in actual model context; not SKILL injection',
        'screening_scene_instruction_strict': True,
        'initial_identity': 'development establishes immutable host-local reference; both formal arms must match',
        'retry': 'at most one settled infrastructure retry; never retry valid robot failure',
        'prior_attempts': 'v5-v9 excluded; v9 valid stapler development preserved separately, no valid formal results',
        'format_repair': 'explicit required-field template, at most four feedback generations on same observation; no action until strict validation',
        'stop_protocol': 'native boundary stop requested once, then verified owned executor teardown; interrupted episodes remain infrastructure-invalid',
        'cleanup': 'worker finally stops owned skill and policy process group',
        'status': 'prepared_not_running',
    }
    (root / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2))
    hashes = {str(path.relative_to(core)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ['PhyAgentOS', 'example/robotwin-rsi-benchmark', 'example/robotwin-vla-bridge']
        for path in (core / directory).rglob('*')
        if path.is_file() and path.suffix in {'.py', '.sh', '.md', '.yaml'}}
    (root / 'SOURCE_HASHES.json').write_text(json.dumps(hashes, indent=2))
    print(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--host', choices=ASSIGNMENTS, required=True)
    args = parser.parse_args()
    deploy(args.core, args.root, args.base, args.host)
