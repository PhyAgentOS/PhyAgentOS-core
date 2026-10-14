#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE=${PAOS_RSI_BASE:-/data6/lvqinhan/rsi}
ROOT=${LESSON_AB_ROOT:-$BASE/lesson_ab}
TOOLS=${LESSON_AB_TOOLS:-$SCRIPT_DIR}
PARALLEL=${ROBOTWIN_PARALLEL_HOME:-$BASE/parallel10}
PY=${PAOS_PYTHON:-python3}
PROFILE=${ROBOTWIN_PROFILE:-gpt6_pi05}
LANE=$1
TASK_NAME=$2
MAX_STEPS=$3
SOURCE_RUN=${4:-1}
SOURCE_SESSION=$5

LANE_ID=${LANE,L}
SKILL=robotwin-rsi-$LANE_ID
TASK_DIR=$ROOT/tasks/$TASK_NAME
LOG_DIR=$TASK_DIR/logs
HOME_BASE=$PARALLEL/homes/$LANE
SKILL_DIR=$HOME_BASE/.PhyAgentOS/skills/$SKILL
WORKSPACE=$HOME_BASE/.PhyAgentOS/workspace
DB=$WORKSPACE/.paos/evolution/experience.sqlite3

mkdir -p "$TASK_DIR"/{states,carriers,results,sessions,reports,scores,prompts,logs}
source "$PARALLEL/homes/env_$LANE.sh"
LOG=$LOG_DIR/driver.log
log() { printf '%s [%s] %s\n' "$(date +%F_%T)" "$LANE" "$*" | tee -a "$LOG"; }

find_res() {
  local cur="$HOME/.PhyAgentOS/forge_runtime/environments/$SKILL/$PROFILE/current"
  [ -L "$cur" ] || return 1
  echo "$(readlink -f "$cur")/launch/profiles/$PROFILE/results"
}

stop_skill() {
  paos skill stop "$SKILL" --force >>"$LOG_DIR/runtime.log" 2>&1 || true
  sleep 5
}

reset_store() {
  stop_skill
  rm -f "$DB" "$DB-wal" "$DB-shm"
  if [ -f "$TASK_DIR/states/$1.sqlite3" ]; then
    cp "$TASK_DIR/states/$1.sqlite3" "$DB"
  fi
}

snapshot_store() {
  stop_skill
  rm -f "$TASK_DIR/states/$1.sqlite3"
  if [ -f "$DB" ]; then cp "$DB" "$TASK_DIR/states/$1.sqlite3"; fi
}

prepare_skill() {
  local carrier=$1
  local phase=$2
  stop_skill
  if [ ! -f "$TASK_DIR/states/base_skill.md" ]; then
    sed '/^## ACTIVE EXPERIENCE CARRIER:/,$d' "$SKILL_DIR/SKILL.md" > "$TASK_DIR/states/base_skill.md.tmp"
    mv "$TASK_DIR/states/base_skill.md.tmp" "$TASK_DIR/states/base_skill.md"
  fi
  cp "$TASK_DIR/states/base_skill.md" "$SKILL_DIR/SKILL.md"
  rm -f "$WORKSPACE/skills/$SKILL/references/LESSONS.md" "$WORKSPACE/LESSONS.md"
  if [ "$carrier" != "none" ]; then
    cat "$TASK_DIR/carriers/${phase}_${carrier}.md" >> "$SKILL_DIR/SKILL.md"
  fi
  local hash
  hash=$(sha256sum "$SKILL_DIR/SKILL.md" | cut -d" " -f1)
  printf '{"phase":"%s","carrier":"%s","skill_sha256":"%s"}\n' "$phase" "$carrier" "$hash" > "$TASK_DIR/states/${phase}_${carrier}_skill_hash.json"
}

