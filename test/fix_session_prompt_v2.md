# PCB Creator Pipeline Fix Session — V2

## Context
V2 test results: **6/10 boards complete end-to-end** (up from 0/10 before V1 fixes). The remaining 4 failures have clear root causes identified below.

Full results: `test/test_catalog.md`

## Findings from Investigation

### Freerouting: Headless Fix WORKS ✅
The `-Djava.awt.headless=true` fix is successful. Freerouting now actually runs and routes boards:
- 555 Blinker: 6/7 nets (86%)
- ADS1115: 3/9 nets (33%)
- NeoPixel: 4/5 nets (80%)
- RS485: 7/8 nets (88%)
- L298N: 12/13 nets (92%)
- LM2596: 4/5 nets (80%)

The built-in router handles remaining nets. No more 300s timeouts.

### Soil Moisture Regression: Designator Prefix Issue
- **Root cause:** Validator only allows `R` prefix for `resistor` type. The potentiometer `RV1` uses `RV` prefix which is correct per IEC 60617 convention, but the validator rejects it.
- **File:** `validators/validate_netlist.py:41` — `"resistor": ["R"]` needs `"RV"` added
- The model correctly preserves `RV1` from the requirements (in V1 it was renaming to `R5`, which we flagged as a bug — now the model does the right thing but the validator punishes it)
- Same 1 error on all 5 rework attempts — model can't "fix" what isn't wrong

### 4ch Relay: Prompt Template Missing `relay` Type
- **Root cause:** The LLM prompt templates list allowed `component_type` values but don't include `relay`. The model sees `type: "relay"` in requirements but generates `type: "ic"` because the prompt says only those types are allowed.
- The schema and validator were updated to accept `relay` + `K` prefix, but the LLM doesn't know.

## Issues to Fix (Priority Order)

### 1. Add `RV` prefix for variable resistors (QUICK — 1 min)
- **File:** `validators/validate_netlist.py:41`
- **Change:** `"resistor": ["R"]` → `"resistor": ["R", "RV"]`
- **Impact:** Unblocks soil moisture board (+1 board, 7/10)

### 2. Add `relay` to prompt templates (QUICK — 2 min)
- **Files:**
  - `orchestrator/prompts/templates/schematic_generate.md.j2:24` — Add `relay` to allowed component_type list
  - `orchestrator/prompts/templates/schematic_rework.md.j2:56` — Same
  - `orchestrator/prompts/templates/schematic_generate_components.md.j2:13` — Same
- **Also add designator guidance:** Mention that relays use `K` prefix and variable resistors use `RV` prefix
- **Impact:** Unblocks 4ch relay board (+1 board, 8/10)

### 3. Step 3 layout improvement for complex boards (MEDIUM — architectural)
- **Boards affected:** ESP8266 (15 parts), Arduino Nano (27 parts)
- **Current behavior:** LLM generates overlapping placements → auto-repair fixes some → QA rejects → repeat 5 times → auto-grow board +20% → still fails
- **Potential approaches (evaluate tradeoffs):**
  - **A) Better error feedback:** Currently the rework prompt shows generic QA rejection. Instead, list the specific overlapping component pairs and their coordinates so the model can fix them precisely.
  - **B) Grid-snap placement:** Pre-compute a grid based on component sizes, snap LLM output to grid positions to reduce overlaps.
  - **C) Constraint-based fallback:** After 5 LLM failures, switch to a deterministic placement algorithm (e.g., force-directed or simulated annealing from scratch without LLM seed).
  - **D) Two-phase placement:** LLM places ICs and connectors first (fewer, larger), then a second pass places passives near their connected ICs.
  - **E) Reduce board density:** Auto-increase board size _before_ first attempt based on total component area + margin, not just as a fallback after failure.
- **Impact:** Would unblock ESP8266 and Arduino Nano (+2 boards, 10/10)

### 4. DRC / Router quality (LOWER PRIORITY — doesn't block completion)
- All 6 completed boards have DRC violations (1-30 errors each)
- Most common: trace-pad shorts (5/6 boards), trace clearance violations
- Freerouting helps (routes 33-92% of nets) but built-in router causes most violations
- **Not blocking** — boards produce valid Gerber output regardless of DRC warnings

## Test Commands
```bash
# Re-run soil moisture after RV prefix fix:
python3 -m orchestrator run --requirements test/requirements/test_soil_moisture.json --project test_soil_moisture --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval

# Re-run 4ch relay after prompt template fix:
python3 -m orchestrator run --requirements test/requirements/test_4ch_relay_module.json --project test_4ch_relay_module --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval

# Re-run ESP8266 after layout improvements:
python3 -m orchestrator run --requirements test/requirements/test_esp8266_breakout.json --project test_esp8266_breakout --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval

# Re-run Arduino Nano after layout improvements:
python3 -m orchestrator run --requirements test/requirements/test_arduino_nano.json --project test_arduino_nano --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval
```

## File Reference
- Validator designator map: `validators/validate_netlist.py:40-57`
- Schematic prompt template: `orchestrator/prompts/templates/schematic_generate.md.j2:24`
- Schematic rework template: `orchestrator/prompts/templates/schematic_rework.md.j2:56`
- Chunked component template: `orchestrator/prompts/templates/schematic_generate_components.md.j2:13`
- Layout step: `orchestrator/steps/step_3_layout.py`
- Test requirements: `test/requirements/*.json` (4ch relay already uses `type: "relay"`)
- Test catalog: `test/test_catalog.md`
