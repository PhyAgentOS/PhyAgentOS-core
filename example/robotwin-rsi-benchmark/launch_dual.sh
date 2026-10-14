#!/usr/bin/env bash
set -euo pipefail
ROOT=$1
source "$RSI_BASE/parallel10/homes/env_L02.sh"
export PYTHONPATH=$RSI_CORE:$ROOT
export LITELLM_LOCAL_MODEL_COST_MAP=True
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
mkdir -p "$ROOT/logs"
"$RSI_AGENT_PYTHON" - "$ROOT" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
root = Path(sys.argv[1])
manifest = json.loads((root / 'MANIFEST.json').read_text())
for task, steps, policy, simulation, gateway, port in manifest['assignment']['tasks']:
    for gpu, minimum in [(policy, 12500), (simulation, 6000)]:
        free = int(subprocess.check_output(['nvidia-smi', '-i', str(gpu), '--query-gpu=memory.free',
            '--format=csv,noheader,nounits'], text=True).strip())
        if free < minimum:
            raise RuntimeError(f'insufficient_gpu_headroom:{gpu}:{free}<{minimum}')
    import socket
    for number in [gateway, port]:
        with socket.socket() as connection:
            connection.bind(('127.0.0.1', number))
PY
mapfile -t rows < <("$RSI_AGENT_PYTHON" - "$ROOT" <<'PY'
import json
from pathlib import Path
import sys
manifest = json.loads((Path(sys.argv[1]) / 'MANIFEST.json').read_text())
for row in manifest['assignment']['tasks']:
    print(' '.join(map(str, row)) + ' ' + manifest['assignment']['pilot'])
PY
)
for row in "${rows[@]}"; do
  read -r task steps policy_gpu simulation_gpu gateway policy_port pilot <<< "$row"
  "$RSI_AGENT_PYTHON" "$ROOT/experiment.py" --root "$ROOT" --task "$task" --pilot-task "$pilot" \
    --max-steps "$steps" --policy-gpu "$policy_gpu" --simulation-gpu "$simulation_gpu" \
    --gateway-port "$gateway" --policy-port "$policy_port" > "$ROOT/logs/$task.log" 2>&1 &
  echo "$!" > "$ROOT/$task.pid"
done
"$RSI_AGENT_PYTHON" "$ROOT/monitor.py" --root "$ROOT" > "$ROOT/logs/monitor.log" 2>&1 &
wait