make_prompt() {
  local arm=$1
  local phase=$2
  local base=$PARALLEL/prompts/${LANE}_${TASK_NAME}.txt
  local lesson_line
  case "$arm" in
    none) lesson_line='- 先 activate_skill("'$SKILL'")。本轮禁止参考任何历史 lesson 或 experience carrier，只使用当前 episode。';;
    native) lesson_line='- 先 activate_skill("'$SKILL'")，完整读取 ACTIVE EXPERIENCE CARRIER 中的 PAOS NATIVE LESSON，并显式转化为检查点决策。';;
    eslp) lesson_line='- 先 activate_skill("'$SKILL'")，完整读取 EVIDENCE-SCOPED LESSON PACK，引用证据编号并转化为检查点决策。';;
    dialogue) lesson_line='- 先 activate_skill("'$SKILL'")，完整读取 PRIOR-EPISODE DIALOGUE CARD，不要复制旧失败动作，按当前证据改变检查点决策。';;
  esac
  "$PY" - "$base" "$lesson_line" "$TASK_DIR/prompts/${phase}_${arm}.txt" <<'PYCODE'
from pathlib import Path
import sys
source, replacement, output = sys.argv[1:]
lines = Path(source).read_text(encoding="utf-8").splitlines()
out = []
replaced = False
for line in lines:
    if line.startswith("- 先 activate_skill("):
        out.append(replacement)
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.insert(1, replacement)
Path(output).write_text("\n".join(out) + "\n", encoding="utf-8")
PYCODE
}

run_episode() {
  local arm=$1
  local phase=$2
  stop_skill
  "$PY" "$TOOLS/assert_idle.py" "$HOME/.PhyAgentOS/workspace/.paos/agent_tasks/tasks.sqlite3" >>"$LOG_DIR/maintenance.log" 2>&1 || return 1
  paos skill start "$SKILL" --profile "$PROFILE" >>"$LOG_DIR/runtime.log" 2>&1
  sleep 30
  local result_path=""
  local session="lessonab-${LANE}-${TASK_NAME}-${arm}-${phase}-$(date +%m%d%H%M%S)"
  for attempt in 1 2; do
    local res_dir
    res_dir=$(find_res || true)
    if [ -z "$res_dir" ]; then
      log "NO_RES_DIR arm=$arm phase=$phase attempt=$attempt"
      sleep 10
      continue
    fi
    mkdir -p "$res_dir"
    local start=$(date +%s)
    local run_log=$LOG_DIR/${phase}_${arm}_attempt${attempt}.log
    timeout $((MAX_STEPS * 4 > 5400 ? MAX_STEPS * 4 : 5400)) paos agent \
      --session "$session" \
      -c "$HOME/.PhyAgentOS/config.rsi.ds.json" \
      --no-markdown \
      -m "$(cat "$TASK_DIR/prompts/${phase}_${arm}.txt")" >>"$run_log" 2>&1 || true
    local new
    new=$(find "$res_dir" -maxdepth 1 -name 'robotwin_*.json' -newermt "@$start" 2>/dev/null | sort | tail -1 || true)
    if [ -n "$new" ]; then
      result_path=$new
      session=$(basename "$session")
      break
    fi
    log "NO_RESULT arm=$arm phase=$phase attempt=$attempt"
  done
  stop_skill
  if [ -z "$result_path" ]; then
    printf '{"lane":"%s","task":"%s","arm":"%s","phase":"%s","status":"missing_result"}\n' "$LANE" "$TASK_NAME" "$arm" "$phase" >> "$TASK_DIR/results/events.jsonl"
    return 1
  fi
  PYTHONPATH="$TOOLS" "$PY" - "$result_path" <<'PYCODE' || return 1
import json, sys
from robotwin_evidence import require_valid_episode
require_valid_episode(json.load(open(sys.argv[1])))
PYCODE
  cp "$result_path" "$TASK_DIR/results/${phase}_${arm}.json"
  cp "$WORKSPACE/sessions/$session.jsonl" "$TASK_DIR/sessions/${phase}_${arm}.jsonl"
  local carrier=none
  case "$arm" in native) carrier=native;; eslp) carrier=eslp;; dialogue) carrier=dialogue;; esac
  local carrier_file=none
  [ "$carrier" != "none" ] && carrier_file=$TASK_DIR/carriers/${phase}_${carrier}.md
  PYTHONPATH=$TOOLS "$PY" "$TOOLS/episode_scorer.py" \
    --session "$TASK_DIR/sessions/${phase}_${arm}.jsonl" \
    --result "$TASK_DIR/results/${phase}_${arm}.json" \
    --arm "$arm" --phase "$phase" --carrier "$carrier_file" \
    --out "$TASK_DIR/scores/${phase}_${arm}.json" >>"$LOG_DIR/scorer.log" 2>&1
  printf '{"lane":"%s","task":"%s","arm":"%s","phase":"%s","status":"completed","session":"%s","result":"%s"}\n' "$LANE" "$TASK_NAME" "$arm" "$phase" "$session" "$TASK_DIR/results/${phase}_${arm}.json" >> "$TASK_DIR/results/events.jsonl"
}

