import argparse
import hashlib
import json
from pathlib import Path
import shutil


LANES = [
    {'lane': '67_H01', 'host': '67', 'gpu': 0, 'repetitions': [1, 2, 3], 'gateway': 19801, 'policy': 19001},
    {'lane': '67_H02', 'host': '67', 'gpu': 6, 'repetitions': [4, 5, 6], 'gateway': 19802, 'policy': 19002},
    {'lane': '9_H03', 'host': '9', 'gpu': 5, 'repetitions': [7, 8], 'gateway': 19803, 'policy': 19003},
    {'lane': '9_H04', 'host': '9', 'gpu': 6, 'repetitions': [9, 10], 'gateway': 19804, 'policy': 19004},
    {'lane': '9_H05', 'host': '9', 'gpu': 7, 'repetitions': [11, 12], 'gateway': 19805, 'policy': 19005},
]


def validate_partition(lanes):
    repetitions = [number for lane in lanes for number in lane['repetitions']]
    if sorted(repetitions) != list(range(1, 13)) or len({lane['lane'] for lane in lanes}) != len(lanes):
        raise ValueError('pair_partition_not_exact')
    for host in {lane['host'] for lane in lanes}:
        ports = [lane[name] for lane in lanes if lane['host'] == host for name in ['gateway', 'policy']]
        if len(ports) != len(set(ports)):
            raise ValueError('conflicting_host_ports')


def deploy(core, root, reference_root):
    validate_partition(LANES)
    if root.exists():
        raise RuntimeError('refuse_existing_shard_batch')
    source = core / 'example/robotwin-rsi-benchmark'
    hashes = {str(path.relative_to(core)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ['PhyAgentOS', 'example/robotwin-rsi-benchmark', 'example/robotwin-vla-bridge']
        for path in (core / directory).rglob('*')
        if path.is_file() and path.suffix in {'.py', '.sh', '.md', '.yaml'}}
    root.mkdir(parents=True)
    for lane in LANES:
        target = root / lane['lane']
        target.mkdir()
        for path in source.glob('*.py'):
            shutil.copy2(path, target / path.name)
        for name in ['BASE_SKILL.md', 'launch_shard.sh']:
            shutil.copy2(source / name, target / name)
        shutil.copytree(core / 'example/robotwin-vla-bridge/src', target / 'bridge_src')
        for name in ['screening_reference.json', 'CHECKPOINT_REFERENCE.json']:
            shutil.copy2(reference_root / name, target / name)
        manifest = {'version': 'v11_handover_pair_partition', 'host': lane['host'], 'lane': lane,
            'local_tasks': ['handover_block'], 'formal_episodes': len(lane['repetitions']) * 2,
            'global_formal_episodes': 96, 'model': 'deepseek-flash', 'core_path': str(core),
            'source_task_root': None if lane == LANES[0] else str(root / LANES[0]['lane'] / 'tasks/handover_block'),
            'memory': 'One development frozen experience, identical bytes and provenance across all handover lanes',
            'paired_arms_same_host': True, 'initial_identity': 'Exact source development identity enforced per native result',
            'pilot_gate': 'Each lane validates its actual first assigned pair; pilot counts toward assigned 24',
            'prior_handover': 'v10 development infrastructure-invalid, stopped and cleaned; zero valid formal',
            'healthy_v10_tasks': 'Continue unchanged on original hosts; no repartition or duplicate assignment',
            'cleanup': 'Finally stops only lane-owned Skill and model; no unrelated GPU job termination'}
        (target / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2))
        (target / 'SOURCE_HASHES.json').write_text(json.dumps(hashes, indent=2))
    (root / 'PARTITION.json').write_text(json.dumps({'lanes': LANES,
        'task': 'handover_block', 'formal_episodes': 24, 'source_core': str(core),
        'revision': 'Pairs partitioned across hosts, never split arms. Record host/runtime strata in analysis.'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, required=True)
    args = parser.parse_args()
    deploy(args.core, args.root, args.reference_root)
