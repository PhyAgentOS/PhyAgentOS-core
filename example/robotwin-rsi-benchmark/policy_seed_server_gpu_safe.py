import hashlib
import json

import jax
import numpy as np

from policy_seed_server import main
from openpi.models import model as model_module


original_restore = model_module.restore_params


def restore_via_host(params_path, *, restore_type=jax.Array, dtype=None, sharding=None):
    if restore_type is not jax.Array:
        return original_restore(params_path, restore_type=restore_type, dtype=dtype, sharding=sharding)
    params = original_restore(params_path, restore_type=np.ndarray, dtype=dtype)
    target = sharding or jax.sharding.SingleDeviceSharding(jax.devices('gpu')[0])
    leaves, structure = jax.tree.flatten(params)
    restored = []
    content = hashlib.sha256()
    total_bytes = 0
    for leaf in leaves:
        content.update(str(leaf.shape).encode())
        content.update(str(leaf.dtype).encode())
        content.update(leaf.tobytes())
        total_bytes += leaf.nbytes
        placed = jax.device_put(leaf, target)
        placed.block_until_ready()
        restored.append(placed)
    print('HOST_RESTORE_AUDIT ' + json.dumps({
        'leaf_count': len(leaves), 'parameter_bytes': total_bytes,
        'host_parameter_sha256': content.hexdigest(), 'dtype_requested': str(dtype),
        'restore_only_changed': True,
    }), flush=True)
    return jax.tree.unflatten(structure, restored)


if __name__ == '__main__':
    model_module.restore_params = restore_via_host
    main()
