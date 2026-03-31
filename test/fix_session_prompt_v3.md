# PCB Creator Pipeline Fix Session — V3

## Context
V3 test results: **8/10 boards complete end-to-end** (up from 6/10 in V2, 0/10 before fixes). Only 2 boards remain:

Full results: `test/test_catalog.md`

## Issues to Fix

### 1. BOM schema missing `relay` type (QUICK — should be 1 line)
- **Problem:** `relay` was added to the circuit netlist schema, validator designator map, and LLM prompt templates — but NOT to the BOM validation schema. The BOM step rejects `component_type: "relay"` as invalid.
- **Board affected:** 4ch relay module (36 components). Step 1 now passes (relay fix worked!) but Step 2 BOM fails after 5 reworks.
- **Model behavior:** Oscillates between outputting `relay` (BOM schema rejects it) and `switch` (BOM validator rejects it as type mismatch vs netlist).
- **Fix:** Find the BOM schema's `component_type` enum and add `relay`. Check all schemas/validators for component_type enums to ensure `relay` is in ALL of them. There may be a shared enum or multiple copies.
- **Files to check:**
  - `orchestrator/steps/step_2_bom.py` — BOM validation logic
  - `schemas/circuit_schema.json` — may have BOM-related schemas
  - Any BOM-specific schema definition
  - Also check the layout step schema (`step_3_layout.py`) for same issue
- **Impact:** Unblocks 4ch relay → 9/10

### 2. Arduino Nano deterministic layout — 1 overlap remains (MEDIUM)
- **Problem:** 27 components on 45×18mm board. The deterministic grid fallback places on a 58.5×23.4mm board (+30%), then SA repair runs 10,000 iterations. Result: 3 violations → 1 remaining overlap.
- **Board:** Arduino Nano clone (ATmega328P TQFP-32, CH340G SOIC-16, 2 crystals, USB, 11 caps, etc.)
- **The near-miss:** Just 1 overlap after 10k SA iterations on a 30%-larger board. Needs a small nudge.
- **Fix options (try in order):**
  - **A)** Increase deterministic fallback board margin from 30% to 50% for boards with >20 components
  - **B)** Increase SA iteration limit from 10,000 to 20,000 for the deterministic fallback path
  - **C)** Make the deterministic grid spacing wider (currently unknown — check `_generate_deterministic_placement`)
  - **D)** If 1 violation is only a boundary warning (not a component overlap), consider relaxing the zero-tolerance policy for the fallback path
- **File:** `orchestrator/steps/step_3_layout.py` — look for `_generate_deterministic_placement` and the fallback chain
- **Impact:** Unblocks Arduino Nano → 10/10

### 3. Router trace width minimum (LOWER PRIORITY — DRC quality)
- **Problem:** 4/8 completed boards have `trace_width_min` DRC violations — the built-in router uses 0.15mm traces which is below the 0.2mm DFM minimum.
- **Fix:** Set the router's default trace width to ≥ 0.2mm
- **File:** `optimizers/router.py` — look for trace width defaults/configuration
- **Impact:** Reduces DRC errors on all boards but doesn't affect completion rate

## Test Commands
```bash
# After fixing BOM relay:
python3 -m orchestrator run --requirements test/requirements/test_4ch_relay_module.json --project test_4ch_relay_module --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval

# After fixing Arduino Nano layout:
python3 -m orchestrator run --requirements test/requirements/test_arduino_nano.json --project test_arduino_nano --model "openai/Qwen3.5-27B-MLX-7bit" --api-base "http://127.0.0.1:8000/v1" --api-key 'D#D?[tC1)6(qX564R5p0mCYxg' --no-thinking --skip-approval
```

## Notes
- The soil moisture requirements file (`test/requirements/test_soil_moisture.json`) was fixed during V3 testing — R1.2 was in two separate nets (SENSOR_MID and AOUT) which is invalid. Merged AOUT's J1.3 pin into SENSOR_MID net.
- Freerouting headless mode is working — routes 33-92% of nets across boards before built-in router takes over.
- The local oMLX model (`Qwen3.5-27B-MLX-7bit`) is reliable for testing but slow (~5-10 min per LLM call with 4 concurrent boards).
