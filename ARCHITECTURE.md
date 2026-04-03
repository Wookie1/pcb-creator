# PCB-Creator Architecture

## Philosophy

PCB-Creator is an AI-driven PCB design tool where **Python controls the process and LLMs do the creative work**. Every verifiable check is deterministic Python; LLMs handle translation, generation, and subjective review. This means:

- LLM outputs are always validated immediately by Python
- Failures trigger automatic rework loops with specific error feedback
- Engineering constants live in one Python file, not scattered across prompts
- The pipeline is reproducible — same inputs produce consistent validation

## System Overview

```
User Input (natural language or JSON)
       │
       ▼
┌─────────────────────────────────┐
│  Step 0: Requirements Gathering │
│  ┌───────────┐  ┌────────────┐  │
│  │ LLM       │→ │ User       │  │  LLM proposes design, user
│  │ plan      │  │ answers    │  │  confirms or overrides defaults
│  └───────────┘  └────────────┘  │
│  ┌───────────┐  ┌────────────┐  │
│  │ LLM       │→ │ Python     │  │  LLM translates (with plan
│  │ translate  │  │ validate   │  │  context), Python validates
│  └───────────┘  └────────────┘  │
│  ┌───────────┐  ┌────────────┐  │
│  │ Tiered    │→ │ Python     │  │  Curated → cache → LLM fallback
│  │ lookup    │  │ enrich     │  │  (parallel LLM for remaining)
│  └───────────┘  └────────────┘  │
│  ┌───────────┐                  │
│  │ LLM       │→ User Approval   │  LLM summarizes, user confirms
│  │ summarize  │                  │
│  └───────────┘                  │
└─────────────────────────────────┘
       │ requirements.json
       ▼
┌─────────────────────────────────┐
│  Step 1: Schematic / Netlist    │
│  ┌───────────┐  ┌────────────┐  │
│  │ LLM       │→ │ Python     │  │  LLM generates netlist JSON
│  │ generate   │  │ extract    │  │  Python extracts and parses
│  └───────────┘  └────────────┘  │
│       │                         │
│       ▼                         │
│  ┌────────────────────────────┐ │
│  │ Python Validator           │ │  Schema + referential integrity
│  │  → Schema validation       │ │  + 9 DRC checks
│  │  → Referential integrity   │ │
│  │  → DRC checks (9 checks)  │ │  Errors → rework loop
│  └────────────────────────────┘ │
│       │ (if valid)              │
│       ▼                         │
│  ┌───────────┐                  │
│  │ LLM       │→ QA report       │  Requirements compliance check
│  │ QA review  │                  │  Issues → rework loop
│  └───────────┘                  │
│                                 │
│  Rework loop: up to 5 attempts  │
└─────────────────────────────────┘
       │ netlist.json
       ▼
┌─────────────────────────────────┐
│  Step 2: Component Selection    │
│  Same pattern: LLM generate →  │
│  Python validate → LLM QA      │
└─────────────────────────────────┘
       │ bom.json
       ▼
┌─────────────────────────────────┐
│  Step 3: Board Layout           │
│  LLM placement → Python DRC    │
│  → SA Repair (if overlaps)     │
│  → LLM QA → SA Optimizer      │
│  → Fiducials                    │
└─────────────────────────────────┘
       │ placement.json
       ▼
┌─────────────────────────────────┐
│  Step 4: Routing (no LLM)      │
│  Freerouting (default) or A*   │
│  → IPC-2221 trace widths       │
│  → Copper fill + stitching     │
│  → Silkscreen generation       │
└─────────────────────────────────┘
       │ routed.json
       ▼
┌─────────────────────────────────┐
│  Step 5: DRC                    │
│  Electrical + DFM + Current     │
│  Checked against mfg profile    │
└─────────────────────────────────┘
       │ drc_report.json
       ▼
┌─────────────────────────────────┐
│  Review & Approval Gate         │
│  Interactive board viewer       │
│  Export/Import KiCad buttons    │
│  DRC results + per-net stats    │
│  [Continue to Output] button    │
└─────────────────────────────────┘
       │
       ▼
   Step 6: Output Generation
```

## Directory Structure

