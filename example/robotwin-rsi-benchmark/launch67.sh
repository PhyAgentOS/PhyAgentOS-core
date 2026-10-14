#!/usr/bin/env bash
set -euo pipefail
DEPLOY=/home/lvqinhan/paos-rsi67_20261012
CORE=$DEPLOY/core-benchmark-integrated
ROOT=$DEPLOY/${RSI_BATCH_NAME:-lesson_fixed4_core_20261012_v8}
MIRROR=$DEPLOY/mirror
source "$MIRROR/data6/lvqinhan/rsi/parallel10/homes/env_L02.sh"
export RSI_CORE=$CORE
export RSI_BASE=$MIRROR/data6/lvqinhan/rsi
export RSI_AGENT_PYTHON=$MIRROR/data7/home/lvqinhan/workspace/phyagentos/.venv/bin/python
export RSI_XPOLICY=$MIRROR/data7/home/lvqinhan/workspace/paos-benchmark-integration/src/robotwin-upstream/XPolicyLab
export RSI_MODEL_PATH=$MIRROR/data7/home/lvqinhan/models/robotwin_pi05/RoboTwin-lerobot_v30-aloha_agilex-joint-0
export PYTHONPATH=$CORE:$ROOT
export LITELLM_LOCAL_MODEL_COST_MAP=True
export OMP_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
mkdir -p "$ROOT/logs"
launch() {
  local task=$1 steps=$2 gpu=$3 gateway=$4 policy=$5
  "$RSI_AGENT_PYTHON" "$ROOT/experiment.py" --root "$ROOT" --task "$task" --max-steps "$steps" \
    --policy-gpu "$gpu" --simulation-gpu "$gpu" --gateway-port "$gateway" --policy-port "$policy" \
    > "$ROOT/logs/$task.log" 2>&1 &
  echo "$!" > "$ROOT/$task.pid"
}
for gpu in 1 2 3 4; do
  free=$(nvidia-smi -i "$gpu" --query-gpu=memory.free --format=csv,noheader,nounits)
  if [ "$free" -lt 28000 ]; then echo "GPU $gpu lacks safe headroom"; exit 1; fi
done
launch turn_switch 400 3 19601 18801
deadline=$((SECONDS+900))
while [ ! -s "$ROOT/tasks/turn_switch/development_a1/GATE_AUDIT.jsonl" ]; do
  if [ -f "$ROOT/tasks/turn_switch/ABORT.json" ] || [ "$SECONDS" -gt "$deadline" ]; then
    echo 'First real decision did not validate; remaining workers not admitted'
    exit 1
  fi
  sleep 3
done
sleep 10
launch handover_block 800 1 19602 18802
sleep 10
launch press_stapler 400 4 19603 18803
sleep 10
launch place_object_scale 400 2 19604 18804
"$RSI_AGENT_PYTHON" "$ROOT/monitor.py" --root "$ROOT" > "$ROOT/logs/monitor.log" 2>&1 &
wait