success_of() { jq -r '.success_rate // 0' "$TASK_DIR/results/$1" 2>/dev/null || echo 0; }

reflect_latest() {
  local phase=$1
  local arm=$2
  local report=$TASK_DIR/reports/${phase}_${arm}_native.json
  if [ "$arm" != "dialogue" ]; then
    reset_store bootstrap_native
    "$PY" "$TOOLS/rsi_evolve.py" \
      --config "$HOME/.PhyAgentOS/config.rsi.ds.json" \
      --session "$TASK_DIR/sessions/${phase}_${arm}.jsonl" \
      --result "$TASK_DIR/results/${phase}_${arm}.json" \
      --task-name "$TASK_NAME" \
      --root "lessonab-${LANE}-${TASK_NAME}-${phase}-${arm}-root" \
      --session-key "lessonab-${LANE}-${TASK_NAME}-${phase}-${arm}-experience" \
      --skill "$SKILL" \
      --report-out "$report" >"$LOG_DIR/${phase}_${arm}_reflect.log" 2>&1
    snapshot_store "${phase}_native"
  fi
  local native_report=$report
  [ "$arm" = "dialogue" ] && native_report=""
  PYTHONPATH=$TOOLS "$PY" "$TOOLS/lesson_materializer.py" \
    --task "$TASK_NAME" \
    --session "$TASK_DIR/sessions/${phase}_${arm}.jsonl" \
    --result "$TASK_DIR/results/${phase}_${arm}.json" \
    --native-report "$native_report" \
    --out-dir "$TASK_DIR/carriers/${phase}_${arm}" >>"$LOG_DIR/materialize.log" 2>&1
  cp "$TASK_DIR/carriers/${phase}_${arm}/eslp.md" "$TASK_DIR/carriers/${phase}_eslp.md"
  cp "$TASK_DIR/carriers/${phase}_${arm}/dialogue.md" "$TASK_DIR/carriers/${phase}_dialogue.md"
  if [ "$arm" != "dialogue" ]; then
    "$PY" - "$TASK_DIR/carriers/${phase}_${arm}/manifest.json" "$report" "$TASK_DIR/carriers/${phase}_native.md" <<'PYCODE'
import json,sys
manifest=json.load(open(sys.argv[1])); report=json.load(open(sys.argv[2])); out=sys.argv[3]
lessons=report.get('lessons') or [x for x in report.get('all_lessons',[]) if x.get('status')=='active']
body='\n## ACTIVE EXPERIENCE CARRIER: PAOS NATIVE LESSON\nThis is the real PAOS reflection output for the same task.\n'
for lesson in lessons: body+='\n- '+json.dumps(lesson,ensure_ascii=False)+'\n'
open(out,'w',encoding='utf-8').write(body)
manifest['documents']['native.md']={'bytes':len(body.encode()),'approx_tokens':len(body)//4}
open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(manifest,ensure_ascii=False,indent=2))
PYCODE
  fi
}

log "START task=$TASK_NAME source_run=$SOURCE_RUN max_steps=$MAX_STEPS pid=$$"
SOURCE_RESULT=$PARALLEL/results/$TASK_NAME/run$SOURCE_RUN.json
[ -f "$SOURCE_RESULT" ]
[ -f "$SOURCE_SESSION" ]