```
pcb-creator/
├── mcp_server.py               # MCP server for AI agent integration (FastMCP, stdio)
├── orchestrator/               # Python orchestration engine
│   ├── cli.py                  # CLI: run, design, gui, validate, import-kicad, mcp
│   ├── runner.py               # Sequential step executor + streaming generator (Steps 0-6)
│   ├── gradio_app.py           # Gradio web GUI (chat, viewer, settings, progress)
│   ├── config.py               # Model, router engine, paths, limits, agent_mode, tiered lookup
│   ├── cache.py                # Thread-safe JSON cache for resolved footprints + specs
│   ├── project.py              # Project directory & file I/O
│   ├── approval_server.py      # Ephemeral HTTP server for CLI approval gate
│   ├── vision_review.py        # Vision-based autonomous board review (agent mode)
│   ├── steps/
│   │   ├── base.py             # StepBase abstract class, StepResult
│   │   ├── step_0_requirements.py  # Validate + calculate + copy attachments
│   │   ├── step_1_schematic.py     # Generate + validate + QA netlist
│   │   ├── step_2_bom.py           # Generate + validate + QA BOM
│   │   └── step_3_layout.py        # Generate + validate + repair + QA placement
│   ├── gather/
│   │   ├── conversation.py     # Interactive requirements gathering + tiered enrichment
│   │   ├── curated_specs.py    # Curated lookup tables (ICs, LEDs, transistors, footprint dims)
│   │   ├── easyeda_lookup.py   # EasyEDA/LCSC API footprint + spec fetcher
│   │   ├── calculator.py       # LED resistor / power calculations
│   │   └── schema.py           # Requirements JSON schema + LLM type coercion
│   ├── llm/
│   │   ├── base.py             # LLMClient abstract base
│   │   └── litellm_client.py   # litellm provider (with continuation, api_base/key)
│   └── prompts/
│       ├── builder.py          # Jinja2 template renderer
│       └── templates/          # Prompt templates (*.md.j2)
├── optimizers/                 # Algorithmic optimization & routing
│   ├── ratsnest.py             # Connectivity, MST, crossings, component associations
│   ├── fiducials.py            # Fiducial marker placement (2 per populated side)
│   ├── placement_optimizer.py  # SA optimizer (wire length, crossings, decoupling, crystal, grouping)
│   ├── pad_geometry.py         # Pad position database (tiered: KiCad → IPC-7351 → cache → built-in)
│   ├── ipc7351.py              # IPC-7351B parametric footprint generator (QFN, BGA, SOP, etc.)
│   ├── freerouter.py           # Freerouting integration (auto-download, DSN→SES flow)
│   └── router.py               # Built-in A* router + IPC-2221 + copper fills + silkscreen
├── exporters/                 # Board export/import
│   ├── kicad_exporter.py      # Export routed JSON → .kicad_pcb (KiCad 9)
│   ├── kicad_importer.py      # Import .kicad_pcb → routed JSON (S-expression parser)
│   ├── kicad_mod_parser.py    # Parse .kicad_mod footprint files → FootprintDef + library index
│   ├── dsn_exporter.py        # Export placement → Specctra DSN (for Freerouting)
│   ├── ses_importer.py        # Import Specctra SES → routed JSON (from Freerouting)
│   ├── component_heights.py   # Package height lookup table for parametric 3D models
│   ├── parametric_models.py   # Generate STEP BREP box shapes for components
│   └── model_fetcher.py       # Fetch 3D STEP models from LCSC/EasyEDA with caching
├── validators/                 # Deterministic validation
│   ├── validate_netlist.py     # Schema + referential integrity + DRC
│   ├── validate_placement.py   # Schema + cross-ref + boundary + overlap + rules
│   ├── validate_routing.py     # Schema + trace clearance + via clearance + connectivity
│   ├── drc_checks.py           # 9 netlist-level design rule checks
│   ├── drc_checks_dfm.py       # 8 DFM checks (trace width, via, annular ring, current capacity)
│   ├── drc_report.py           # Consolidated DRC report (electrical + DFM + current + mechanical)
│   └── engineering_constants.py # Constants, DFM profiles (JLCPCB, OSH Park, PCBWay, generic)
├── visualizers/                # Board visualization tools
│   ├── placement_viewer.py     # Interactive viewer (routing, fills, DRC, export/import, approval)
│   └── netlist_viewer.py       # Schematic-style block diagram (components, pins, nets)
├── schemas/
│   ├── circuit_schema.json     # Netlist JSON Schema (draft-07)
│   ├── bom_schema.json         # BOM JSON Schema
│   ├── placement_schema.json   # Placement JSON Schema
│   └── routed_schema.json      # Routed JSON Schema (traces + vias + silkscreen)
├── tests/
│   ├── test_drc_checks.py          # 22 unit tests for DRC
│   ├── test_validate_placement.py  # 24 unit tests for placement validator
│   ├── test_placement_optimizer.py # 29 unit tests for optimizer/repair/fiducials
│   ├── test_switch_led.json        # Simple test fixture (5 components)
│   ├── test_attiny_pot_led.json    # Medium test (6 components + IC)
│   ├── test_arduino_uno.json       # Complex test (21 components + DXF)
│   └── arduino_uno_outline.dxf     # DXF board outline for Arduino test
├── projects/                   # Generated project outputs
│   └── {project_name}/
│       ├── {name}_requirements.json   # Step 0: structured requirements
│       ├── {name}_netlist.json        # Step 1: flat netlist (components, ports, nets)
│       ├── {name}_bom.json            # Step 2: bill of materials
│       ├── {name}_placement.json      # Step 3: component positions
│       ├── {name}_routed.json         # Step 4: traces, vias, fills, silkscreen
│       ├── {name}_drc_report.json     # Step 5: DRC results
│       └── output/                    # Step 6: manufacturer files
│           ├── {name}-F_Cu.gbr        #   front copper
│           ├── {name}-B_Cu.gbr        #   back copper
│           ├── {name}-F_SilkS.gbr     #   front silkscreen
│           ├── {name}-F_Mask.gbr      #   front solder mask
│           ├── {name}-B_Mask.gbr      #   back solder mask
│           ├── {name}-F_Paste.gbr     #   front paste stencil
│           ├── {name}-Edge_Cuts.gbr   #   board outline
│           ├── {name}.drl             #   Excellon drill file
│           ├── {name}_bom.csv         #   BOM (JLCPCB format)
│           ├── {name}_cpl.csv         #   pick-and-place (JLCPCB CPL format)
│           ├── {name}_board.step      #   populated PCB 3D model (board + components)
│           └── {name}_gerbers.zip     #   all Gerbers + drill for upload
├── pcb-creator                 # Launcher script (auto-uses .venv python)
├── .env                        # API keys: PCB_LLM_API_KEY, OPENAI_API_KEY (gitignored)
├── STANDARDS.md                # Master standards (injected into prompts)
├── FLOW.md                     # Step definitions and workflow rules
├── AGENTS.md                   # Agent role descriptions
└── VISUALIZATION.md            # Data format reference for custom visualizations
```

## Data Flow

### Netlist Schema

The netlist is a flat array of typed elements — not nested. This makes validation straightforward:

```json
{
  "version": "1.0",
  "project_name": "example",
  "elements": [
    { "element_type": "component", "component_id": "comp_r1", ... },
    { "element_type": "port", "port_id": "port_r1_1", "component_id": "comp_r1", ... },
    { "element_type": "net", "net_id": "net_vcc", "connected_port_ids": ["port_r1_1", ...], ... }
  ]
}
```

**Three element types:**
- **component** — Physical part (resistor, IC, connector). Has designator, value, package, optional properties.
- **port** — A pin on a component. Has pin_number, name, electrical_type (power_in/power_out/signal/ground/passive/no_connect).
- **net** — Named connection between 2+ ports. Has net_class (power/ground/signal).

**Key constraint:** Every port appears in exactly one net (unless `no_connect`). This ensures the netlist is fully constrained.

### Requirements Format

Requirements use a simpler format with component refs and pin-name connections:

```json
{
  "project_name": "led_blink",
  "power": { "voltage": "5V", "source": "2-pin header" },
  "components": [
    { "ref": "R1", "type": "resistor", "value": "220ohm", "package": "0805" }
  ],
  "connections": [
    { "net_name": "VCC", "net_class": "power", "pins": ["J1.1", "R1.1"] }
  ]
}
```

The LLM's job in Step 1 is to expand this into the full netlist with explicit ports and proper IDs.

## Validation Architecture

### Three-Layer Validation

Validation runs in order — each layer gates the next:

**Layer 1: JSON Schema** — Structural conformance against `circuit_schema.json`. Catches missing fields, wrong types, invalid patterns.

**Layer 2: Referential Integrity** — Cross-element consistency:
- Every port's `component_id` references a real component
- Every net's `connected_port_ids` reference real ports
- Every component has at least one port
- Designator prefixes match component types (R→resistor, D→led, etc.)
- Sequential numbering within each prefix (no gaps)
- No duplicate IDs, designators, or pin numbers within a component
- Warns on unconnected ports

**Layer 3: DRC (Design Rule Checks)** — Electrical correctness:

| # | Check | Error | Warning | Needs V_supply |
|---|-------|:-----:|:-------:|:--------------:|
| 1 | Single-pin nets (duplicate ports) | X | | |
| 1 | Single-pin nets (same component) | | X | |
| 2 | Duplicate nets | X | | |
| 3 | Ground net with power_out pin | X | | |
| 3 | Net class / pin type mismatch | | X | |
| 4 | Multiple power_out on same net | X | | |
| 5 | Extreme component values | | X | |
| 6 | IC missing decoupling cap | | X | |
| 7 | Resistor power exceeds rating | X | | X |
| 7 | Resistor power near rating | | X | X |
| 8 | Cap below voltage derating | X | | X |
| 9 | Power budget report | | X | X |

Checks 7-9 require `V_supply` from the requirements JSON (`--requirements` flag).

### Resistor Power Calculation

For resistors in series with LEDs, the DRC computes actual operating current rather than using the LED's maximum rated current:

```
I_actual = (V_supply - V_forward) / R
P = I_actual² × R
```

LED detection: trace nets from resistor ports, skip power/ground nets, verify the connected LED pin is the anode (not cathode).

For resistors NOT in series with LEDs, worst-case: `P = V_supply² / R`.

### Shared Engineering Constants

`validators/engineering_constants.py` is the single source of truth for all numeric values used by both the Python validator and the calculator:

- Package power ratings (0402 through 2512)
- LED forward voltage defaults by color
- Voltage derating factors (ceramic: 1.5×, electrolytic: 2×)
- Resistor power derating (2× safety margin)
- Value parsing functions: `parse_voltage()`, `parse_current()`, `parse_resistance()`, `parse_capacitance()`

