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
│  Freerouting (required for 4L) │
│  or built-in A* (2-layer only) │
│  → IPC-2221 trace widths       │
│  → Copper fill + stitching     │
│  → Inner planes (4-layer)      │
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
│   ├── escape_router.py        # Fine-pitch dog-bone escape breakout (protected wiring)
│   ├── route_cleanup.py        # Post-route rip+reroute of kicad-cli-shorting/incomplete nets
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
│   ├── kicad_drc.py            # Authoritative DRC via kicad-cli, shaped into the report dict
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
│           ├── {name}-In1_Cu.gbr      #   inner GND plane (4-layer only)
│           ├── {name}-In2_Cu.gbr      #   inner power plane (4-layer only)
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
  "board": { "layers": 2 },
  "components": [
    { "ref": "R1", "type": "resistor", "value": "220ohm", "package": "0805" }
  ],
  "connections": [
    { "net_name": "VCC", "net_class": "power", "pins": ["J1.1", "R1.1"] }
  ]
}
```

Set `"board": { "layers": 4 }` to request a 4-layer board (F.Cu signal → In1.Cu GND plane → In2.Cu power plane → B.Cu signal). **4-layer boards require Freerouting** (`PCB_ROUTER_ENGINE=freerouting`, the default). The most-connected non-GND power net is automatically chosen as the inner2 plane (e.g. VCC3V3 over VCC5 if VCC3V3 connects more components).

```json
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
0. **Custom footprints (tier 0, searched first)** — project-local `.kicad_mod` files in `<project>/custom-footprints.pretty/` (registered via the `register_custom_footprint` MCP tool) and an optional global dir (`PCB_CUSTOM_FOOTPRINT_DIR` / `config.custom_footprint_dir`). Lets an agent supply an exact datasheet footprint for a part no library covers, taking precedence over every tier below. `check_footprint_coverage` / `check_footprint_tier` report which tier (including `custom`) resolves each BOM line, so the agent can register footprints *before* placement instead of discovering gaps mid-flow.
1. **KiCad library** — parses `.kicad_mod` files from the official KiCad footprint library (~50K packages). Community-maintained, datasheet-verified. Requires `PCB_KICAD_LIBRARY_PATH` to point to the library root. Lazy index built on first lookup with short alias generation (`SOIC-8_3.9x4.9mm_P1.27mm` → also matches `SOIC-8`).
2. **IPC-7351B parametric** (`ipc7351.py`) — algorithmic footprint generation per the IPC land pattern standard. Covers QFN, DFN, SOP/SSOP/TSSOP/MSOP, SOT-223, SOT-89, and BGA families. Zero I/O cost.
3. **Local cache** — cached footprints from prior EasyEDA or LLM lookups.
4. **Built-in approximations** — hardcoded definitions (0402-1210, SOT-23, SOIC-8, TO-220, HC49, PJ-002A, 6mm tactile) and parametric generators (DIP-N, PinHeader 1xN/2xN, TQFP-N). Custom-built for this project; used as last local resort when no authoritative library is available.
5. **Normalized-name retry** — if tiers 1-4 all miss on a verbose KiCad name, `_normalize_package()` reduces it to a recognized code and retries once: `R_0805_2012Metric` → `0805`, `C_0603_1608Metric_HandSolder` → `0603`, `SOIC-8_3.9x4.9mm_P1.27mm` → `SOIC-8`, `Crystal_HC49-4H_Vertical` → `HC49`. This is what lets an imported KiCad netlist resolve common passives even without `PCB_KICAD_LIBRARY_PATH` set.
6. *(caller-managed)* **EasyEDA API** → **LLM fallback** → **perimeter distribution** (absolute last resort)

**Footprint verification gate (agent-driven flow).** In the LLM-driven pipeline, the gather step verifies and enriches every footprint before the user approves. The agent-driven flow (KiCad netlist import → granular MCP tools) has no gather step, so an unresolved package would silently become a 3 mm placeholder. `validators/verify_footprints.py` closes this gap: it resolves every component through the tiers above and returns the ones that miss. `stages.run_placement` runs this gate first and **refuses to place** if anything is unresolved, returning a structured `unresolved_footprints` list (designator, package, pin count, reason). The agent remediates by correcting the package name, setting `PCB_KICAD_LIBRARY_PATH`, or calling the `provide_footprint` MCP tool (alias to a known package, or supply explicit pin offsets + pad size — cached with `source: "agent"`, `needs_review: true`). The `import_kicad_netlist` and `verify_footprints` MCP tools surface the same list so the agent can fix footprints immediately after import. Fiducials are exempt (they carry their own geometry). **Note:** the MCP server bootstraps `configure_lookup()` (via `_ensure_lookup_configured()`) so the KiCad-library and cache tiers are active in the agent process — the CLI and GUI do this at startup.

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
- **Optimize mode**: Improves valid placements. Minimizes wire length (MST-based ratsnest) and crossing count via simulated annealing. Iteration count auto-scales from component count (200 × movable components, bounded [2000, **8000**] — the upper cap keeps runtime under ~30s on a Raspberry Pi 5; override with `PCB_OPTIMIZER_ITERATIONS` or `SAConfig(max_iterations=N)`). Early termination on stagnation (default 500 iterations without improvement).

Move types: translate (70%), swap same-package (15%), rotate (15%). Pinned components (user-placed via `place_component`, connectors, fiducials, mounting-hole/keepout packages) are excluded from moves. After optimize, `find_placement_violations` re-checks (pad-extent aware) and `run_placement` **fails with a structured `violations` report** if anything still overlaps or overhangs the edge — so a pinned part that conflicts is surfaced, not silently shipped.

