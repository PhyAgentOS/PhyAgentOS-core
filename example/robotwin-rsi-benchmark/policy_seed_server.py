import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def array_digest(value):
    import numpy as np

    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, dict):
            tree = {name: array_digest(item) for name, item in sorted(value.items())}
        else:
            tree = [array_digest(item) for item in value]
        content = json.dumps(tree, sort_keys=True, separators=(',', ':')).encode()
        return {'encoding': 'structured_numeric_tree', 'sha256': hashlib.sha256(content).hexdigest()}
    array = np.asarray(value)
    if array.dtype.hasobject:
        raise RuntimeError('object_array_is_not_a_semantic_numeric_digest')
    return {'shape': list(array.shape), 'dtype': str(array.dtype),
            'sha256': hashlib.sha256(array.tobytes()).hexdigest()}


def seed_policy(policy, seed):
    if getattr(policy, '_is_pytorch_model', False):
        import torch

        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        return 'pytorch'
    if not hasattr(policy, '_rng'):
        raise RuntimeError('policy_rng_unavailable')
    import jax

    policy._rng = jax.random.key(seed)
    return 'jax'


def make_seeded_model(base_model):
    class SeededModel(base_model):
        def __init__(self, config, control_path, audit_path):
            self.control_path = control_path
            self.audit_path = audit_path
            self.trial_id = None
            self.inferences = 0
            super().__init__(config)

        def audit(self, event):
            with self.audit_path.open('a', encoding='utf-8') as output:
                output.write(json.dumps({'timestamp': time.time(), **event}) + '\n')

        def apply_seed(self, force=False):
            control = json.loads(self.control_path.read_text())
            if force or self.trial_id != control['trial_id']:
                backend = seed_policy(self.policy, control['policy_seed'])
                self.trial_id = control['trial_id']
                self.inferences = 0
                self.audit({'event': 'rng_reset', 'trial_id': self.trial_id,
                            'policy_seed': control['policy_seed'], 'backend': backend})

        def reset(self):
            super().reset()
            self.apply_seed(force=True)

        def get_action_batch(self, env_idx_list=None, **kwargs):
            self.apply_seed()
            observation = self.observation_window
            actions = super().get_action_batch(env_idx_list=env_idx_list, **kwargs)
            if self.inferences == 0:
                self.audit({'event': 'first_inference', 'trial_id': self.trial_id,
                            'state': array_digest(observation['state']),
                            'images': {name: array_digest(value) for name, value in observation['images'].items()},
                            'prompt': observation['prompt'], 'actions': array_digest(actions)})
            self.inferences += 1
            return actions

    return SeededModel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xpolicy-root', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.xpolicy_root))
    sys.path.insert(0, str(args.xpolicy_root.parent))
    from XPolicyLab.policy.Pi_05.model import Model
    from client_server.ws.model_server import PolicyServer, PolicyServerConfig

    config = {
        'task_name': 'adjust_bottle', 'env_cfg_type': 'aloha_agilex', 'action_type': 'joint',
        'model_path': os.environ.get('RSI_MODEL_PATH', '/data7/home/lvqinhan/models/robotwin_pi05/RoboTwin-lerobot_v30-aloha_agilex-joint-0'),
        'checkpoint_num': 29999, 'train_config_name': 'pi05_base_aloha_full_sim_arx-x5_seed_0',
        'repo_id': 'RoboTwin_lerobot_v30', 'policy_name': 'Pi_05',
    }
    model = make_seeded_model(Model)(config, args.control, args.audit)
    server = PolicyServer(model, PolicyServerConfig(host='127.0.0.1', port=args.port))
    print(f'SEEDED_POLICY_READY port={args.port}', flush=True)
    asyncio.run(server.serve_forever())


if __name__ == '__main__':
    main()
