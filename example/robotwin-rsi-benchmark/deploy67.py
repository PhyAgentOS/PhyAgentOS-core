import json
import hashlib
import os
from pathlib import Path
import shutil


ROOT = Path('/home/lvqinhan/paos-rsi67_20261012')
CORE = ROOT / 'core-benchmark-integrated'
BATCH = ROOT / os.environ.get('RSI_BATCH_NAME', 'lesson_fixed4_core_20261012_v8')
SOURCE = CORE / 'example/robotwin-rsi-benchmark'


def main():
    if (BATCH / 'tasks').exists():
        raise RuntimeError('cannot deploy over an admitted experiment')
    BATCH.mkdir(exist_ok=True)
    for path in SOURCE.glob('*.py'):
        shutil.copy2(path, BATCH / path.name)
    shutil.copy2(SOURCE / 'BASE_SKILL.md', BATCH / 'BASE_SKILL.md')
    shutil.copytree(CORE / 'example/robotwin-vla-bridge/src', BATCH / 'bridge_src', dirs_exist_ok=True)
    reference = ROOT / 'mirror/data6/lvqinhan/rsi/lesson_fixed4_20261012_v4'
    for name in ['screening_reference.json', 'CHECKPOINT_REFERENCE.json']:
        shutil.copy2(reference / name, BATCH / name)
    tasks = ['place_object_scale', 'handover_block', 'press_stapler', 'turn_switch']
    manifest = {'version': 'senior_core_private_adapter_outcome_intent_v8',
        'core_source': 'feature/benchmark@cbf956b0e5e176a185cda9264f93c2850e4e1c3d plus private adapter and episodic scope patch',
        'model': 'deepseek-flash', 'tasks': tasks, 'formal_episodes': 96, 'development_episodes': 4,
        'arms': ['none', 'fixed'], 'formal_policy_seeds': list(range(2001, 2013)),
        'development_seed': 3101, 'reflection_frozen_before_formal': True,
        'cross_task_memory': False, 'online_lesson_updates': False,
        'first_pair_gate_task': 'turn_switch', 'machine': '222.200.185.67',
        'runtime_revision': 'Ubuntu22.04 A100; immutable development-established initial identity per task',
        'screening_scene_and_instruction_strict': True, 'paired_arms_same_worker_and_initial_identity': True,
        'screening_render_identity': 'record equality, not require cross-driver hash equality',
        'unbounded_release': False, 'control_protocol': 'benchmark_contract_v1',
        'memory_type': 'task-local frozen episodic experience, not generalized two-case LessonCandidate',
        'resources': {'place_object_scale': [2, 2], 'handover_block': [1, 1],
            'press_stapler': [4, 4], 'turn_switch': [3, 3]},
        'cleanup': 'per-worker finally stops own skill and kills own policy process group',
        'status': 'prepared_not_running'}
    (BATCH / 'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    source_hashes = {str(path.relative_to(CORE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ['PhyAgentOS', 'example/robotwin-rsi-benchmark', 'example/robotwin-vla-bridge']
        for path in (CORE / directory).rglob('*') if path.is_file() and path.suffix in {'.py', '.sh', '.md', '.yaml'}}
    (BATCH / 'SOURCE_HASHES.json').write_text(json.dumps(source_hashes, indent=2))
    print(BATCH)


if __name__ == '__main__':
    main()