The LLM-facing `engineering_rules.md` contains the same rules as prose. The Python enforces them — the LLM prompt is guidance, not the gate.

## LLM Integration

### Model Selection

Default: `openrouter/qwen/qwen3.5-27b` — 27B dense model, good balance of speed and quality. Handles ~30KB JSON output in a single response.

Also tested: `openrouter/x-ai/grok-4.1-fast` (fast, reliable structured output), local `Qwen3.5-27B-MLX-7bit` via oMLX (works with SA repair for complex boards).

Override per-run: `--model openrouter/...` or `--model openai/<name> --api-base http://localhost:8000/v1` for local models. Environment variables: `PCB_GENERATE_MODEL`, `PCB_REVIEW_MODEL`, `PCB_GATHER_MODEL`.

### Output Continuation

If the LLM's response is truncated (`finish_reason: "length"`), the client automatically appends the partial response as an assistant message and asks "Continue exactly where you left off." Up to 4 continuations, then returns whatever was accumulated.

### JSON Extraction

LLM responses may contain markdown fences, preamble text, or continuation artifacts. `extract_json()` tries three strategies in order:
1. Direct parse (clean JSON)
2. Extract from markdown `` ```json ... ``` `` fences
3. Find outermost `{` to `}` (handles extra text around JSON)

### Prompt Templates

Jinja2 templates in `orchestrator/prompts/templates/`:

| Template | LLM Role | Input | Output |
|----------|----------|-------|--------|
| `gather_plan` | Design consultant | Natural language | Design plan + questions JSON |
| `gather_translate` | Translator | Natural language + plan context | Requirements JSON |
| `gather_datasheet` | Datasheet lookup | Component info | Specs JSON (LLM fallback only) |
| `gather_footprint` | Footprint lookup | Component info | Footprint dims JSON (LLM fallback only) |
| `gather_summarize` | Technical writer | Requirements JSON | Markdown summary |
| `schematic_generate` | Schematic engineer | Requirements + rules | Netlist JSON |
| `schematic_rework` | Schematic engineer | Previous output + errors | Fixed netlist JSON |
| `bom_generate` | Component engineer | Netlist + rules | BOM JSON |
| `bom_rework` | Component engineer | Previous output + errors | Fixed BOM JSON |
| `layout_generate` | Layout engineer | Netlist + BOM + board config + hints | Placement JSON |
| `layout_rework` | Layout engineer | Previous output + errors | Fixed placement JSON |
| `qa_review` | QA engineer | Step output + requirements + validator output | QA report JSON |

Templates are injected with STANDARDS.md sections and engineering rules via `PromptBuilder`.

## Requirements Gathering

### Interactive Flow (`pcb-creator design`)

```
User describes circuit
      │
      ▼
LLM plans design (gather_plan)
  → summarizes understanding
  → proposes concrete design with defaults
  → asks 2-5 clarifying questions
  → system injects verified component specs
      │
      ▼
User reviews plan
  ├─→ types corrections → LLM re-plans (loop)
  └─→ clicks Proceed / presses Enter
      │
      ▼
LLM translates to JSON (gather_translate, with plan context)
      │
      ▼
Python validates schema
      │
      ▼
Tiered spec enrichment
                    1. Curated table (ATmega328P, NE555, etc.)
                    2. Local cache (~/.pcb-creator/component_cache.json)
                    3. LLM fallback (parallel, results cached)
                              │
                              ▼
                    Tiered footprint enrichment
                    1. Curated dims table
                    2. Local cache
                    3. EasyEDA/LCSC API (if available)
                    4. LLM fallback (parallel, results cached)
                              │
                              ▼
                    Python runs engineering calculations
                    (LED resistor values, power dissipation)
                              │
                              ▼
                    LLM summarizes for user
                              │
                              ▼
                    User approves or gives feedback
                              │
                    ┌─────────┴─────────┐
                    │ Approved          │ Feedback
                    ▼                   ▼
              Run pipeline        Loop with context
```

### Design Planning Step (Multi-Turn)

Before translation, the LLM analyzes the user's circuit description and produces a design plan (`gather_plan.md.j2`). The plan includes:

- **Understanding** — one paragraph summarizing the circuit's purpose and functional blocks
- **Proposed design** — concrete choices for power source, packages, board size, component list, and engineering notes (decoupling caps, pull-ups, etc.)
- **Clarifying questions** — 2-5 focused questions about genuinely ambiguous aspects, each with a sensible default

**Multi-turn conversation:** The planning phase is a loop, not a single round. After seeing the plan, the user can:
- Answer questions
- Suggest design changes (e.g., "use WS2812B instead of regular LEDs")
- Press Enter / click "Proceed to Design" when satisfied

Each round, the system automatically looks up specs for components mentioned in the plan from the curated tables and cache (`_inject_specs_for_plan`). If real specs are found (pin counts, voltages, pinouts), they're injected into the conversation as `[Verified Component Data]` messages so the LLM can incorporate them. This grounds the plan in real data rather than LLM guesses.

The multi-turn conversation is simulated over the single-turn LLM client by accumulating a `conversation_history` (user messages, assistant plans, system spec injections) and rendering the full history into the prompt each round.

**GUI flow:** The plan appears as a chat message with a "Proceed to Design ➜" button. The user can type corrections and click Send for another round, or click Proceed when ready. The `"planning"` phase loops until proceed.

**CLI flow:** Same loop via `input()`. User presses Enter to proceed or types corrections.

**Agent mode:** Uses single-round `_plan()` — the agent handles iteration externally.

**Graceful degradation:** If the planning LLM call fails, the flow falls through to translation without plan context — identical to the pre-planning behavior. Mid-conversation failures keep the last good plan.

### Tiered Component Enrichment

During gather only (not during validation), missing component specs and footprint dimensions are resolved through a tiered lookup that minimizes LLM calls. This keeps the validator pure Python with no LLM dependencies.

**Spec enrichment triggers:**
- LEDs missing `vf` → look up forward voltage
- ICs missing `pin_count` or `pinout` → look up pin count and pin mapping
- Capacitors missing `voltage_rating` → look up rating
- Transistors missing `vce_max`/`vds_max` → look up ratings

**Spec resolution tiers:**
1. **Curated table** (`gather/curated_specs.py`) — ~30 common ICs (ATmega328P, NE555, LM7805, 74HC595, etc.), LED specs by color, transistor specs (2N2222, IRF540N, etc.), capacitor defaults. Instant, no I/O.
2. **Local cache** (`~/.pcb-creator/component_cache.json`) — results from prior LLM calls. Keyed by `type:value:package`.
3. **LLM fallback** — only fires for components not resolved by tiers 1-2. Remaining calls run in parallel via `ThreadPoolExecutor` (configurable workers via `PCB_LLM_ENRICHMENT_WORKERS`, default 4). Results cached with `source: "llm"` and `needs_review: true`.

**Footprint dimension resolution tiers:**
1. **Curated table** — ~35 package entries (QFN, SSOP, TSSOP, MSOP, LQFP, USB, SOT-223, etc.)
2. **Local cache** — prior results keyed by `footprint_dims:{package}`
3. **EasyEDA/LCSC API** — fetches real footprint data via `easyeda2kicad` (optional dependency), converts pin positions to bounding box. Rate-limited to 1 req/s.
4. **LLM fallback** — parallel, results cached

**Cache format:** Single JSON file with `footprints` and `specs` sections. Each entry tracks `source` (curated/kicad/easyeda/llm), `resolved` date, and `needs_review` flag.

