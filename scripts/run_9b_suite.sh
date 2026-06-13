#!/bin/bash
# Run test boards through the LLM-driven pipeline with a local model.
# Captures pass/fail + routing completion + DRC per board into a summary.
#
# Usage:
#   scripts/run_9b_suite.sh                       # default model, all boards
#   MODEL=Qwen3.6-35B-A3B-oQ6 scripts/run_9b_suite.sh board1 board2 ...
set -o pipefail
cd "$(dirname "$0")/.."

MODEL="${MODEL:-Qwen3.5-9B-MLX-9bit}"
export PCB_LLM_API_BASE="http://localhost:8083/v1"
export PCB_LLM_API_KEY="local-dummy"
export PCB_GENERATE_MODEL="openai/${MODEL}"
export PCB_REVIEW_MODEL="openai/${MODEL}"
export PCB_GATHER_MODEL="openai/${MODEL}"
export PCB_MODEL_PROFILE="${PCB_MODEL_PROFILE:-small}"
export PCB_ROUTER_ENGINE="freerouting"
export PCB_LLM_TIMEOUT="600"

TAG=$(echo "$MODEL" | tr -c 'A-Za-z0-9' '_')
LOG_DIR="test/logs/${TAG}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/summary.txt"

# Boards: from args if given, else the full suite (Phase-3 targets first).
if [ "$#" -gt 0 ]; then
  BOARDS=("$@")
else
  BOARDS=(
    test_555_blinker
    test_esp8266_breakout
    test_4ch_relay_module
    test_ads1115_breakout
    test_lm2596_buck
    test_neopixel_driver
    test_rs485_converter
    test_soil_moisture
    test_l298n_motor_driver
    test_arduino_nano
  )
fi

echo "suite — model ${MODEL}, profile=${PCB_MODEL_PROFILE}" | tee "$SUMMARY"
echo "started $(date)" | tee -a "$SUMMARY"
echo "" | tee -a "$SUMMARY"

for board in "${BOARDS[@]}"; do
  rm -rf "projects/${board}"
  LOG="$LOG_DIR/${board}.log"
  START=$(date +%s)
  .venv/bin/python -m orchestrator run \
    --requirements "test/requirements/${board}.json" \
    --project "$board" \
    --no-thinking --agent-mode --skip-approval > "$LOG" 2>&1
  EXIT=$?
  ELAPSED=$(( $(date +%s) - START ))

  # Pull key metrics from the log
  ROUTED=$(grep -oE "Routed: [0-9]+/[0-9]+ nets \([0-9.]+%\)" "$LOG" | tail -1)
  DRC=$(grep -oE "DRC: (PASSED|FAILED) — [^$]*" "$LOG" | tail -1)
  STEP1FAIL=$(grep -c "Phase 3.*invalid\|too short and invalid\|BLOCKED" "$LOG")

  if [ $EXIT -eq 0 ]; then
    printf "PASS  %-26s %4ds  %s  %s\n" "$board" "$ELAPSED" "${ROUTED:-no-route}" "${DRC:-}" | tee -a "$SUMMARY"
  else
    LASTERR=$(grep -iE "error|failed|blocked|traceback" "$LOG" | tail -1 | cut -c1-80)
    printf "FAIL  %-26s %4ds  exit=%d  %s\n" "$board" "$ELAPSED" "$EXIT" "$LASTERR" | tee -a "$SUMMARY"
  fi
done

echo "" | tee -a "$SUMMARY"
echo "finished $(date)" | tee -a "$SUMMARY"
echo "logs: $LOG_DIR" | tee -a "$SUMMARY"
