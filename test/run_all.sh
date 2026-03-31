#!/bin/bash
# Run all 10 test boards through the PCB creator pipeline
# Model: openrouter/qwen/qwen3.5-27b (default)
# Logs: test/logs/

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$SCRIPT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$LOG_DIR"

BOARDS=(
  test_555_blinker
  test_esp8266_breakout
  test_ads1115_breakout
  test_l298n_motor_driver
  test_lm2596_buck
  test_4ch_relay_module
  test_neopixel_driver
  test_rs485_converter
  test_soil_moisture
  test_arduino_nano
)

echo "=== PCB Creator Test Suite ==="
echo "Timestamp: $TIMESTAMP"
echo "Model: openrouter/qwen/qwen3.5-27b"
echo "Boards: ${#BOARDS[@]}"
echo ""

PASSED=0
FAILED=0

for board in "${BOARDS[@]}"; do
  echo "--- Running: $board ---"
  START=$(date +%s)
  LOG_FILE="$LOG_DIR/${board}_${TIMESTAMP}.log"

  cd "$PROJECT_DIR"
  python3 -m orchestrator run \
    --requirements "test/requirements/${board}.json" \
    --project "$board" \
    --no-thinking \
    --agent-mode \
    2>&1 | tee "$LOG_FILE"

  EXIT_CODE=${PIPESTATUS[0]}
  END=$(date +%s)
  ELAPSED=$((END - START))

  if [ $EXIT_CODE -eq 0 ]; then
    echo "  RESULT: PASS (${ELAPSED}s)"
    PASSED=$((PASSED + 1))
  else
    echo "  RESULT: FAIL (${ELAPSED}s, exit=$EXIT_CODE)"
    FAILED=$((FAILED + 1))
  fi
  echo ""
done

echo "=== Summary ==="
echo "Passed: $PASSED / ${#BOARDS[@]}"
echo "Failed: $FAILED / ${#BOARDS[@]}"
echo "Logs: $LOG_DIR/*_${TIMESTAMP}.log"