**Two-sided placement (`SAConfig.two_sided`).** A layer-flip move (≈15% in optimize, 20% in repair) moves small SMD passives (resistor/capacitor/diode only — never connectors, ICs, LEDs, through-hole, pinned, or keepout parts) to the bottom side. A `bottom_penalty` term keeps flips reluctant on 2-layer boards (the bottom is the router's only escape layer there) but cheap on 4-layer with `plane_layers=1` (inner signal layer frees capacity). Bottom pads are X-mirrored (`dx → -dx`) consistently across `build_pad_map`, the KiCad export, and the Gerber/CPL side fields; layer-conflict checks treat through-hole parts as blocking *both* sides (`_layers_conflict`, `_effective_layer`). Use it to make a dense board *fit*, not to improve routability. **Flip guard:** beyond the resistor/capacitor/diode type gate, a flip candidate must have ≤3 pins and a coarse (≥0.8mm) pad pitch — so a high-pin or fine-pitch part *mis-typed* as a passive (e.g. a 30-pin 0.5mm FPC inferred as `capacitor`) can never be sent to the bottom.

**Escape-halo / fanout reservation (`SAConfig.escape_weight`, enhancement A).** A dense or fine-pitch part needs a clear channel on its escape edges to fan its pins out — roughly `escapes × (trace + clearance)` of perimeter. Nothing reserved it before, so neighbours crowded the pin rows and the autorouter had nowhere to take the escapes (a ~77% routing plateau on a dense fine-pitch board). `_build_escape_halos` computes a per-part fanout demand (pin count vs distinct leaving-net count) and a halo radius from the classic fanout-annulus bound — to lay `N` tracks at pitch `p` around a part you need a clear ring of circumference `N·p`, i.e. radius ≥ `N·p / 2π`, measured beyond the body. `_escape_halo_cost` then penalizes foreign pads that intrude into that halo, scaled by intrusion depth and the foreign part's pin count, and **layer-aware** (an opposite-side SMD does not contend; a through-hole part blocks both sides). It **self-gates**: only parts that exceed the pin/pitch threshold (`ESCAPE_PIN_THRESHOLD=8`, `ESCAPE_PITCH_THRESHOLD_MM=0.8`) get a halo, so ordinary boards produce `{}` and the term is a pure no-op. The annulus pitch is sized from the board's actual routing rules (`escape_track_pitch_mm = trace + clearance`). On by default at a modest weight.

**Localized routing-feedback re-place (`SAConfig.focus_components`, enhancement C).** When the first route comes back incomplete, `run_route_with_retry` maps the `unrouted_nets` back to the components that carry them (`_components_for_unrouted`) and re-places with those parts named in `focus_components` — which forces an *enlarged* escape halo around exactly the region that failed (a local spacing increase, plus the congestion term, on a fresh seed). This replaces the old blunt global `+0.5mm` clearance bump, which disturbed parts that had routed fine; the global bump remains only as a fallback when no unrouted region can be identified. Keeps whichever attempt routed better.

**Near-complete → incremental finish (not re-place).** Re-placing only helps when the board needs a *different* placement. A route that already reached `INCREMENTAL_FINISH_PCT` (≥85%) is essentially placed right with a few stragglers, so `run_route_with_retry` skips the re-place and instead does an **incremental** pass — `build_incremental_fixed_routing` protects every fully-connected net's wiring and Freerouting re-routes only the residual nets. This is the key fix for the auto-retry blowing up wall-clock: the old path re-placed and ran a *whole second route* that, on a board Freerouting can't fully close, oscillates its unrouted count for the full pass budget (stacked on the first route + short-cleanup = 10+ min that looks hung). The incremental pass touches a handful of nets and `_route_score` keeps the better of the two, so it never regresses. `build_incremental_fixed_routing` is shared with `route_board(keep_existing=True)`. (Note: bounding the *first* route's own oscillation needs a smaller `-mp`, not a mid-pass SIGTERM — FR 2.1.0 only writes the SES at end-of-passes, so killing it mid-run yields no partial.)

**Connector fanout orientation (enhancement D, in `optimizers/initial_placement.py`).** The grid placer stacks connectors on the left (vertical) edge. A **wide, high-pin** connector (long pad-span axis = x, `≥ ORIENT_MIN_PINS` pins) is rotated 90° (`_connector_rotation`) so its pad row runs **vertically along the edge** — every pin escapes a short hop into the interior, instead of the row poking horizontally into the board with deep, hard-to-escape inner pins. Connectors are pinned through the SA optimize pass, so the orientation sticks. **Gated on pin count after a measured regression:** reorienting *all* connectors deterministically *worsened* routing on boards with small terminal blocks (rs485 0→3 DRC, 4ch 11→26 DRC) — few-pin headers/terminal blocks gain nothing from reorientation and the rotation just disrupts their escapes. So only connectors wide enough for the deep-inner-pin problem to exist (≥10 pins) are reoriented; that makes D a no-op on the current suite (its connectors are ≤6 pins) and on a fine-pitch FFC connector that is mis-typed `capacitor` (inferred from its footprint name) so isn't a "connector" at all. A's escape halo still covers such a part (fine-pitch/pin-count trigger) and the flip guard protects it. The `ORIENT_CONNECTORS` kill-switch and `ORIENT_MIN_PINS` threshold make D A/B-testable. *Remaining D work:* seed a connector's fed components in pin order (SA's wirelength term already recovers much of this for movable neighbours), and broaden the trigger to high-pin/fine-pitch parts mis-typed as passives.

**Mounting holes seeded at corners (`optimizers/initial_placement.py`).** Mounting-hole packages are mechanical keepouts that the SA optimizer pins by package (`_is_keepout_package`), so wherever the grid *seeds* them is where they stay. The grid placer therefore seeds the first four at the board corners (pads inset by the margin), matching how enclosures place standoffs — rather than letting them fall into the size-sorted interior grid, which stacked them in a row near one edge. The corner regions are reserved (each edge's connector cursor starts past `corner_reserve` and stops short of the far corner) so a corner hole and an edge connector — both pinned — never produce an unfixable overlap. Boards with no mounting holes are unaffected (`corner_reserve = 0`).

**Routing-demand congestion / RUDY (`SAConfig.demand_weight`, enhancement B) — implemented, off by default.** Where pad-density congestion measures where *pads* sit, B models routing *demand*: `_routing_demand_cost` spreads each signal net's estimated wire (half-perimeter of its pad bounding box) uniformly over that box (the classic RUDY estimator), sums the per-cell contributions into a demand heatmap (2.5mm cells), and penalizes cells whose demand exceeds ~75% of the track capacity they could physically carry (`DEMAND_UTILIZATION_LIMIT`). Plane nets are excluded. It is correct, self-gating, and costs ~0 runtime. **It is OFF by default (`demand_weight=0.0`)** because the empirical finding is that *no board in the current suite is channel-congestion-limited*: the densest test board (73 parts) peaks at only ~7% over capacity in a few cells (byte-identical placement at weight 0 vs 40), and the others are likewise inert. That board's wall is *local fanout at a pinned fine-pitch connector* (A's and D's domain), not broad channel congestion. So the weight that would make B bite is unvalidated — enabling it would reshape a genuinely-congested board in an unverified way. The term stays implemented and unit-tested (`tests/test_routing_demand.py`), ready to enable and tune the day a congestion-limited board exists to validate against.

**Incremental cost evaluation (`ratsnest.IncrementalCost`):** An SA move perturbs only a few components, so the cost is evaluated *incrementally* rather than recomputed globally each iteration. The evaluator caches per-net MST geometry and a flat segment list; a move recomputes only the nets touching the moved component(s) and re-tests crossings via a spatial grid (`evaluate`/`commit`/`revert`). Power/ground nets are treated as **plane nets** (`_PLANE_NET_CLASSES`) — delivered by copper pours, not point-to-point traces — so they keep a cheap wirelength term but are excluded from the crossing metric and the spatial grid. This removes the board-spanning hub segments that previously dominated cost. Net effect: roughly O(n × E²) per full run → near-linear, ~5–19× faster on 40–150-component boards, which is what keeps large boards from timing out.

**Board auto-sizing:** Before the first LLM attempt, board dimensions are checked against total component footprint area × 2.5. Default board sizes are auto-expanded if too small; user-specified sizes are warned but respected.

**Fallback chain** (after all LLM rework attempts exhausted):
1. Grow board 20%, re-run SA repair on last LLM placement
2. Generate deterministic grid-based placement on 30%-larger board (connectors on edges, largest components first), then SA repair. The grid placer lives in `optimizers/initial_placement.py` (`generate_grid_placement`) and is shared with the granular placement stage (see MCP Server).

### Fiducials

2 fiducial markers per populated board side (top and/or bottom). Placed in diagonally opposite corners. 1mm copper dot with 2mm clearance (3mm total footprint). Exempt from netlist cross-reference validation.

### Attachments

Requirements can reference DXF board outlines, sketches, and photos via the `attachments` array. Files are copied to the project directory at Step 0. Later steps discover them via `used_by_steps`.

## Step 4: Routing

Purely algorithmic (no LLM). Takes placement.json + netlist.json, outputs routed.json.

**Routing engines:** Two engines available, configured via `PCB_ROUTER_ENGINE` env var or `config.router_engine`:

### Freerouting (default)

External autorouter via Specctra DSN/SES format exchange. Uses the Freerouting Java application (v2.1.0+, requires Java 17+).

**Workflow:** `placement.json + netlist.json` → [fine-pitch escape breakout](#fine-pitch-escape--fanout-pre-routing-optimizersescape_routerpy) (auto, protected wiring) → DSN export → Freerouting JAR (headless) → SES import → copper fills → [short-cleanup pass](#fine-pitch-escape--fanout-pre-routing-optimizersescape_routerpy) (kicad-cli-driven rip+reroute of shorting/incomplete nets) → silkscreen → `routed.json`

- **DSN export** (`exporters/dsn_exporter.py`): Converts placement + netlist to Specctra DSN format. Board outline, component footprints with physical pad definitions (from `pad_geometry.py` tiered lookup), net connectivity, and design rules. Plane nets (GND, and the chosen power net when In2 is a plane) are excluded from the DSN so Freerouting never routes them — they're delivered by copper pours. Only inner layers that are *planes* are omitted from the `structure` routing-layer list (`plane_layers`, see [4-Layer Inner Planes](#4-layer-inner-planes)); via padstacks and **through-hole pad padstacks both carry a shape on every routing layer** so Freerouting treats a TH pad as the obstacle it physically is on the inner signal layer too (a shape only on F.Cu/B.Cu let it route an inner trace straight through a through-hole pad → a real short). The `structure` section also emits Specctra `(keepout)` circles for plane-net (GND) escape stubs/vias, which are absent from the netlist and otherwise invisible to the router (see [Fine-Pitch Escape](#fine-pitch-escape--fanout-pre-routing-optimizersescape_routerpy)). The `(wiring)` section is empty for a fresh route, or carries existing traces/vias as `(type protect)` wires for [incremental routing](#mcp-server) and the escape breakout.
- **SES import** (`exporters/ses_importer.py`): Parses Freerouting's session output. Extracts wire paths (traces) and vias, maps net names back to internal net_ids. Reuses S-expression parser from `kicad_importer.py`.
- **Orchestration** (`optimizers/freerouter.py`): Auto-downloads JAR to `~/.cache/pcb-creator/` on first use. `-mt 1` (single-threaded, avoids clearance bugs); `-mp` (max optimization passes) and the timeout come from the `effort` level (fast = 5 passes/120s, normal = 20/300s, best = 40/900s + one retry on timeout). Runs via `Popen` with a stdout reader thread that parses each `Auto-router pass #N … (M unrouted)` line into live progress (plus a ~10s heartbeat). **Graceful timeout:** on hitting the wall clock it SIGTERMs Freerouting and imports a partial SES if one was written, else raises an actionable message — note Freerouting v2.1 only writes the SES at end-of-passes, so a killed mid-pass run usually yields no partial. **JVM heap cap:** the JVM is launched with `-Xmx` (`_default_heap_mb`: `PCB_FREEROUTING_HEAP_MB` if set, else ~55% of RAM clamped to [1024, 6144] MB) — Freerouting 2.x grows memory aggressively (>25 GB observed on a 73-part 4-layer board), so without a cap the OS OOM-kills it into an opaque crash; the cap turns that into a catchable `OutOfMemoryError`. OOM is detected from the JVM output or exit code (−9/137) and reported as a **host memory limit** with the *correct* recovery: raise `PCB_FREEROUTING_HEAP_MB` / use a bigger-RAM host, or reduce what the router must hold (fewer parts, smaller board, power+ground on planes). It explicitly does **not** advise "add a routing layer" — more signal layers make Freerouting use *more* memory, so that worsens OOM (the old message had this backwards). `ensure_java()` also probes common Java paths (`/usr/bin/java`, …) before `PATH`, since the MCP server may run with a restricted environment. **Fallback:** if Freerouting raises (timeout-with-no-partial, OOM, crash), `run_routing` logs the failure and falls back to the built-in A* router **on 2-layer boards only**. On a 4-layer board it returns an error — but one framed as a failure of *this run*, not a layer-count limit (it explicitly tells the agent N-layer routing is supported and to retry / `keep_existing`, **not** to reduce layers or enlarge the board). The old "the built-in router cannot route >2 layers" wording made agents abandon perfectly routable 4-layer boards. (A 4-layer board with `router_engine != "freerouting"` is likewise rejected up front.)
- **Copper fills** (`router.py:apply_copper_fills()`): Standalone function that rebuilds a routing grid from the Freerouting output, marks existing traces/vias, then runs the standard fill algorithm. Removes plane nets (GND on 2-layer; GND + power net on 4-layer) from the unrouted list and updates completion stats to 100%.

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

**Segment-aware connectivity (`_check_connectivity`):** the per-net union-find connects pads, vias, and trace endpoints to any point lying *along* a same-net, same-layer trace **segment**, not just at coincident endpoints. This matters because Freerouting freely produces T-junctions (a branch trace teeing into a trunk's interior), mid-trace via drops, and pads sitting under a trace — all real electrical connections that endpoint-only matching missed, splitting a genuinely-routed multi-pad net into false "disconnected groups" (observed on a dense board: Freerouting reported ~98% routed while the validator flagged several power nets as disconnected). Layer matching is preserved (a top pad still needs a via to reach a bottom trace), so the change only removes *false* disconnects — a genuinely unreached pad is still flagged. Regression tests in `tests/test_connectivity_junctions.py`.

**Via layer spanning (`_via_spanned_layers`):** a via connects to same-net traces on **every copper layer it spans** (`_copper_stack` order top→inner1→…→bottom), not just its `from_layer`/`to_layer`. A through-via (top↔bottom) physically passes through inner1/inner2, so an inner-layer trace landing on it is connected. Without this, nets routed on inner *signal* layers (e.g. `plane_layers=0`, where both inner layers carry signal) showed false disconnects even when fully routed — this was the second half of a "97.9% routed but N disconnected" discrepancy on a dense 4-layer board. A genuinely via-less inner trace is still flagged (regression-tested), so no real break is masked.

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
4. Add **stitching vias** on a ~5mm grid wherever fill exists on both layers — these connect the top and bottom ground planes into a unified reference. The fill grid only models the outer layers, so inner-layer signal traces (In2 when `plane_layers<2`) are marked as **via-exclusion zones** first — a *through* stitching via dropped on an inner trace would pierce and short it. Stitching and rescue vias both honour those zones (`grid.can_place_via`).
5. Add **rescue vias** — find top-layer fill islands disconnected from GND, drop a via at the island centroid where bottom fill exists, connecting the island through the bottom plane. Only rescues islands ≥4 cells to avoid saving tiny slivers.
6. **Cross-layer island removal** via BFS: seeds from GND pads on either layer, traverses stitching vias and rescue vias to reach bottom fill. Islands unreachable from any GND pad through same-layer adjacency or cross-layer vias are removed
7. Convert bitmap to merged rectangles (run-length encoding + vertical merge) for output

**Why stitching vias matter:** The bottom layer typically has no GND pads (SMD components are on top). Without stitching vias, the entire bottom fill would be removed as an island. Stitching vias provide cross-layer connectivity so the bottom plane stays connected to the top GND features.

**Output:** `copper_fills` array in routed.json with layer, net_id, net_name, and polygon vertex arrays. Stitching vias appear in the `vias` array alongside routing vias.

**Validator:** Connectivity check recognizes fill-connected pads as connected (no traces needed for fill net).

### 4-Layer Inner Planes

On 4-layer boards, two additional copper regions are generated after the outer fills:

**Stackup options (`board.plane_layers`, default 2).** `inner_plane_count(board)` decides how many inner layers are solid planes: 2 = In1 GND + In2 power (route on F.Cu/B.Cu — best power integrity); 1 = In1 GND plane only, **In2.Cu becomes a third signal routing layer** (power routed as traces, not a plane) — ~50%% more signal capacity for dense boards such as a fine-pitch connector with many GPIO; 0 = all inner layers signal. The DSN structure lists only non-plane copper layers as routable, `run_routing` excludes a net from routing only when it is delivered by a plane (GND always; the power net only when In2 is a plane), and `apply_copper_fills` generates a plane per plane-layer.

**Inner1 (GND plane):** Board-outline polygon with circular cutouts (antipads) around every through-hole pad and via whose net is not GND. Cutout radius = pad radius + 0.25mm clearance.

**Inner2 (power plane):** Same structure, net = the most-connected non-GND power net in the design (selected by `connected_port_ids` count — e.g. VCC3V3 wins over VCC5 if more components connect to it). SMD pads don't penetrate to inner layers, so each SMD power pad gets a **via-in-pad plane stitching via**: the via is placed at the pad centre first (so the pad and via coincide — a physical, validatable connection), falling back to a small offset with a short stub trace if the centre is blocked by a foreign neighbour. Through-hole power pads already penetrate inner2 and need no via. Works on both sides — a bottom-side SMD power pad stitches down to inner2 identically. (Fine-pitch QFP power pins whose neighbours block every via site are reported as a warning rather than silently stranded.)

**Plane connectivity crediting:** Because a solid inner plane is one continuous pour, the routing connectivity validator (`_check_connectivity`) treats every same-net feature that *reaches* the plane — through-hole pads (which penetrate it) and vias that span to the plane layer — as mutually connected. Without this, a power net delivered by a single inner plane (no outer fill) would report each pad as its own disconnected group.

**Ordering requirement:** Power stitching vias must be computed *before* generating the inner planes so their positions are included in the antipad cutout pass. `apply_copper_fills()` enforces this order: (1) compute GND stitching vias, (2) compute power stitching vias, (3) generate inner1 plane with all via positions, (4) generate inner2 plane.

**DRC:** `check_inner_plane_antipad` verifies that every TH pad and via has a proper antipad clearance in each inner plane. The pass formula is `cutout_radius − (distance_to_cutout_center + pad_radius) ≥ clearance_min`. The cutout radius is measured as the polygon's **inscribed** (minimum-vertex) radius after dropping the duplicated closing vertex — the conservative, physically-correct nearest-copper distance (an earlier mean-vertex measurement double-counted the closing vertex and produced false failures).

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
- Pad *offsets* are written unrotated (KiCad rotates the offset by the footprint angle on load); the footprint angle is written **negated** so KiCad's clockwise rotation reproduces `build_pad_map`'s counter-clockwise layout — without this, every 90°/270° part's pads landed 180° off and traces connected to the wrong pad.
- **Pad rotation (size swap):** KiCad does NOT rotate an SMD pad's *rectangle* with the footprint angle on load — only the pad position rotates (verified via `pcbnew`: a −90° part's pads report orient 0 and keep their authored w×h in board frame). So the exporter **pre-swaps pad width/height for 90°/270° rotations** (mirroring `build_pad_map`), or a rotated fine-pitch part's long pads overlap their neighbours (e.g. a 0.5 mm-pitch FFC connector whose 1.3 mm-long pads, left un-rotated, overlap into ~45 pad-pad shorts). See *Fine-Pitch Escape* for the full story.
- Pad offsets rounded to 4 decimal places to avoid floating-point noise that breaks trace-to-pad connectivity
- Through-hole pads declared on `*.Cu` (all layers); SMD pads on component layer only
- Zone clearance set to 0.5mm to prevent fill flooding between dense TH pin fields (e.g., DIP-28 at 2.54mm pitch)
- Stitching vias excluded from the via-exclusion radius of any component pad — including no-net pads (mounting / NC) — to prevent shorts
- **Sibling `.kicad_pro`** carries the rules the board was actually routed to (clearance, track width, and `min_via_diameter`/`min_through_hole` floored at the smallest via actually present — e.g. 0.45/0.2 mm escape vias), so `kicad-cli` DRC checks the real rules instead of its 0.2 mm defaults.

**KiCad CLI integration:**
- DRC: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc --format json`
- Render: `kicad-cli pcb render --side top` for automated board image generation
- Used for programmatic DRC validation during development

### Import (`exporters/kicad_importer.py`)

Parses `.kicad_pcb` files back into the pipeline's routed JSON format. Enables the workflow: export → manual fix in KiCad → re-import for Step 5 (DRC) and Step 6 (Gerber).

**Parsed elements:** Net declarations, footprint positions/pads, segments (traces), vias, zones. Components matched back to netlist by designator and pin net assignments.

### Netlist Import (`exporters/kicad_netlist_importer.py`)

Lets a **mid-stream KiCad project** (schematic already drawn by another tool/agent) continue inside pcb-creator without starting over. Converts a KiCad netlist export (`.net`) or schematic (`.kicad_sch`) into the pipeline's `circuit_schema` netlist JSON:

- **`.net`** (KiCad: File → Export → Netlist) — the reliable input, carries full connectivity + component metadata.
- **`.kicad_sch`** — used for component metadata; connectivity is read from a sibling `.net` of the same stem (a `.kicad_sch` alone has no wire connectivity in agent-generated files).

Inference fills the metadata KiCad doesn't carry: `component_type` from reference-designator prefix, `net_class` (power/ground/signal) from net name, `electrical_type` per pin from net class + component type. Footprint library prefixes are stripped, IDs are normalized to the schema patterns, single-node nets are dropped with warnings. Exposed to agents via the `import_kicad_netlist` MCP tool, which writes `<project>_netlist.json` ready for the placement stage.

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

**DFM profile resolution:** Loads from `requirements.manufacturing.manufacturer` (LLM-generated format) or top-level `requirements.manufacturer` (hand-written test format), falls back to `generic`. Explicit values in `requirements.manufacturing` override profile defaults. **With the `generic` profile (no manufacturer chosen), the trace-width / clearance / via-drill / annular minimums are floored to the rules the board was actually *routed* to (0.127 mm etc.) and the smallest via actually present** — a fine-pitch board legitimately tightens to 0.127 mm, which is manufacturable but false-fails the conservative 0.25/0.2 mm generic default (this otherwise produced hundreds of bogus `trace_width_min` / `clearance_min` violations on an escape-routed board). A named manufacturer keeps its real limits (routing already respected them).

**Available DFM profiles** (in `validators/engineering_constants.py`):

| Profile key | Description | Min trace | Min clearance | Min via drill |
|-------------|-------------|-----------|---------------|---------------|
| `jlcpcb_standard` | JLCPCB 2-layer standard | 0.127mm | 0.127mm | 0.3mm |
| `jlcpcb_4layer` | JLCPCB 4-layer (JLC7628 stackup, 1oz outer / 0.5oz inner, 1.6mm) | 0.127mm | 0.127mm | 0.3mm |
| `pcbway_standard` | PCBWay standard 2-layer | 0.127mm | 0.127mm | 0.3mm |
| `oshpark_2layer` | OSH Park 2-layer (ENIG, purple soldermask) | 0.152mm | 0.152mm | 0.254mm |
| `generic` | Conservative fallback | 0.200mm | 0.200mm | 0.3mm |

**Report format:** `{project}_drc_report.json` with per-check pass/fail, violation details (location, measured value, required value, net), and aggregate statistics.

**Authoritative engine (kicad-cli).** The internal `drc_report` above is the portable, dependency-free baseline, but its geometry (pad shorts, antipads, zone connectivity) and exact short *count* don't match what the fab sees. **When kicad-cli is installed, `stages.run_drc` supersedes the report with KiCad's own DRC** (`validators/kicad_drc.py`): it exports the board (zones poured via `pcbnew`, rules carried by the sibling `.kicad_pro`), runs `kicad-cli pcb drc`, and shapes the result into the *same* report dict (mapping KiCad violation types → the same rule names, keeping the internal current-capacity check, which KiCad has no rule for). This is the same authority the short-cleanup pass uses, so route ↔ DRC agree, and it's the same engine the manufacturer runs. The internal report is the fallback when kicad-cli is absent. Without this, the internal validator reported ~500 violations on a fine-pitch board (mostly the rule-mismatch false-fails above plus geometry disagreements) where KiCad reports ~10–20 — making every escape/fine-pitch board look broken to agents.

**Output:** `validators/drc_report.py:run_drc()` returns the portable report dict; `stages.run_drc` returns the kicad-cli-authoritative one when available. Runner prints summary, saves JSON, and passes to the approval gate viewer for display.

## Step 6: Output Generation

Produces manufacturer-ready files in `projects/{name}/output/`. All files are structured for direct upload to JLCPCB, PCBWay, or similar manufacturers.

**Output files:**
- **Gerber RS-274X** (`gerber_exporter.py`): F_Cu, B_Cu, F_SilkS, B_SilkS, F_Mask, B_Mask, F_Paste, Edge_Cuts — plus In1_Cu and In2_Cu for 4-layer boards — generated using the `gerber-writer` library (100% spec compliant). Board outline supports both rectangular and arbitrary polygon shapes (from DXF). Silkscreen uses a stroke vector font (`stroke_font.py`) for text rendering (A-Z, 0-9, symbols). Fiducials are 1mm copper dots with 3mm solder mask openings.
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

#### Small-model strategy (Step 1)

Beyond normalization, several mechanisms make netlist generation reliable down to a ~35B-A3B MoE / ~9–14B-dense floor:

- **Chunked generation** (`_generate_chunked`): above a size threshold the netlist is built in phases — components → ports (batched ~8 components/call) → nets (split into a power/ground call then a signal call over a compact port table). Small models corrupt one giant JSON far more often than a sequence of small focused ones. `PCB_MODEL_PROFILE=small` lowers the threshold (8000→5000 chars) and batch sizes so weaker models chunk sooner.
- **Reasoning-tag stripping** (`_strip_reasoning`): local MLX Qwen/DeepSeek builds emit the answer inside a `<think>…</think>` block, close it, then *re-emit* it. Naive first-brace-to-last-brace extraction spans both copies and is invalid JSON; the extractor takes the content after the last `</think>`. Applied in `extract_json` and `_extract_json_array`.
- **Repetition guard** (`truncate_repetition` in `litellm_client.py`): a looping model can emit the same block until the token budget is exhausted (observed: 111K chars of repeated nets). The guard truncates at the start of the repeated span so the valid prefix survives for repair, and stops requesting further continuations.
- **JSON repair** (`extract_json` / `_extract_json_array`): trailing-comma and single-quote-key fixes, plus salvage of a truncated array by closing at the last complete object — on top of the existing brace-imbalance repair.

Live results: Qwen3.6-35B-A3B (the floor target) takes the prior-crashing test boards from 0/7 to 7/7 *generation* success; routing/DRC quality then depends on board density and effort level.

### Planned: Routability-Driven Placement

**Motivation.** The SA placement optimizer minimizes *proxies* for routability — MST wirelength, MST crossings, pad-density congestion — but never models where traces actually need to go. On dense boards this plateaus: a representative dense 4-layer board (73 parts, a 30-pin 0.5 mm FFC connector — referred to below as "the dense reference board") autoroutes to ~77% and the unrouted nets all cluster around the connector's fanout (its signal escapes + the driver nets they feed). The wall is local congestion / fanout space, not global capacity. These enhancements make placement *routing-aware*. Implement one at a time; **gate each on `scripts/eval_boards.py` (no regression on `test/requirements`) + a re-route of the dense reference board**, since a routability heuristic that helps one board can hurt another. Goal is to push the autoroutable fraction up (~77% → ~90%+), not to 100% — some fine-pitch fanouts still finish best by hand.

Highest-leverage first:

- **A. Escape-halo / fanout reservation around dense parts.** ✅ **Implemented** (see *Escape-halo / fanout reservation* under Placement Optimizer). Per-part fanout demand → annulus-bound halo → layer-aware penalty on intruding foreign pads, self-gating to dense/fine-pitch parts. On by default.
- **B. Congestion-driven placement on routing *demand* (not pad count).** ✅ **Implemented, off by default** (see *Routing-demand congestion / RUDY* under Placement Optimizer). RUDY heatmap + over-capacity penalty, self-gating. The empirical surprise: none of our boards — including the dense reference board — are channel-congestion-limited (it peaks ~7% over capacity and is fanout-limited, not congestion-limited), so B is a proven no-op on the current suite and its biting weight is unvalidated. Kept + unit-tested, dormant until a congestion-limited board exists to tune against. The lesson reorders the roadmap: **D is the real lever for the fanout-limited case.**
- **C. Localized routing-feedback re-place.** ✅ **Implemented** (see *Localized routing-feedback re-place* under Placement Optimizer). The incomplete route's `unrouted_nets` map back to their components, which get an enlarged escape halo on retry instead of the old global +0.5mm clearance bump.
- **D. Connector fanout orientation + pin-ordered neighbours.** ✅ **Orientation implemented, gated on pin count** (see *Connector fanout orientation* under Placement Optimizer); wide ≥10-pin edge connectors are rotated long-axis-along-edge, and a two-sided flip guard protects mis-typed high-pin/fine-pitch parts. Reorienting *all* connectors regressed small-terminal-block boards, hence the gate — which makes D inert on the current suite (≤6-pin connectors) and on a fine-pitch FFC mis-typed as a passive. **Remaining:** seed a connector's fed components in pin order, and broaden the trigger to high-pin/fine-pitch parts mis-typed as passives. Effort: remaining work moderate.
- **E. Pin-pitch-scaled clearance.** Generalize the single global `min_clearance_mm` to per-component: fine-pitch / high-pin parts get a larger keepout automatically (a lighter cousin of A — A now reserves *fanout* space via a soft penalty; E would harden it into the overlap constraint). Effort: low.
- **F. Decoupling caps under the IC (two-sided).** Extend the layer-flip move to place a chip's decoupling caps on the bottom directly beneath it — frees the IC's top-side escape perimeter and improves decoupling. Today caps scatter by congestion, competing for escape channels. Compounds with A. Effort: moderate.

**Verdict (2026-06-13), after building and isolating A–D on the dense reference board:** *placement was not its bottleneck.* The clean A-isolation (same repaired placement, route with `escape_weight` 0 vs 6) routed **identically-or-worse with the halo on** (97.9% off → 95.8% on, A-on Freerouting timed out) — the board was never escape-*space*-limited, so spreading neighbours only lengthens traces. B is inert (not congestion-limited), D is inert (the connector mis-typed + suite connectors small). All three are committed and validated as **no-regression**, and may still help connector-heavy / congestion-bound boards — but none moved this one. **E and F would also be inert here** (more placement spreading the board doesn't need), so they are deprioritized. The real wall was **router-side**, addressed by the next section. A/C remain genuinely useful as the routing-feedback retry scaffold; B/D/E/F are parked as situational.

### Fine-Pitch Escape / Fanout Pre-Routing (`optimizers/escape_router.py`)

**Status: v2 implemented, AUTO-enabled on fine-pitch boards** (`config.escape_fanout` is tri-state: `None` = auto-enable when a part is below `FINE_PITCH_THRESHOLD_MM`; `PCB_ESCAPE_FANOUT=true/false` forces it). `generate_escape_routing` detects single-row/column fine-pitch parts and emits the **full breakout** for every escaping pin — pad → short stub → staggered through-via → a deterministic **onward fanout trace** that threads the gaps between the far via row and exits on a clean release line at the pad pitch — all returned in routed-schema form and fed to Freerouting as `fixed_routing` `(type protect)` wiring on a fresh route. The autorouter therefore starts from a comfortable-pitch grid *clear of the pad field* and never has to fan the group out itself. Unit-tested in `tests/test_escape_router.py`. **A 30-pin 0.5 mm FFC connector that previously could not be made DRC-clean now routes 0 shorts / 100%.** Multi-row / quad parts (QFP/QFN) are still skipped (left to the autorouter).

**The root-cause correction (important).** When a dense 4-layer board with a 30-pin 0.5 mm FFC connector failed routing DRC (~86 errors, mostly `shorting_items`), it looked like an unroutable fine-pitch fanout. It wasn't — it was a **systematic exporter bug**: `kicad_exporter` wrote SMD pads with their raw, unrotated `(size w h)`, but **KiCad does not rotate an SMD pad's rectangle with the footprint angle on load** (verified via `pcbnew.GetBoundingBox` — a −90° part's pads report orient 0 and keep their authored w×h in board frame). So the connector's 1.3 mm-long pads stayed oriented along the 0.5 mm-pitch axis and **overlapped their own neighbours → ~45 pad-pad shorts** plus the track-pad shorts those caused. Freerouting was fine all along (its DSN image rotates pad shapes); only the KiCad export was corrupt. Fix: the exporter pre-swaps pad w/h for 90°/270° (mirroring `build_pad_map`); see *Export → Pad rotation*. With that fixed, the escape router's breakout is what makes the connector route cleanly.

**At 0.5 mm pitch the breakout is mandatory:** 0.27 mm-wide pads at 0.5 mm pitch leave a 0.23 mm gap — narrower than one 0.127 mm trace + clearance — so *no* trace can pass between adjacent pads. The escape geometry:
1. **Detect** the fine-pitch part (reuse `_min_pad_pitch` / `FINE_PITCH_THRESHOLD_MM`); determine the escape axis (perpendicular to the pad row/column, toward the board interior).
2. **Two staggered via rows** (alternating distance by pin parity) so Ø0.45 mm escape vias clear each other at the pad pitch. The near-row pins' onward traces thread the gaps *between* the far-row vias (≈0.2 mm clearance) — placed deterministically because the generic autorouter can't reliably hit those channels.
3. **Stackup-aware drop layer:** signal pins fan out on an inner *signal* layer (In2 when `plane_layers=1`), never a plane. **GND (plane-net) pins drop straight to the GND plane** with a via and no onward trace — which also connects the GND pads Freerouting otherwise left floating.
4. **Keepouts for plane-net escapes:** a GND stub/via is excluded from the routing netlist, so Freerouting can't see it; the escape router emits Specctra `(keepout)` circles along each so other nets don't route over them (clears the near-connector GND-via shorts).
5. Everything is emitted as **protected wiring** via the existing `fixed_routing` path.

Two supporting DSN fixes were required (both under *DSN export*): **through-hole padstacks now span every routing layer** (was F.Cu/B.Cu only → Freerouting routed inner-layer traces straight through TH pads), and copper-fill **stitching/rescue vias avoid inner-layer signal traces and no-net pads** (a through via dropped on either shorts it).

**Short-cleanup pass (`optimizers/route_cleanup.py`, integrated in `run_routing`).** Freerouting's first pass still occasionally clips a through-hole pad (gate-driver / header / SWD pins) in congestion. After routing, the cleanup exports the board, runs **kicad-cli DRC** for the authoritative reroute-fixable violations — `shorting_items` **and clearance violations** (`clearance`/`track_clearance`/`via_clearance`/`hole_clearance`/…, both nets of a two-item clearance collected), ∪ `incomplete_net_ids` — rips those nets, and re-routes them with everything else — **including all escape wiring** (matched by coords and preserved, so a fine-pitch net never loses its breakout) — held as protected wiring; it accepts the re-route only if fewer bad nets remain (max 2 iterations). Gated by `config.short_cleanup` / `PCB_SHORT_CLEANUP` (default on), a no-op without kicad-cli. The bad-net list MUST come from kicad-cli — the internal geometric short-check disagrees with KiCad's geometry. On a dense board it drives the through-hole-pad shorts (and via-to-net clearance nicks) to zero (occasionally leaving one in a genuinely congested spot). Hole-to-hole / drill-spacing violations are NOT in this list — they aren't fixable by re-routing a net; those are prevented at via generation (next paragraph). The dependency-injected core is unit-tested in `tests/test_route_cleanup.py`.

**Stitching-via hole-to-hole spacing (`_filter_via_hole_spacing` in `apply_copper_fills`).** GND/power stitching vias are placed on a coarse grid, but a stitching via can still land within the hole-to-hole minimum of a Freerouting routing via (or another stitching via) — two GND vias 0.25mm apart tripped a `hole_to_hole` DRC error. Because stitching vias are *redundant* plane connections, the merge step drops any new stitching via whose drill would sit closer than `via_drill_mm + HOLE_TO_HOLE_MIN_MM` (0.5mm edge-to-edge) to an existing routing via or an already-kept stitching via — the existing routing vias are never dropped. This removes the violation at the source rather than reactively. Unit-tested in `tests/test_via_spacing.py`.

### Future Enhancements

- **Manufacturer quoting (Step 7)**: Auto-submit BOM, Gerbers, assembly files to manufacturer APIs (JLCPCB, PCBWay, OSH Park) for fabrication + assembly quotes. Present comparison to user. Steps 5-6 produce submission-ready files in manufacturer-expected formats. Blocked on: vendor API availability or TOS review for agent accounts + Playwright.
- **build123d for richer 3D models**: Replace hand-written STEP BREP boxes with build123d parametric shapes (rounded IC bodies, pin legs, LED domes, etc.). Blocked on: build123d/cadquery adding Python 3.14 support (requires OpenCascade `cadquery-ocp` wheels).
- **6-layer stackup**: For boards that exceed 3 signal layers + GND plane (e.g. `sig/GND/sig/sig/PWR/sig`), the principled next step over dropping to `plane_layers=0` on 4-layer. Extends `_COPPER_LAYERS_BY_COUNT` and `inner_plane_count`.
- **Code-enforced approval gate**: The route-failure ladder marks its two physically-constrained rungs (2→4 layer promotion, board enlargement) with `requires_user_approval: True` on `next_step`, and the message text itself instructs the agent to ask the user first (see the MCP "Route-failure escalation ladder" note). That gate is only as strong as the agent's compliance — it's advisory metadata + prose, and there is no programmatic `next_step` executor that could bypass it today (verified on the Hermes deployment). To make it a *hard* guarantee independent of agent behavior, gate the underlying mutations in code: have `optimize_placement(layers=4, …)` and any board-enlargement require an explicit `approved=True` argument and `fail()` with a remediation that asks for it when absent. Then a misbehaving or future programmatic caller cannot silently change stackup/cost or board size — the check lives in the tool, not the prompt.
- **Place-and-route co-optimization (route-aware placement)**: The SA placer minimizes *proxies* for routability (MST wirelength/crossings, pad-density/RUDY congestion, escape halos), never actual routing. Evidence that this is loosely coupled to real routing success: on a dense reference board, isolating the escape-halo term moved routing completion only ~2% and in the *wrong* direction (97.9% off → 95.8% on), and the routability plateau held across every placement variation — so the placer sits at a *proxy*-optimum, not a routing-optimum. A truly route-aware placer would close the loop: run a fast **global router** (or a congestion estimator stronger than RUDY) *inside* the placement loop and feed real per-region congestion / unroutable-pair signals back into the SA cost, or iterate place→route→rip-up-worst-region→re-place. This is the principled path to "best placement for routing" but a much larger system than proxy-SA. Caveat: it only helps boards that are genuinely *placement*-limited — that reference board turned out not to be (its dominant failure was an exporter bug, then a fine-pitch escape problem, neither of which placement can fix), so validate that a target board is placement-bound (routability sensitive to placement changes) before investing. The existing routing-feedback retry (`run_route_with_retry` → `focus_components`, enhancement C) is a coarse first step in this direction (one place→route→re-place iteration using the real `unrouted_nets`).

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

**Two pin sources — and unpinning must clear both.** A pin can live in *either* the durable store (`<project>_placement_pins.json`, written by `place_component`/`set_component_positions`) *or* the placement file's `placement_source=="user"` flag (set from requirements hints, and mirrored from the durable store every time `run_placement` injects pins). `run_placement` re-scrapes the placement file's user flags on each run and re-injects them — so clearing only the durable store left the placement-file flag behind, and the very next `optimize_placement` **resurrected the pin** (the "keeps treating TB3 as pinned from the old placement file" symptom, where the agent could never actually unpin it). `clear_placement_pin`/`unplace_component` and `clear_all_pins` now clear **both** sources, and `run_placement` returns `pinned_components` — the union of both sources — so the agent always sees the true fixed set instead of guessing. `stages.all_pinned_designators` is the single helper that computes that union.

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
- **Single stage implementation**: Both `run_workflow` (CLI) and `run_workflow_streaming` (Gradio) delegate routing / DRC / output to `orchestrator/stages.py` (`run_routing` / `run_drc` / `run_export`) — the same functions the granular MCP tools call — so there is exactly one implementation of each. The runner re-reads `<project>_routed.json` (written by `run_routing`) to get the in-memory board for the steps that follow (approval gate, vision review, `--export-kicad`). Stage functions take an optional `log` callable: the CLI passes `print` to preserve its console output verbatim, while MCP and the Gradio generator leave it unset (silent). `run_routing` returns `validation_errors`/`validation_warnings` that the streaming path records via `project.update_status`.
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

**CLI (recommended for shell-based agents like Agent Zero):** Agents write a requirements JSON file to disk (avoiding MCP transport encoding issues with large payloads), then run `pcb-creator run --requirements file.json --agent-mode --skip-qa --json-output`. Use `pcb-creator schema` to get the expected format. The `--project` flag is optional — auto-generated from the requirements JSON. The `--json-output` flag prints structured results (success, routing stats, DRC, output files) to stdout. Attach DXF board outlines or other files with `--attach file.dxf` (repeatable).

### MCP Server

The MCP server (`mcp_server.py`) exposes the pipeline as tools for any MCP-compatible AI agent (Claude Code, Agent SDK, OpenClaw, Agent Zero, Hermes). Uses FastMCP with stdio transport, runs headless.

**Projects directory:** `~/.pcb-creator/projects/` (configurable via `PCB_PROJECTS_DIR` env var). Persists across sessions — any agent can resume or inspect previous designs.

**Response envelope (`mcp_envelope.py`).** Every tool returns a uniform shape so a small client model always knows what to do next: `ok(data, next_step)` carries a machine-readable `next_step` `{tool, args, why}`; `fail(error, remediation)` carries `remediation` `[{option, tool, args}]` recovery options; `working(...)` carries `state: "running"`, `poll_again_in_s`, and a `status_hint` for async tools. `get_workflow_guide()` returns the ordered tool sequence for each workflow so a cold-start agent can self-orient. This is the highest-leverage feature for driving the pipeline from a weaker local model — it never has to infer the next call from prose.

The server supports **three workflows**:

**0. Build from scratch (incremental, best for small client models).** Instead of emitting one large netlist JSON (which ≤14B models get wrong ~3/4 of the time), the agent makes many small validated calls: `create_circuit` → `add_component` (returns the resolved pin table; footprint is gated at add time) → `connect_pins` (by pin number or name, e.g. `"U1.7"`/`"D1.anode"`; net class auto-inferred) → `finalize_circuit` (compiles the canonical `<project>_netlist.json`, runs full validation + the footprint gate). Rework with `list_circuit`, `remove_component`, `disconnect_pins`, `mark_no_connect`. The artifact is identical to the KiCad-import output, so placement → routing → DRC → export work unchanged.

**1. Autonomous (one shot) — `design_pcb`.** Runs the whole LLM-driven pipeline (requirements → schematic → BOM → placement → routing → DRC → output). **Async:** returns immediately with `state: "running"` and works on a daemon thread; poll `get_project_status` for `design_state` (`running`/`complete`/`failed`), `design_progress` (live step), and `design_result` when complete. Single-flight: re-invoking for a project already running returns the in-progress job. Best when you want pcb-creator to do everything, but note it runs its own nested LLM + vision-critic loop you cannot see into. Two input modes:
- **Structured (preferred for agents):** Pass `requirements_json` dict directly — skips LLM translation. Call `get_requirements_schema()` first.
- **Natural language:** Pass a plain-text `description` — translated to structured requirements via LLM.

**2. Granular (agent-driven, recommended when the *caller* is already an agent).** When an external agent already does circuit design and its own critic/QA, running `design_pcb` nests two autonomous loops — an opaque inner LLM + vision-critic rework loop that can blow the MCP timeout. The granular tools avoid this: each is **deterministic (no LLM, no vision critic)**, returns quickly, and never hides a rework loop, so the calling agent owns the loop. Backed by `orchestrator/stages.py` (`run_placement` / `run_routing` / `run_drc` / `run_export`), the single deterministic implementation of those stages.

Flow: `import_kicad_netlist` → (`verify_footprints` / `provide_footprint` until clear) → `optimize_placement` → `route_board` → poll `get_project_status` → `run_drc` → `export_outputs`. The agent evaluates DRC violations itself and decides on rework; it reviews the board with `get_board_image` instead of an internal vision critic. The footprint gate between import and placement guarantees no board is ever placed with 3 mm placeholder footprints — the deterministic equivalent of the LLM-flow's gather-time footprint verification.

**Tools:**

| Tool | Description |
|------|-------------|
| `design_pcb(description, project_name?, requirements_json?, settings?, attachments?)` | **Autonomous, async.** Starts the full LLM pipeline on a background thread and **returns immediately** (`state: "running"`). Poll `get_project_status` for `design_state`/`design_progress`; the final `design_result` carries routing stats, DRC summary, output file paths. Single-flight per project. |
| `get_requirements_schema()` | Returns the JSON Schema for structured requirements. |
| `get_workflow_guide()` | Returns the ordered tool sequence for each workflow (build / import / autonomous) — call first to self-orient. |
| `create_circuit` / `add_component` / `connect_pins` / `disconnect_pins` / `mark_no_connect` / `remove_component` / `list_circuit` / `finalize_circuit` | **Build-from-scratch.** Incremental circuit construction (`orchestrator/circuit_builder.py`); `finalize_circuit` emits the canonical netlist after full validation. |
| `place_component(project_name, designator, x_mm, y_mm, rotation_deg?, layer?)` / `unplace_component(...)` | **Granular.** Fix a component at exact coordinates (edge connector, mounting hole); validated immediately against board bounds and other pins, then never moved by `optimize_placement`. Pins persist in `<project>_placement_pins.json`. A rejected position (overlap / out-of-bounds) returns a **concrete free coordinate** (`suggested_x_mm`/`suggested_y_mm` + a ready-to-run remediation) so the agent retries there instead of looping on "move it." `unplace_component` clears the pin from **both** pin sources (see below) so it can't be resurrected. |
| `clear_all_pins(project_name)` | **Granular.** Unpin every component in one call — wipes the durable store and resets all `placement_source="user"` flags in the placement file. The clean-slate alternative to `unplace_component`-ing parts one at a time; returns the list of designators it unpinned. |
| `set_component_positions(project_name, positions, board_width_mm?, board_height_mm?)` | **Granular.** Bulk version of `place_component`: pre-position many components as `placement_source='user'` anchors (edge connectors, debug ports) in one call before `optimize_placement`. Pins are written to the **durable** `<project>_placement_pins.json` (not just the placement file's `placement_source` flags) so they survive a full placement regeneration — fixing the silent no-op where a later `optimize_placement` scattered batch-set pins. Fails loudly (rather than reporting success while pinning nothing) and surfaces any designators it could not pin. |
| `import_kicad_netlist(project_name, file_path, description?)` | **Granular.** Convert a KiCad `.net`/`.kicad_sch` into the project netlist (mid-stream handoff). No LLM. Runs the footprint gate and returns `unresolved_footprints`. |
| `verify_footprints(project_name)` | **Granular.** Resolve every component's footprint through the tiered lookup; returns `unresolved_footprints` (empty = clear). The same gate `optimize_placement` enforces. |
| `check_footprint_coverage(components, project_name?)` | **Granular.** Pre-flight: classify each BOM line by the resolution tier that resolves it (`custom`/`kicad_library`/`ipc7351`/`cache`/…) or flags it as unresolved — run before placement to find parts needing a custom footprint. |
| `provide_footprint(project_name, package, like_package?, pin_offsets?, pad_size?)` | **Granular.** Resolve an unknown footprint: alias to a known package (`like_package`) or supply explicit `pin_offsets` + `pad_size`. Cached with `source: "agent"`, `needs_review: true`. |
| `register_custom_footprint(project_name, package_name, kicad_mod_content)` | **Granular.** Write a `.kicad_mod` into the project's `custom-footprints.pretty/` (tier 0 — searched before the system KiCad library); found automatically thereafter. |
| `optimize_placement(project_name, board_width_mm?, board_height_mm?, seed?, two_sided?, plane_layers?)` | **Granular.** Deterministic grid placement → repair → SA (wirelength + crossings + congestion + escape halos). Synchronous. **Blocks with `unresolved_footprints` if any footprint is unresolved**, and **fails with a structured `violations` report** if pinned components overlap or overhang the edge. `two_sided=True` lets small SMD passives flip to the bottom (reluctant on 2-layer, free on 4-layer). `plane_layers` (4-layer) sets inner planes vs signal layers (2/1/0) — lower it to add routing capacity on dense boards. Board dims required on first placement; `seed` makes it reproducible. |
| `route_board(project_name, effort?, max_seconds?, auto_retry?, allow_grow?, keep_existing?)` | **Granular.** Starts routing on a background thread and **returns immediately** (`state: "running"`). `effort` = fast/normal/best (Freerouting passes + timeout); `auto_retry` (default on) — if the result is **near-complete (≥85%)** it finishes the residual nets *incrementally* (fast, protects the routed wiring), otherwise it re-places with extra clearance and re-routes once; keeps the better attempt either way. `keep_existing=True` does **incremental** routing (protect current wiring, route only unrouted nets) — use it to finish a partly-routed board. |
| `run_drc(project_name)` | **Granular.** The 14 deterministic routing/DFM design-rule checks; returns a severity-ranked summary (`top_violations`, `failing_rules` each with a `remediation_hint`). |
| `export_outputs(project_name)` | **Granular.** Gerbers/drill/BOM-CSV/CPL/STEP/ZIP into the project `output/` dir. |
| `list_projects()` | List all projects with status and output availability. |
| `get_project_status(project_name)` | Detailed status: step progress, routing stats, DRC pass/fail. Includes `routing_state` + `routing_progress` + `routing_elapsed_s` (poll after `route_board`) and `design_state` + `design_progress` + `design_elapsed_s` + `design_result`/`design_error` (poll after `design_pcb`). |
| `get_drc_report(project_name, verbose?)` | DRC summary by default (severity-ranked top violations + per-rule remediation hints); `verbose=True` returns the full per-check report. |
| `export_kicad(project_name)` | Export completed design to KiCad `.kicad_pcb` format. |
| `get_board_image(project_name, width?)` | Render routed board as base64 PNG image (for the agent's own visual review). |

**Incremental routing.** `route_board(keep_existing=True)` / `run_routing(fixed_routing=...)` emits the existing traces/vias into the DSN `(wiring)` section as Specctra `(type protect)` wires; Freerouting keeps them and routes only the remaining ratsnest, and the SES it writes echoes protected + new wiring. Placement is not re-run (existing traces stay valid) and a post-route union restores any protected wiring a degenerate empty SES would drop. Used to finish a hand-started or KiCad-imported board (`exporters/kicad_importer.import_kicad_pcb` extracts the existing routing) rather than re-routing from scratch.

**Async routing & anti-abandonment:** `route_board` runs `stages.run_routing` on a daemon thread and records state in an in-memory job registry (`_ROUTE_JOBS`) keyed by project; `get_project_status` reports `routing_state` and reconciles with the on-disk `_routed.json` so state survives an empty registry (e.g. server restart). **Both engines stream live progress** into `routing_progress`: Freerouting's stdout is parsed for per-pass `{pass_num, incomplete_connections, score, elapsed_s}` (with a ~10s heartbeat counter so the poller always sees forward motion even between passes), and the built-in NCR router fires a per-iteration callback (`{iteration, max_iterations, legal_nets, ...}`). While routing, `get_project_status` always includes a `status_hint` ("pass 3/20, 7 connections incomplete — poll again in ~15s; do not run other tools or external CLIs") — this is what stops a client agent from giving up mid-route and reaching for KiCad itself. The `poll_again_in_s` cadence is **adaptive** (`_poll_interval`): 15s for the first ~30s, 30s out to ~3 min, 60s thereafter, and the `status_hint` tells the agent to wait that interval rather than busy-polling a multi-minute job. Because agents routinely ignore that advisory, the cadence is **also enforced server-side** (`_throttle_poll`): while a route/design job is running, `get_project_status` records each response time per project, and a poll that arrives sooner than the adaptive interval is *held* (a plain `time.sleep` in the threadpool-run sync tool, so the asyncio loop and other projects are unaffected) until the interval elapses — capped at `_MAX_POLL_BLOCK_S` (default 25s, `PCB_MAX_POLL_BLOCK_S`) so a single call never approaches the client's per-tool timeout. A caller that already waited the recommended interval is not delayed at all; an idle project is never throttled. The registry is re-read after the hold so the response still carries fresh progress. All pipeline modules log to **stderr** (never stdout) so the stdio JSON-RPC stream is never corrupted.

**Route-failure escalation ladder (`_route_failure_next_step`).** When a route comes back incomplete/failed, the failure's `next_step` isn't a bare error — it's the *next thing to try*, chosen by reading the current placement so the ladder advances as the agent acts: (1) 2-layer → 4-layer `plane_layers=2`; (2) `plane_layers=2 → 1` (reallocate an inner plane to signal — free); (3) `plane_layers=1 → 0` (free the last inner plane — free); (4) `plane_layers=0` → enlarge the board ~15%. Capacity is added *first* (the free stackup moves), and the two rungs that change something the user likely constrained — the layer count (rung 1, raises cost/changes the stackup) and the board dimensions (rung 4, often fixed by an enclosure/mating part) — set `requires_user_approval: True` **and** spell out "ASK THE USER … do not do this on your own" in the message. The boolean is machine-readable metadata for any caller that chains `next_step`; the prose is the guard for the LLM agent that actually consumes it (see the "Code-enforced approval gate" future enhancement for making this a hard, in-code gate). Behavior is locked by `tests/test_agent_sim.py`.

**Freerouting process lifecycle (`optimizers/freerouter.py`).** The router owns its JVM child end-to-end so it never orphans: the subprocess is tracked and torn down in a `try/finally`, an `atexit` hook + SIGTERM/SIGINT handlers (installed from `mcp_server.main()` on the main thread) kill any live JVM on server shutdown, and each `route_board` start **reaps orphaned Freerouting JVMs** — POSIX-only, keyed strictly on `ppid == 1` **and** our jar path, so it never touches a live route or an unrelated `java` process. Verified empirically that normal/timeout/OOM/abandonment/parent-death all leave no stray `java`/`pcbnew`/`kicad-cli` processes and no leftover temp dirs.

**Async design:** `design_pcb` uses the same pattern — a daemon worker runs the full pipeline and records state in `_DESIGN_JOBS`; `get_project_status` reports `design_state`/`design_progress`/`design_result`/`design_error`/`design_elapsed_s`, and reconciles with on-disk `STATUS.json` so a respawned server can still report design state. Because the project dir may not exist yet during the early (pre-mkdir) phase, the status tool consults the design registry *before* its "project not found" check, so an in-flight or early-failed design is never reported as missing. This decouples the long autonomous pipeline from the MCP transport timeout, but the nested LLM + vision-critic loop remains opaque to the caller — agents with their own QA should prefer the granular tools.

**Configuration:** Same `PCB_*` environment variables as the CLI. The server forces `agent_mode=True`, `skip_qa=True` (calling agent reviews results itself), `max_rework_attempts=3`, and `llm_timeout=300`. The granular tools ignore the LLM settings entirely (they make no model calls). `design_pcb` overrides can be passed per-call via `settings`.

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

3. **Deterministic validation catches most errors.** The 14 routing/DFM DRC checks (plus the netlist-level electrical checks in Step 1) catch the same classes of errors that ECAD tools flag (KiCad DRC, Altium). The LLM QA adds value for requirements compliance but is not the primary safety net. This is also why the granular agent-driven MCP flow keeps DRC as a first-class deterministic stage while dropping the vision critic.

4. **Resistor power needs circuit-aware calculation.** Using a LED's max rated current (20mA) instead of the actual operating current (V_supply - Vf) / R produces false positives. The DRC traces the circuit topology to compute real power dissipation.

5. **Shared constants prevent drift.** Having `engineering_constants.py` as the single source of truth for the Python side, while `engineering_rules.md` serves as prose guidance for the LLM, means the Python always enforces the correct values regardless of what the LLM remembers.

6. **Small models work.** The entire pipeline — gather, generate, validate, QA — runs on Qwen3.5-27B (27B parameters). Total context per call is under 15K tokens. This is viable for local inference on a Mac Studio.

7. **LLMs can't do spatial math.** For complex boards (21+ components), LLMs consistently generate overlapping placements. The SA repair algorithm resolves these in seconds — this hybrid approach (LLM for semantics, algorithm for geometry) is far more robust than asking the LLM to retry.

8. **Repair mode enables weaker models.** With repair, even a local 27B model can handle the Arduino Uno (21 components). The LLM gets approximate positions right, and the algorithm fixes the geometry. Without repair, the same model fails all rework attempts.

9. **Thinking models waste tokens on structured output.** Qwen3.5's thinking mode produces reasoning preamble before JSON, consuming tokens and sometimes breaking JSON extraction. Use `--no-thinking` to disable this via `extra_body`. With thinking disabled, the full pipeline (Steps 0-4) runs in ~4 minutes on local Qwen3.5-27B vs 2+ hours with thinking enabled. As a safety net, `_strip_reasoning` also handles models that still emit (and sometimes duplicate) a `<think>…</think>` block even with thinking nominally disabled — common on local MLX builds.

10. **QA LLMs hallucinate failures on valid output.** Small models frequently reject valid outputs — e.g., claiming placement JSON "lacks netlist data" or inventing calculation errors. The fix: when the Python validator passes with 0 errors, QA failures are overridden to warnings. The deterministic validator is the authoritative gate, not the LLM QA. This prevents rework loops on phantom issues.

11. **Step-specific QA prompts matter.** The QA template must tell the LLM what to check per step. Step 3 (placement) should only check spatial properties, not electrical connectivity — that was verified in Steps 1-2. Without step-specific guidance, small models apply all checks indiscriminately and fail on inapplicable criteria.

12. **Through-hole pads must be marked on both layers.** The initial implementation only blocked TH pads on the component layer, allowing bottom-layer traces to route through TH pad copper — producing hundreds of DRC shorts in KiCad. Marking TH pads on both layers with asymmetric clearance (full pad on component layer, drill-sized on opposite layer) eliminated most shorts but significantly reduced routability between dense TH pin fields. This is physically correct — routing between DIP-28 pins at 2.54mm pitch with 1.6mm pads leaves <1mm clearance.

13. **KiCad coordinate conventions differ subtly.** KiCad uses Y-down coordinates and applies footprint rotation to pad offsets internally. Our pipeline uses Y-up (standard math CCW). The pad Y-offset must be negated in the KiCad export to produce matching absolute pad positions. Floating-point noise in pad offsets (e.g., `3.8099999999999987` instead of `3.81`) causes KiCad to compute pad centers that don't match trace endpoints — rounding to 4 decimal places fixes this.

14. **KiCad CLI enables programmatic DRC.** `kicad-cli pcb drc --format json` runs full DRC without opening the GUI, returning machine-readable violation reports. This is invaluable for iterating on export fixes — each change can be verified in seconds instead of manually inspecting in the KiCad editor.