### Tiered Footprint Pad Geometry

`pad_geometry.py` resolves package names to exact pad positions (pin offsets + pad sizes) for placement, routing, and export. Uses a tiered lookup configured once at startup via `configure_lookup()`, which sets module-level defaults used by all 9+ call sites across the codebase.

**Footprint resolution tiers:**
1. **KiCad library** — parses `.kicad_mod` files from the official KiCad footprint library (~50K packages). Community-maintained, datasheet-verified. Requires `PCB_KICAD_LIBRARY_PATH` to point to the library root. Lazy index built on first lookup with short alias generation (`SOIC-8_3.9x4.9mm_P1.27mm` → also matches `SOIC-8`).
2. **IPC-7351B parametric** (`ipc7351.py`) — algorithmic footprint generation per the IPC land pattern standard. Covers QFN, DFN, SOP/SSOP/TSSOP/MSOP, SOT-223, SOT-89, and BGA families. Zero I/O cost.
3. **Local cache** — cached footprints from prior EasyEDA or LLM lookups.
4. **Built-in approximations** — hardcoded definitions (0402-1210, SOT-23, SOIC-8, TO-220, HC49, PJ-002A, 6mm tactile) and parametric generators (DIP-N, PinHeader 1xN/2xN, TQFP-N). Custom-built for this project; used as last local resort when no authoritative library is available.
5. *(caller-managed)* **EasyEDA API** → **LLM fallback** → **perimeter distribution** (absolute last resort)

**IC pinout resolution:** For complex ICs, the gather step returns the full pin-to-function mapping (e.g., ATmega328P pin 1 = PC6/RESET) in `specs.pinout`. The `validators/pinout.py` module parses these strings into structured `PinInfo` objects with inferred electrical types. During netlist validation, `_fix_pinout_from_requirements()` auto-corrects wrong pin names and electrical types before DRC runs. The `check_pinout_compliance` DRC check then flags any remaining mismatches (out-of-range pins, unresolvable name conflicts). The schematic generation prompt also receives a structured pinout table so the LLM has an unambiguous reference.

## Project Lifecycle

### Status Tracking

Every project has `STATUS.json` tracking progress through steps:

```
IN_PROGRESS → AWAITING_QA → COMPLETE
                    ↓
              QA_FAILED → REWORK_IN_PROGRESS → (retry)
                                    ↓
                              BLOCKED (after 5 attempts)
```

### Rework Loop

When validation or QA fails, the schematic engineer receives:
- The previous (failing) netlist
- Specific error messages from the validator or QA agent
- The original requirements

This gives the LLM targeted feedback to fix specific issues rather than regenerating from scratch.

## Step 2: Component Selection (BOM)

LLM generates a Bill of Materials from the netlist, specifying procurement-ready details:

- **Value** — exact rating (`10kohm`, `100nF`, `green`)
- **Specs** — tolerance, power rating, voltage rating, forward voltage/current, etc.
- **Description** — human-readable procurement description
- **Notes** — circuit context for each component

Validated against `schemas/bom_schema.json`. Cross-referenced with netlist for designator/type/package consistency.

## Step 3: Board Layout (Placement)

The most complex step — involves both LLM and algorithmic processing:

```
LLM generates placement (may have overlaps)
        │
        ▼
Python validator: schema + cross-ref + boundary + overlap + clearance
        │
   ┌────┴────┐
   │ Valid   │ Invalid (overlaps)
   │         ▼
   │    SA Repair Algorithm
   │    (resolve overlaps, ~1000-10000 iterations)
   │         │
   │         ▼
   │    Re-validate
   │         │
   │    ┌────┴────┐
   │    │ Valid   │ Still invalid → rework attempt
   │    ▼
   ▼
LLM QA review
   │
   ▼ (if passed)
SA Optimizer (minimize wire length + crossings)
   │
   ▼
Add fiducial markers (2 per populated layer)
   │
   ▼
Re-validate → Approval gate
```

### Placement Optimizer

Pure Python, zero LLM calls. Two modes:

- **Repair mode**: Resolves overlaps from invalid LLM placements. Cost function heavily penalizes violations (boundary + overlap), with wire length as secondary objective. Runs up to 10,000 iterations with stagnation limits of 1,500 (no violations) / 3,000 (general).
- **Optimize mode**: Improves valid placements. Minimizes wire length (MST-based ratsnest) and crossing count via simulated annealing. Iteration count auto-scales from component count (1000 × movable components, bounded [2000, 50000]). Early termination on stagnation.

Move types: translate (70%), swap same-package (15%), rotate (15%). Pinned components (user-placed, connectors, fiducials) excluded from moves.

**Board auto-sizing:** Before the first LLM attempt, board dimensions are checked against total component footprint area × 2.5. Default board sizes are auto-expanded if too small; user-specified sizes are warned but respected.

**Fallback chain** (after all LLM rework attempts exhausted):
1. Grow board 20%, re-run SA repair on last LLM placement
2. Generate deterministic grid-based placement on 30%-larger board (connectors on edges, largest components first), then SA repair

### Fiducials

2 fiducial markers per populated board side (top and/or bottom). Placed in diagonally opposite corners. 1mm copper dot with 2mm clearance (3mm total footprint). Exempt from netlist cross-reference validation.

### Attachments

Requirements can reference DXF board outlines, sketches, and photos via the `attachments` array. Files are copied to the project directory at Step 0. Later steps discover them via `used_by_steps`.

## Step 4: Routing

Purely algorithmic (no LLM). Takes placement.json + netlist.json, outputs routed.json.

**Routing engines:** Two engines available, configured via `PCB_ROUTER_ENGINE` env var or `config.router_engine`:

### Freerouting (default)

External autorouter via Specctra DSN/SES format exchange. Uses the Freerouting Java application (v2.1.0+, requires Java 17+).

**Workflow:** `placement.json + netlist.json` → DSN export → Freerouting JAR (headless) → SES import → copper fills → silkscreen → `routed.json`

- **DSN export** (`exporters/dsn_exporter.py`): Converts placement + netlist to Specctra DSN format. Board outline, component footprints with physical pad definitions (from `pad_geometry.py` tiered lookup), net connectivity, and design rules. GND excluded from routing (handled by copper fill).
- **SES import** (`exporters/ses_importer.py`): Parses Freerouting's session output. Extracts wire paths (traces) and vias, maps net names back to internal net_ids. Reuses S-expression parser from `kicad_importer.py`.
- **Orchestration** (`optimizers/freerouter.py`): Auto-downloads JAR to `~/.cache/pcb-creator/` on first use. Runs headlessly with `-mp 20` (max optimization passes). Falls back to built-in router on failure. Configurable timeout (default 300s).
- **Copper fills** (`router.py:apply_copper_fills()`): Standalone function that rebuilds a routing grid from the Freerouting output, marks existing traces/vias, then runs the standard fill algorithm. Removes the fill net (GND) from unrouted list and updates completion stats to 100%.

### Built-in Router (fallback)

Grid-based A* with 8-connected movement (orthogonal + 45° diagonal) on a 0.25mm grid. Used when `router_engine="builtin"` or when Freerouting fails.

**Net ordering:** Multi-trial optimization — tries multiple signal net orderings and keeps the best. Power/ground nets always route first.

