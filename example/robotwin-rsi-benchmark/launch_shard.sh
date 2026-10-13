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
import socket
import subprocess
import sys
root = Path(sys.argv[1])
manifest = json.loads((root / 'MANIFEST.json').read_text())
lane = manifest['lane']
free = int(subprocess.check_output(['nvidia-smi', '-i', str(lane['gpu']),
    '--query-gpu=memory.free', '--format=csv,noheader,nounits'], text=True).strip())
if free < 24000:
    raise RuntimeError(f'insufficient_gpu_headroom:{free}')
for port in [lane['gateway'], lane['policy']]:
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', port))
command = [sys.executable, str(root / 'experiment.py'), '--root', str(root),
    '--task', 'handover_block', '--pilot-task', 'handover_block', '--max-steps', '800',
    '--policy-gpu', str(lane['gpu']), '--simulation-gpu', str(lane['gpu']),
    '--gateway-port', str(lane['gateway']), '--policy-port', str(lane['policy']),
    '--repetitions', *map(str, lane['repetitions'])]
if manifest['source_task_root']:
    command += ['--import-task-root', manifest['source_task_root']]
with (root / 'logs/handover_block.log').open('a') as output:
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
(root / 'handover_block.pid').write_text(str(process.pid))
with (root / 'logs/monitor.log').open('a') as output:
    subprocess.Popen([sys.executable, str(root / 'monitor.py'), '--root', str(root)],
        stdout=output, stderr=subprocess.STDOUT)
raise SystemExit(process.wait())
PY