if [ ! -f "$TASK_DIR/states/bootstrap_native.sqlite3" ]; then
  log "MATERIALIZE begin"
  reset_store empty
    "$PY" "$TOOLS/rsi_evolve.py" \
    --config "$HOME/.PhyAgentOS/config.rsi.ds.json" \
    --session "$SOURCE_SESSION" \
    --result "$SOURCE_RESULT" \
    --task-name "$TASK_NAME" \
    --root "lessonab-${LANE}-${TASK_NAME}-bootstrap-root" \
    --session-key "lessonab-${LANE}-${TASK_NAME}-bootstrap-experience" \
    --skill "$SKILL" \
    --report-out "$TASK_DIR/reports/bootstrap_native.json" >"$LOG_DIR/bootstrap_reflect.log" 2>&1
  snapshot_store bootstrap_native
  PYTHONPATH=$TOOLS "$PY" "$TOOLS/lesson_materializer.py" \
    --task "$TASK_NAME" \
    --session "$SOURCE_SESSION" \
    --result "$SOURCE_RESULT" \
    --native-report "$TASK_DIR/reports/bootstrap_native.json" \
    --out-dir "$TASK_DIR/carriers/bootstrap" >>"$LOG_DIR/materialize.log" 2>&1
  cp "$TASK_DIR/carriers/bootstrap/eslp.md" "$TASK_DIR/carriers/bootstrap_eslp.md"
  cp "$TASK_DIR/carriers/bootstrap/dialogue.md" "$TASK_DIR/carriers/bootstrap_dialogue.md"
  "$PY" - "$TASK_DIR/carriers/bootstrap/manifest.json" "$TASK_DIR/reports/bootstrap_native.json" "$TASK_DIR/carriers/bootstrap_native.md" <<'PYCODE'
import json,sys
manifest=json.load(open(sys.argv[1])); report=json.load(open(sys.argv[2])); out=sys.argv[3]
lessons=report.get('lessons') or [x for x in report.get('all_lessons',[]) if x.get('status')=='active']
body='\n## ACTIVE EXPERIENCE CARRIER: PAOS NATIVE LESSON\nThis is the real PAOS reflection output for the same task.\n'
for lesson in lessons: body+='\n- '+json.dumps(lesson,ensure_ascii=False)+'\n'
open(out,'w',encoding='utf-8').write(body)
manifest['documents']['native.md']={'bytes':len(body.encode()),'approx_tokens':len(body)//4}
open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(manifest,ensure_ascii=False,indent=2))
PYCODE
  log "MATERIALIZE done"
fi

for arm in none native eslp dialogue; do
  if [ ! -f "$TASK_DIR/results/replay1_${arm}.json" ]; then
    case "$arm" in
      none|dialogue) reset_store empty ;;
      native|eslp) reset_store bootstrap_native ;;
    esac
    prepare_skill "$arm" bootstrap
    make_prompt "$arm" replay1
    log "RUN replay1 arm=$arm"
    if run_episode "$arm" replay1; then
      log "DONE replay1 arm=$arm success=$(success_of "replay1_${arm}.json")"
    else
      log "FAILED replay1 arm=$arm"
    fi
  fi
  phase=replay1
  if [ "$(success_of "replay1_${arm}.json")" = "0" ] && [ "$arm" != "none" ] && [ -f "$TASK_DIR/results/replay1_${arm}.json" ] && [ ! -f "$TASK_DIR/results/replay2_${arm}.json" ]; then
    log "REFLECT replay1 arm=$arm"
    reflect_latest "$phase" "$arm" || log "REFLECT_FAILED arm=$arm"
    case "$arm" in
      none|dialogue) reset_store empty ;;
      native|eslp) reset_store "${phase}_native" ;;
    esac
    prepare_skill "$arm" "$phase"
    make_prompt "$arm" replay2
    log "RUN replay2 arm=$arm"
    if run_episode "$arm" replay2; then
      log "DONE replay2 arm=$arm success=$(success_of "replay2_${arm}.json")"
    else
      log "FAILED replay2 arm=$arm"
    fi
  fi
done

log "FINISH task=$TASK_NAME"