**Multi-pass routing:**
1. **Pass 1** — 8-connected A* with normal clearance, best ordering from multi-trial search
2. **Rip-up-and-retry** — If a net fails, clear previously routed signal nets, route the failed net, re-route cleared nets
3. **Fine-grid retry** — Failed nets re-route on a 2x finer grid with narrower trace width (0.20mm min, above DFM minimum)
4. **Pass 2 (relaxed clearance)** — Remaining unrouted nets retry with 85% of normal clearance

**Trace sizing:** IPC-2221 auto-calculation from copper weight and estimated net current.

**Silkscreen generation:** Designator text labels, pin 1 dot indicators, anode "A" markers, board name and revision label. Silkscreen elements are filtered against exclusion zones (pads, fiducials, vias) to prevent overlaps. Board name/rev tries 5 candidate positions (corners + center) and picks the first non-colliding one.

**Validation:** 5 checks — schema, trace-to-trace clearance, via clearance, connectivity (union-find), no-shorts.

### Post-Routing Approval Gate

Two modes depending on how the pipeline is invoked:

**CLI mode** (`pcb-creator run`): After routing completes, the pipeline serves an interactive board viewer via an ephemeral HTTP server (`orchestrator/approval_server.py`) and blocks until the user approves in the browser.

**GUI mode** (`pcb-creator gui`): The Gradio UI itself provides the approval flow — the board viewer updates progressively, and Export/Import/Continue buttons appear in the Gradio interface. No ephemeral server needed.

**Agent mode** (`pcb-creator run --agent-mode`): Uses vision-based autonomous review (`orchestrator/vision_review.py`). The board is rendered to PNG via `cairosvg`, then sent with DRC/routing stats to a vision-capable LLM (configurable via `PCB_VISION_MODEL`, default `anthropic/claude-sonnet-4-20250514`). The LLM responds APPROVE or REQUEST_CHANGES. Pre-checks auto-approve if 100% routed with 0 DRC errors, and auto-escalate if routing is incomplete. After 3 failed review attempts (configurable via `PCB_VISION_MAX_ATTEMPTS`), auto-approves and continues. In GUI mode, escalation shows the approve button for manual approval.

**Skip-approval mode** (`pcb-creator run --skip-approval`): Bypasses all approval gates entirely — no vision review, no browser gate. Intended for batch testing and CI pipelines.

**Skip-QA mode** (`pcb-creator run --skip-qa`, or `PCB_SKIP_QA=true`): Bypasses per-step LLM QA reviews (Steps 1-3) and the post-routing vision review. Python validators still run — only the LLM review calls are skipped. Enabled by default in MCP server mode, where the calling agent reviews results itself via `get_project_status` and `get_board_image`. Roughly halves LLM calls and pipeline time.

**Viewer features:**
- Routed traces (color-coded by net, layered by copper layer)
- Copper fills (semi-transparent GND polygons)
- Through-hole pads (gold circles with drill holes) and SMD pads
- Per-net routing stats (collapsible table: net name, class, width, length, vias, connected components)
- Component hover tooltips (value, specs, position, package)
- Trace hover tooltips (net name, class, width, layer, segment length)

**Action buttons:**
- **Export KiCad** — client-side `.kicad_pcb` download for manual editing
- **Import KiCad** — upload edited `.kicad_pcb` back into the pipeline (server-side import via `import_kicad_pcb()`, page reloads with updated routing)
- **Continue to DRC & Export** — approve routing, server shuts down, pipeline resumes

When the viewer HTML is opened as a saved file (no server running), Import and Continue buttons show fallback CLI commands. Export always works client-side.

## Step 4b: Copper Fills (integrated into Step 4)

Copper fill floods unused board area with GND copper on **both layers**. Runs after routing completes within `route_board()`. PCB blanks come with copper on both sides — the fab etches away what isn't needed, so filling both layers costs nothing extra and provides a solid ground reference.

**Fill-first approach:** GND net is skipped during trace routing — it's "routed" by the copper pour. This frees routing space for signal/power traces.

**Algorithm:** Scanline fill on the existing routing grid:
1. Mark all EMPTY cells on both layers as fillable
2. Apply clearance mask (0.25mm) around all non-GND features
3. Apply thermal relief around GND pads (0.2mm gap, 4 cardinal spokes at 0.25mm width)
4. Add **stitching vias** on a ~5mm grid wherever fill exists on both layers — these connect the top and bottom ground planes into a unified reference
5. Add **rescue vias** — find top-layer fill islands disconnected from GND, drop a via at the island centroid where bottom fill exists, connecting the island through the bottom plane. Only rescues islands ≥4 cells to avoid saving tiny slivers.
6. **Cross-layer island removal** via BFS: seeds from GND pads on either layer, traverses stitching vias and rescue vias to reach bottom fill. Islands unreachable from any GND pad through same-layer adjacency or cross-layer vias are removed
7. Convert bitmap to merged rectangles (run-length encoding + vertical merge) for output

**Why stitching vias matter:** The bottom layer typically has no GND pads (SMD components are on top). Without stitching vias, the entire bottom fill would be removed as an island. Stitching vias provide cross-layer connectivity so the bottom plane stays connected to the top GND features.

**Output:** `copper_fills` array in routed.json with layer, net_id, net_name, and polygon vertex arrays. Stitching vias appear in the `vias` array alongside routing vias.

**Validator:** Connectivity check recognizes fill-connected pads as connected (no traces needed for fill net).

## KiCad Export / Import

### Export (`exporters/kicad_exporter.py`)

Generates `.kicad_pcb` files that open directly in KiCad 9. This enables manual routing of unrouted nets, DRC inspection, and Gerber export through KiCad's native tools.

**Exported elements:**
- Board outline → `Edge.Cuts` layer `gr_rect`
- Components → `footprint` blocks with inline pad definitions (no library dependency)
- Traces → `segment` elements with proper net assignment
- Vias → `via` elements (routing + stitching)
- Copper fills → `zone` definitions (board-outline zones on F.Cu and B.Cu; KiCad computes actual fill when user presses B)
- Silkscreen → `fp_text` (designators), `fp_circle` (pin 1 dots), `gr_text` (anode markers)
- Net declarations → `(net N "name")` for all nets in the design
- Fiducials → simplified 1mm circle pads

**Key conventions:**
- Pad offsets are unrotated (KiCad applies footprint rotation internally)
- Pad Y offsets are negated to match KiCad's Y-down coordinate system
- Pad offsets rounded to 4 decimal places to avoid floating-point noise that breaks trace-to-pad connectivity
- Through-hole pads declared on `*.Cu` (all layers); SMD pads on component layer only
- Zone clearance set to 0.5mm to prevent fill flooding between dense TH pin fields (e.g., DIP-28 at 2.54mm pitch)
- Stitching vias excluded from areas within via-exclusion radius of any component pad to prevent shorts

