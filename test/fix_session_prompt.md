# PCB Creator Pipeline Fix Session

## Context
A systematic test of 10 boards (Arduino-style and accessory boards) was run using `openrouter/qwen/qwen3.5-27b` and local `Qwen3.5-27B-MLX-7bit` via oMLX. Full results are in `test/test_catalog.md`. No board completed the full pipeline end-to-end. The issues below are prioritized by impact.

## Issues to Fix (Priority Order)

### 1. Router bug — `channel_pressure` NameError (HIGH)
- **File:** `optimizers/router.py:3478` in `_fine_grid_retry()`
- **Problem:** `channel_pressure` variable is referenced but never defined in scope. Should call `_build_channel_pressure()` first or pass it as a parameter.
- **Impact:** Crashes Step 4 routing for any board that falls back to the built-in router's fine-grid retry path. Blocked 2/10 boards (ADS1115, Soil Moisture).

### 2. Freerouting email popup blocks automation (HIGH)
- **Problem:** Freerouting opens a GUI popup asking for an email address on launch. It has a button to continue without entering one, but since the pipeline runs headlessly, no one clicks it → 300s timeout every time.
- **Impact:** Freerouting NEVER completes. Every board wastes 5 minutes on the timeout before falling back to the built-in router.
- **Fix options:** Pass a CLI flag to skip the popup (check Freerouting docs for `--no-gui` or similar), or use xdotool/AppleScript to auto-dismiss, or pipe stdin, or find a headless Freerouting JAR.

### 3. Approval gate blocks automation (HIGH)
- **File:** `orchestrator/runner.py:302-318`
- **Problem:** `--agent-mode` triggers vision review using `anthropic/claude-sonnet-4-20250514`. When that model isn't available or the review "escalates," it falls through to `serve_approval_gate()` which opens an HTTP server on localhost and blocks waiting for a browser click.
- **Impact:** No board can complete Steps 5-6. Blocked 4/10 boards that successfully routed.
- **Fix:** Add a `--skip-approval` CLI flag that bypasses both vision review and browser approval. Or make vision review gracefully auto-approve when the vision model is unavailable.

### 4. No retry for API-level failures (MEDIUM)
- **File:** `orchestrator/steps/step_1_schematic.py` (and step_2, step_3)
- **Problem:** When OpenRouter returns an empty/whitespace response (`OpenrouterException - Unable to get json response`), it's treated as a fatal error. The rework loop only retries on validation failures, not API errors.
- **Impact:** 5/10 boards hit this on OpenRouter. Intermittent — often works on retry.
- **Fix:** Wrap LLM calls in a retry loop (2-3 attempts) for API-level exceptions before counting it as a rework attempt.

### 5. Step 3 Layout generation fragility (MEDIUM-HIGH)
- **Problem:** Layout is the most failure-prone step. 3/9 boards maxed out 5 rework attempts. Even simple 8-component boards needed 4 reworks. The model generates overlapping placements. Auto-repair fixes some but not all.
- **Impact:** 56% pass rate — worst of any step.
- **Details from Arduino Nano (27 parts, 45x18mm board):**
  - Attempt 1: 59 overlaps → auto-repair left 8
  - Attempt 2: 51 → 3
  - Attempt 3: 40 → 8
  - Attempt 4: 19 → 4
  - Attempt 5: 25 → 4
- **Fix options to evaluate:**
  - Dynamically increase board size when overlaps persist
  - More aggressive auto-repair (current: 667 iterations)
  - Feed auto-repair results back as rework context (show model which specific components overlap)
  - Constraint-based placement fallback (grid-based, no LLM)
  - Reduce placement precision requirements (snap to grid)

### 6. Silkscreen DRC defaults too small (LOW — easy fix)
- **Files:** Wherever silkscreen text defaults are set (likely in `exporters/` or `visualizers/`)
- **Problem:** Default "Rev 1.0" text height is 0.80mm (min 1.00mm) and stroke is 0.120mm (min 0.150mm). Every routed board fails this DRC check.
- **Fix:** Change defaults to height ≥ 1.0mm and stroke ≥ 0.15mm.

### 7. Missing `relay` component type in schema (LOW — easy fix)
- **File:** `schemas/circuit_schema.json` and `validators/` designator prefix rules
- **Problem:** No `relay` component type exists. Relays must use `type: "ic"` but the validator requires IC-type components to use `U` prefix. Relay convention is `K` prefix.
- **Impact:** Blocked 4ch relay board (36 components) — model correctly used `K1-K4` but validator rejected all 5 rework attempts.
- **Fix:** Either add `relay` to the type enum with allowed prefix `K`, or allow `K` as a valid prefix for `ic` type.

### 8. Package mismatch not caught by Python validator (LOW)
- **Problem:** Model substitutes resistor packages (e.g., 0805→1206) based on power calculations, ignoring requirements. QA catches this but Python validator passes, so QA is overridden.
- **Impact:** 3/10 boards have wrong packages propagated through the pipeline.
- **Fix:** Add package compliance check to the Python validator (compare output packages against requirements).

## Test Infrastructure
- Test requirements: `test/requirements/*.json` (10 files)
- Test catalog: `test/test_catalog.md`
- Batch runner: `test/run_all.sh` (uses `python3`, supports logging)
- Logs: `test/logs/`
- Run command: `python3 -m orchestrator run --requirements test/requirements/<slug>.json --project <slug> --no-thinking --agent-mode`
- Local model: `openai/Qwen3.5-27B-MLX-7bit` at `http://127.0.0.1:8000/v1` with key from `.env`