**KiCad CLI integration:**
- DRC: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc --format json`
- Render: `kicad-cli pcb render --side top` for automated board image generation
- Used for programmatic DRC validation during development

### Import (`exporters/kicad_importer.py`)

Parses `.kicad_pcb` files back into the pipeline's routed JSON format. Enables the workflow: export → manual fix in KiCad → re-import for Step 5 (DRC) and Step 6 (Gerber).

**Parsed elements:** Net declarations, footprint positions/pads, segments (traces), vias, zones. Components matched back to netlist by designator and pin net assignments.

### HTML Visualizer Export Button

The interactive HTML visualizer includes a "Download KiCad PCB" button that triggers client-side `.kicad_pcb` generation via JavaScript `Blob` + `saveAs`. Filename defaults to `{project_name}.kicad_pcb`.

## Through-Hole Pad Clearance

Through-hole pads span both copper layers (drill penetrates the full board). The routing grid marks TH pads with asymmetric clearance:

- **Component layer (top):** Full pad size + clearance as obstacle zone, full pad area marked with net ID
- **Opposite layer (bottom):** Full circular pad (max(w,h) diameter, matching KiCad export) + clearance as obstacle zone, marked with net ID

This prevents traces on the bottom layer from routing through TH pad copper — a common source of DRC shorts. The trade-off is reduced routability between dense TH pin fields (e.g., DIP-28), which accurately reflects physical manufacturing constraints.

**Detection:** `pad_geometry.py` sets `layer="all"` for known TH packages (DIP, PinHeader, PJ-002A, TO-220, HC49, 6mm_tactile). The footprint definition's `is_through_hole` attribute is checked first.

## Step 5: DRC (Design Rule Check)

Consolidates all validation into a structured report checked against the manufacturer's DFM profile. Runs automatically after routing, results displayed in the approval gate viewer.

**Check categories:**
- **Electrical** (from `validate_routing.py`): trace clearance, via clearance, connectivity (union-find), no shorts, pad clearance
- **DFM** (from `drc_checks_dfm.py`): trace width minimum, clearance minimum, via drill minimum, annular ring, silkscreen height/width — all checked against manufacturer profile from `engineering_constants.py`
- **Mechanical** (from `drc_checks_dfm.py`): hole-to-hole spacing, copper-to-board-edge distance
- **Current** (from `drc_checks_dfm.py`): IPC-2221 trace current capacity — verifies each trace width can carry its net's estimated current

**DFM profile resolution:** Loads from `requirements.manufacturing.manufacturer` (e.g., "jlcpcb_standard"), falls back to `generic`. Explicit values in `requirements.manufacturing` override profile defaults.

**Report format:** `{project}_drc_report.json` with per-check pass/fail, violation details (location, measured value, required value, net), and aggregate statistics.

**Output:** `validators/drc_report.py:run_drc()` returns the report dict. Runner prints summary, saves JSON, and passes to the approval gate viewer for display.

## Step 6: Output Generation

Produces manufacturer-ready files in `projects/{name}/output/`. All files are structured for direct upload to JLCPCB, PCBWay, or similar manufacturers.

**Output files:**
- **Gerber RS-274X** (`gerber_exporter.py`): F_Cu, B_Cu, F_SilkS, B_SilkS, F_Mask, B_Mask, F_Paste, Edge_Cuts — generated using the `gerber-writer` library (100% spec compliant). Board outline supports both rectangular and arbitrary polygon shapes (from DXF). Silkscreen uses a stroke vector font (`stroke_font.py`) for text rendering (A-Z, 0-9, symbols). Fiducials are 1mm copper dots with 3mm solder mask openings.
- **Excellon drill** (`gerber_exporter.py`): NC drill file with tool table, grouped by drill size. Sources: via holes + through-hole pad holes.
- **BOM CSV** (`bom_csv_exporter.py`): JLCPCB-compatible columns (Designator, Value, Package, Quantity, Description, Specs, Notes).
- **Pick-and-place CSV** (`bom_csv_exporter.py`): JLCPCB CPL format (Designator, Val, Package, Mid X, Mid Y, Rotation, Layer). Includes fiducials for machine vision alignment.
- **STEP AP214** (`step_exporter.py`): Populated PCB 3D model — board solid plus 3D component models at their placed positions with rotation. Component models sourced from LCSC/EasyEDA library via `easyeda2kicad` (cached in `~/.pcb-creator/3d-models/`, configurable via `PCB_3D_MODELS_DIR`). Parametric box fallbacks generated from `component_heights.py` package height database for components without library models. Generated directly in ISO 10303-21 text format (no CAD kernel dependency). Bare board export still available via `export_step()`.
- **Assembly drawing PDF** (`assembly_drawing.py`): Print-friendly assembly reference showing board outline, component courtyard rectangles with designator labels, pin 1 dots, polarity indicators, board dimensions with annotation arrows, title block (project name, date, revision), and BOM table. One page per populated side (top/bottom). Generated via SVG→PDF conversion using `cairosvg`. Supports polygon board outlines from DXF.
- **Gerber ZIP** (`gerber_exporter.py`): All Gerbers + drill packaged for manufacturer upload. BOM CSV, CPL, and assembly PDF are separate files (uploaded independently per manufacturer workflow).

**Dependencies:** `gerber-writer>=0.4` (Gerber generation). All other formats use Python stdlib.

### Completed Features

- **Gradio GUI** (`pcb-creator gui`): Web UI with chat-style circuit description input, drag-and-drop file upload, LLM-powered requirements translation with review/feedback loop, progressive board visualization (netlist → placement → routed → DRC), provider presets (OpenRouter/Local/Custom), Export KiCad and Import KiCad actions, step progress panel, and settings accordion. Viewer embedded via iframe with `sandbox="allow-scripts"` for full JavaScript interactivity (tooltips, pan/zoom).
- **Conversational requirements refinement**: After the LLM translates a circuit description, the GUI shows a rich markdown summary (with tables, calculations, wiring details) for user review. Users can send feedback/corrections that re-run the translation with context, or approve to start the pipeline. Input controls hide during pipeline execution to maximize progress panel visibility.
- **Agent-mode flag** (`--agent-mode`): Replaces the browser approval gate with vision-based autonomous review. Auto-approves on escalation. Used internally by the GUI.
- **Skip-approval flag** (`--skip-approval`): Bypasses all approval gates for batch/CI runs.
- **API-level retry**: LLM calls automatically retry up to 3 times with exponential backoff on transient errors (timeouts, rate limits, 5xx, connection errors).
- **Relay support**: Full pipeline support for relay components (`component_type: "relay"`, designator prefix `K`). Also supports variable resistor prefix `RV`.
- **DXF board outline** (`exporters/dxf_parser.py`): Parse DXF files containing board outlines (LWPOLYLINE, POLYLINE, or chained LINE entities). Extracted polygon vertices flow through placement, routing, and all exporters. Attach a DXF file as a `board_outline` attachment and set `outline_type: "dxf"` in requirements.
- **Assembly drawing PDF** (`exporters/assembly_drawing.py`): Generated in Step 6 alongside Gerbers. Shows component placement with designators, polarity marks, dimensions, and BOM table. Supports polygon outlines.
- **Netlist block diagram** (`visualizers/netlist_viewer.py`): Schematic-style visualization shown after Step 1 — components as colored boxes with pins, connected by bezier-curved nets that route around boxes.
- **LLM output robustness** (`gather/schema.py`): Automatic type coercion (string→number) and null stripping before JSON Schema validation. Prevents rework loops caused by LLMs outputting `"2"` instead of `2` or `null` for optional fields.
- **Gather validation & auto-fix** (`gather/schema.py`): Duplicate-pin validation catches pins assigned to multiple nets. Auto-fix fallback removes pins from power/ground nets when they conflict with signal nets. MCP server path has validation + 3-attempt rework loop matching the interactive CLI.
- **Functional IC pin names**: Gather prompt enforces functional names (`U1.GND`, `U1.PB3`) instead of physical pin numbers. Includes ICSP/SPI dual-function pin guidance for ATmega boards. IC pinout data auto-enriched via `translate()` for all callers.
- **Auto-merge shared nets** (`step_1_schematic.py`): When the model creates separate nets for the same physical pin (common with dual-function MCU pins like MOSI/D11), a union-find merge step automatically combines them before counting as a rework attempt.
- **Netlist structure normalization** (`step_1_schematic.py`): Small models (<20B) often generate netlist JSON with separate `ports[]`, `nets[]`, `components[]` top-level arrays instead of the expected flat `elements[]` array. Auto-normalizes both variants (partial split with `elements` containing only components, and full split with no `elements` key) before validation. Tested with Qwen 9B — raises pass rate from 22% to parity with 27B on simple boards.

### Future Enhancements

- **Manufacturer quoting (Step 7)**: Auto-submit BOM, Gerbers, assembly files to manufacturer APIs (JLCPCB, PCBWay, OSH Park) for fabrication + assembly quotes. Present comparison to user. Steps 5-6 produce submission-ready files in manufacturer-expected formats. Blocked on: vendor API availability or TOS review for agent accounts + Playwright.
- **build123d for richer 3D models**: Replace hand-written STEP BREP boxes with build123d parametric shapes (rounded IC bodies, pin legs, LED domes, etc.). Blocked on: build123d/cadquery adding Python 3.14 support (requires OpenCascade `cadquery-ocp` wheels).

## User-Defined Component Positions

Users can specify exact component positions via `placement_hints` in requirements:
```json
{ "ref": "J1", "x_mm": 2.8, "y_mm": 6.0, "edge": "left" }
```

These are respected throughout the pipeline:
- Step 3 (Layout): LLM prompt includes hints; components get `placement_source: "user"`
- SA Optimizer: Components with `placement_source: "user"` are **pinned** (never moved)
- SA Repair: Pinned components stay fixed; other components move around them
- Connectors and fiducials are also pinned by component type

## Visualization

### Board Viewer (`visualizers/placement_viewer.py`)

Interactive HTML/SVG board views with:
- Color-coded components by type with pad shapes (rectangles for each pin, derived from pad_geometry)
- Ratsnest lines (MST per net, color-coded by net class)
- Routed traces (width-proportional, colored by net, layer-aware opacity, supports diagonal segments)
- Vias (concentric circles: annular ring + drill hole)
- Silkscreen elements (designator labels, pin 1 dots, anode "A" markers)
- Routing progress bar in header (green/yellow/red by completion %)
- Hover tooltips with value, specs, and description (from BOM)
- Pan/zoom with auto-fit-to-viewport, side panel with routing statistics and component table
- Fiducial markers
- `embed_mode` flag suppresses action buttons when embedded in the Gradio GUI
- `fitToView()` with `requestAnimationFrame` retry for correct sizing inside iframes

### Netlist Viewer (`visualizers/netlist_viewer.py`)

Schematic-style block diagram showing circuit connectivity after Step 1:
- Components as labeled boxes colored by type (blue=resistor, red=LED, green=IC, purple=connector)
- Pins on box edges (left/right split) with pin name labels
- Bezier-curved net connections that exit away from box edges (no through-box routing)
- Color-coded by net class (red=power, blue=ground, gray=signal)
- Multi-pin nets converge at junction dots
- Hover tooltips with component details, specs, and descriptions
- Auto-scaling SVG (`preserveAspectRatio="xMidYMid meet"`) fills available viewport
- Three-column layout: connectors → ICs → passives

### Progressive Visualization

In the Gradio GUI, the board viewer updates progressively through the pipeline:
1. **After Step 1** → Netlist block diagram (verify connectivity)
2. **After Step 3** → Placement view with ratsnest (component positions)
3. **After Step 4** → Routed board with traces, vias, and copper fills
4. **After Step 5** → Routed board + DRC report panel

See `VISUALIZATION.md` for the data format reference (useful for building custom visualizations).

## Gradio GUI (`pcb-creator gui`)

### Architecture

The GUI is a single Gradio Blocks app (`orchestrator/gradio_app.py`) with two columns:
- **Left (30%)**: Chat display (rich markdown), circuit description input, file upload, action buttons, step progress panel
- **Right (70%)**: Board viewer (iframe-embedded HTML with full JavaScript interactivity)

### Workflow Phases

1. **Input phase**: User types circuit description, optionally attaches files (DXF outlines, sketches, photos). Settings accordion allows choosing provider (OpenRouter/Local/Custom), API key, model, max tokens.
2. **Translation phase**: LLM translates natural language → structured requirements JSON. The `RequirementsGatherer` handles datasheet enrichment, footprint lookup, and engineering calculations automatically.
3. **Review phase**: Rich markdown summary displayed (tables, wiring, calculations). User can send feedback (re-runs translation with context) or approve.
4. **Pipeline phase**: Input controls hide. Step progress panel shows Steps 0-6 with status indicators. Board viewer updates progressively (netlist → placement → routed → DRC).
5. **Complete phase**: Export KiCad / Import KiCad buttons appear. Input controls reappear for next design.

### Technical Details

- **Iframe embedding**: Viewer HTML is wrapped in `<iframe srcdoc="...">` with `sandbox="allow-scripts allow-same-origin"` because Gradio 6 sanitizes inline `<script>` tags. The iframe preserves full tooltip, pan/zoom, and trace hover interactivity.
- **Generator pattern**: `run_workflow_streaming()` yields event dicts at step boundaries. The Gradio handler consumes these to update UI components progressively without blocking.
- **LLM type coercion**: `coerce_requirements_types()` in `gather/schema.py` fixes common LLM output issues (string "2" → int 2, null → key deletion) before JSON Schema validation, reducing failed rework loops.
- **Provider presets**: Dropdown auto-fills api_base + model name. OpenRouter uses litellm's `openrouter/` prefix (no explicit api_base needed). Local uses `openai/` prefix with `http://localhost:8000/v1`.
- **Project slugs**: `_slugify()` strips filler words and caps at 24 chars with whole-word boundary. "A blink circuit with an LED and an ATTiny85" → `blink_led_attiny85`.

### CLI Commands

| Command | Description |
|---------|-------------|
| `pcb-creator gui` | Launch Gradio web GUI (port 7860) |
| `pcb-creator gui --port 8080 --share` | Custom port + public Gradio URL |
| `pcb-creator run --requirements req.json --project name` | Headless pipeline with browser approval |
| `pcb-creator run --requirements req.json --agent-mode --skip-qa --json-output` | Agent-optimized: auto project name, structured JSON result to stdout |
| `pcb-creator run ... --agent-mode` | Headless pipeline, vision-based approval (auto-approves on escalation) |
| `pcb-creator run ... --skip-approval` | Headless pipeline, skip all approval gates (batch/CI) |
| `pcb-creator run ... --skip-qa` | Skip per-step LLM QA reviews and vision review (validators still run) |
| `pcb-creator schema` | Print requirements JSON schema to stdout (for agents to learn the format) |
| `pcb-creator design --project name` | Interactive CLI requirements gathering |
| `pcb-creator import-kicad --project name --kicad-file board.kicad_pcb` | Re-import edited KiCad file |
| `pcb-creator validate netlist.json` | Validate an existing netlist |
| `pcb-creator mcp` | Launch MCP server (stdio transport) for AI agent integration |

### Agent Integration

**CLI (recommended for shell-based agents like Agent Zero):** Agents write a requirements JSON file to disk (avoiding MCP transport encoding issues with large payloads), then run `pcb-creator run --requirements file.json --agent-mode --skip-qa --json-output`. Use `pcb-creator schema` to get the expected format. The `--project` flag is optional — auto-generated from the requirements JSON. The `--json-output` flag prints structured results (success, routing stats, DRC, output files) to stdout.

### MCP Server

The MCP server (`mcp_server.py`) exposes the pipeline as tools for any MCP-compatible AI agent (Claude Code, Agent SDK, OpenClaw, Agent Zero). Uses FastMCP with stdio transport, runs headless with vision-based approval.

**Projects directory:** `~/.pcb-creator/projects/` (configurable via `PCB_PROJECTS_DIR` env var). Persists across sessions — any agent can resume or inspect previous designs.

**Two input modes for `design_pcb`:**
- **Structured (preferred for agents):** Pass `requirements_json` dict directly — skips LLM translation entirely. Call `get_requirements_schema()` first to get the expected JSON Schema.
- **Natural language:** Pass a plain-text `description` — translated to structured requirements via LLM automatically. Useful for simpler circuits or human-driven workflows.

**Tools:**

| Tool | Description |
|------|-------------|
| `design_pcb(description, project_name?, requirements_json?, settings?)` | Run the full pipeline. Pass `requirements_json` to skip LLM translation (preferred for agents), or `description` for NL input. Returns routing stats, DRC summary, output file paths. |
| `get_requirements_schema()` | Returns the JSON Schema for structured requirements. Call once, then construct `requirements_json` dicts for `design_pcb`. |
| `list_projects()` | List all projects with status and output availability. |
| `get_project_status(project_name)` | Detailed status: step progress, routing stats, DRC pass/fail. |
| `get_drc_report(project_name)` | Full DRC report with per-check violations. |
| `export_kicad(project_name)` | Export completed design to KiCad `.kicad_pcb` format. |
| `get_board_image(project_name, width?)` | Render routed board as base64 PNG image. |

**Configuration:** Same `PCB_*` environment variables as the CLI. The server forces `agent_mode=True`, `skip_qa=True` (calling agent reviews results itself), `max_rework_attempts=3`, and `llm_timeout=300` (5 min per LLM call, fail fast). These can be overridden per-call via the `settings` parameter.

## LLM Provider Support

### Cloud (OpenRouter)
Default: `openrouter/x-ai/grok-4.1-fast` — fast, reliable structured output for netlist/BOM/placement generation. Also tested with `openrouter/qwen/qwen3.5-27b` (27B dense model, good for local or cost-sensitive use).

### Local (oMLX / Ollama)
Use `--model openai/<model-name> --api-base http://localhost:8000/v1` for local models.
Tested with `Qwen3.5-27B-MLX-7bit` via oMLX. Local models need longer timeouts (30min default) for complex boards (21+ components).

### Configuration

API key loaded from `.env` file (`PCB_LLM_API_KEY`). In the GUI, keys can be entered in the Settings accordion (overrides env var for that session).

Environment variables: `PCB_LLM_API_KEY`, `PCB_LLM_API_BASE`, `PCB_GENERATE_MODEL`, `PCB_REVIEW_MODEL`, `PCB_GATHER_MODEL`, `PCB_LLM_MAX_TOKENS`, `PCB_LLM_TIMEOUT`, `PCB_VISION_MODEL`, `PCB_KICAD_LIBRARY_PATH` (root of KiCad footprint library for tiered lookup), `PCB_COMPONENT_CACHE_PATH` (default `~/.pcb-creator/component_cache.json`), `PCB_LLM_ENRICHMENT_WORKERS` (parallel LLM calls for enrichment, default 4).

## Key Learnings

1. **IC pinouts must be in requirements.** LLMs don't reliably know pin mappings for specific IC packages. The datasheet lookup step resolves these during gather, before the user approves requirements. The `validators/pinout.py` module then enforces them deterministically — auto-correcting wrong names/types and flagging out-of-range pins via DRC.

2. **Output token limits matter.** A 21-component Arduino Uno netlist is ~30KB / ~8K tokens. Free-tier models with 8K output limits truncate. The continuation mechanism handles this, but choosing a model with adequate output capacity (Qwen3.5-27B at 32K+) avoids the issue entirely.

3. **Deterministic validation catches most errors.** The 9 DRC checks catch the same classes of errors that ECAD tools flag (KiCad DRC, Altium). The LLM QA adds value for requirements compliance but is not the primary safety net.

4. **Resistor power needs circuit-aware calculation.** Using a LED's max rated current (20mA) instead of the actual operating current (V_supply - Vf) / R produces false positives. The DRC traces the circuit topology to compute real power dissipation.

5. **Shared constants prevent drift.** Having `engineering_constants.py` as the single source of truth for the Python side, while `engineering_rules.md` serves as prose guidance for the LLM, means the Python always enforces the correct values regardless of what the LLM remembers.

6. **Small models work.** The entire pipeline — gather, generate, validate, QA — runs on Qwen3.5-27B (27B parameters). Total context per call is under 15K tokens. This is viable for local inference on a Mac Studio.

7. **LLMs can't do spatial math.** For complex boards (21+ components), LLMs consistently generate overlapping placements. The SA repair algorithm resolves these in seconds — this hybrid approach (LLM for semantics, algorithm for geometry) is far more robust than asking the LLM to retry.

8. **Repair mode enables weaker models.** With repair, even a local 27B model can handle the Arduino Uno (21 components). The LLM gets approximate positions right, and the algorithm fixes the geometry. Without repair, the same model fails all rework attempts.

9. **Thinking models waste tokens on structured output.** Qwen3.5's thinking mode produces reasoning preamble before JSON, consuming tokens and sometimes breaking JSON extraction. Use `--no-thinking` to disable this via `extra_body`. With thinking disabled, the full pipeline (Steps 0-4) runs in ~4 minutes on local Qwen3.5-27B vs 2+ hours with thinking enabled.

10. **QA LLMs hallucinate failures on valid output.** Small models frequently reject valid outputs — e.g., claiming placement JSON "lacks netlist data" or inventing calculation errors. The fix: when the Python validator passes with 0 errors, QA failures are overridden to warnings. The deterministic validator is the authoritative gate, not the LLM QA. This prevents rework loops on phantom issues.

11. **Step-specific QA prompts matter.** The QA template must tell the LLM what to check per step. Step 3 (placement) should only check spatial properties, not electrical connectivity — that was verified in Steps 1-2. Without step-specific guidance, small models apply all checks indiscriminately and fail on inapplicable criteria.

12. **Through-hole pads must be marked on both layers.** The initial implementation only blocked TH pads on the component layer, allowing bottom-layer traces to route through TH pad copper — producing hundreds of DRC shorts in KiCad. Marking TH pads on both layers with asymmetric clearance (full pad on component layer, drill-sized on opposite layer) eliminated most shorts but significantly reduced routability between dense TH pin fields. This is physically correct — routing between DIP-28 pins at 2.54mm pitch with 1.6mm pads leaves <1mm clearance.

13. **KiCad coordinate conventions differ subtly.** KiCad uses Y-down coordinates and applies footprint rotation to pad offsets internally. Our pipeline uses Y-up (standard math CCW). The pad Y-offset must be negated in the KiCad export to produce matching absolute pad positions. Floating-point noise in pad offsets (e.g., `3.8099999999999987` instead of `3.81`) causes KiCad to compute pad centers that don't match trace endpoints — rounding to 4 decimal places fixes this.

14. **KiCad CLI enables programmatic DRC.** `kicad-cli pcb drc --format json` runs full DRC without opening the GUI, returning machine-readable violation reports. This is invaluable for iterating on export fixes — each change can be verified in seconds instead of manually inspecting in the KiCad editor.
