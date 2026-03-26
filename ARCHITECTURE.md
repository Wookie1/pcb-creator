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
│  │ LLM       │→ │ Python     │  │  LLM translates, Python validates
│  │ translate  │  │ validate   │  │
│  └───────────┘  └────────────┘  │
│  ┌───────────┐  ┌────────────┐  │
│  │ LLM       │→ │ Python     │  │  LLM looks up specs, Python merges
│  │ datasheet  │  │ enrich     │  │
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
   Step 6: Output Generation (planned)
```

## Directory Structure

```
pcb-creator/
├── orchestrator/               # Python orchestration engine
│   ├── cli.py                  # CLI: run, design, validate, import-kicad
│   ├── runner.py               # Sequential step executor (Steps 0-5 + approval gate)
│   ├── config.py               # Model, router engine, paths, limits
│   ├── project.py              # Project directory & file I/O
│   ├── approval_server.py      # Ephemeral HTTP server for post-routing approval gate
│   ├── steps/
│   │   ├── base.py             # StepBase abstract class, StepResult
│   │   ├── step_0_requirements.py  # Validate + calculate + copy attachments
│   │   ├── step_1_schematic.py     # Generate + validate + QA netlist
│   │   ├── step_2_bom.py           # Generate + validate + QA BOM
│   │   └── step_3_layout.py        # Generate + validate + repair + QA placement
│   ├── gather/
│   │   ├── conversation.py     # Interactive requirements gathering + enrichment
│   │   ├── calculator.py       # LED resistor / power calculations
│   │   └── schema.py           # Requirements JSON schema (board, hints, attachments)
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
│   ├── pad_geometry.py         # Pad position database (package → pin offsets)
│   ├── freerouter.py           # Freerouting integration (auto-download, DSN→SES flow)
│   └── router.py               # Built-in A* router + IPC-2221 + copper fills + silkscreen
├── exporters/                 # Board export/import
│   ├── kicad_exporter.py      # Export routed JSON → .kicad_pcb (KiCad 9)
│   ├── kicad_importer.py      # Import .kicad_pcb → routed JSON
│   ├── dsn_exporter.py        # Export placement → Specctra DSN (for Freerouting)
│   └── ses_importer.py        # Import Specctra SES → routed JSON (from Freerouting)
├── validators/                 # Deterministic validation
│   ├── validate_netlist.py     # Schema + referential integrity + DRC
│   ├── validate_placement.py   # Schema + cross-ref + boundary + overlap + rules
│   ├── validate_routing.py     # Schema + trace clearance + via clearance + connectivity
│   ├── drc_checks.py           # 9 netlist-level design rule checks
│   ├── drc_checks_dfm.py       # 8 DFM checks (trace width, via, annular ring, current capacity)
│   ├── drc_report.py           # Consolidated DRC report (electrical + DFM + current + mechanical)
│   └── engineering_constants.py # Constants, DFM profiles (JLCPCB, OSH Park, PCBWay, generic)
├── visualizers/                # Board visualization tools
│   └── placement_viewer.py     # Interactive viewer (routing, fills, DRC, export/import, approval)
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
│           ├── {name}_board.step      #   bare PCB 3D model
│           └── {name}_gerbers.zip     #   all Gerbers + drill for upload
├── .env                        # API keys (gitignored)
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
| `gather_translate` | Translator | Natural language | Requirements JSON |
| `gather_datasheet` | Datasheet lookup | Component info | Specs JSON |
| `gather_footprint` | Footprint lookup | Component info | Footprint dimensions JSON |
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
User describes circuit → LLM translates to JSON
                              │
                              ▼
                    Python validates schema
                              │
                              ▼
                    Python scans for missing specs
                    (LEDs without Vf, ICs without pin_count)
                              │
                              ▼
                    LLM looks up datasheet specs  ← should also resolve IC pinouts
                              │
                              ▼
                    Python merges specs into requirements
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

### Datasheet Enrichment

During gather only (not during validation), the LLM resolves missing component specs. This keeps the validator pure Python with no LLM dependencies.

**Current enrichment triggers:**
- LEDs missing `vf` → look up forward voltage
- ICs missing `pin_count` → look up pin count
- Capacitors missing `voltage_rating` → look up rating
- Transistors missing `vce_max`/`vds_max` → look up ratings

**Needed (not yet implemented):** IC pinout resolution — for complex ICs, the gather step should also return the full pin-to-function mapping (e.g., ATmega328P pin 1 = PC6/RESET). This proved critical in testing: without explicit pinouts, the LLM generates incorrect pin assignments.

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

- **Repair mode**: Resolves overlaps from invalid LLM placements. Cost function heavily penalizes violations (boundary + overlap), with wire length as secondary objective. Runs up to 10,000 iterations.
- **Optimize mode**: Improves valid placements. Minimizes wire length (MST-based ratsnest) and crossing count via simulated annealing. Iteration count auto-scales from component count (1000 × movable components, bounded [2000, 50000]). Early termination on stagnation.

Move types: translate (70%), swap same-package (15%), rotate (15%). Pinned components (user-placed, connectors, fiducials) excluded from moves.

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

- **DSN export** (`exporters/dsn_exporter.py`): Converts placement + netlist to Specctra DSN format. Board outline, component footprints with physical pad definitions (from `pad_geometry.py`), net connectivity, and design rules. GND excluded from routing (handled by copper fill).
- **SES import** (`exporters/ses_importer.py`): Parses Freerouting's session output. Extracts wire paths (traces) and vias, maps net names back to internal net_ids. Reuses S-expression parser from `kicad_importer.py`.
- **Orchestration** (`optimizers/freerouter.py`): Auto-downloads JAR to `~/.cache/pcb-creator/` on first use. Runs headlessly with `-mp 20` (max optimization passes). Falls back to built-in router on failure. Configurable timeout (default 300s).
- **Copper fills** (`router.py:apply_copper_fills()`): Standalone function that rebuilds a routing grid from the Freerouting output, marks existing traces/vias, then runs the standard fill algorithm. Removes the fill net (GND) from unrouted list and updates completion stats to 100%.

### Built-in Router (fallback)

Grid-based A* with 8-connected movement (orthogonal + 45° diagonal) on a 0.25mm grid. Used when `router_engine="builtin"` or when Freerouting fails.

**Net ordering:** Multi-trial optimization — tries multiple signal net orderings and keeps the best. Power/ground nets always route first.

**Multi-pass routing:**
1. **Pass 1** — 8-connected A* with normal clearance, best ordering from multi-trial search
2. **Rip-up-and-retry** — If a net fails, clear previously routed signal nets, route the failed net, re-route cleared nets
3. **Pass 2 (relaxed clearance)** — Remaining unrouted nets retry with 70% of normal clearance

**Trace sizing:** IPC-2221 auto-calculation from copper weight and estimated net current.

**Silkscreen generation:** Designator text labels, pin 1 dot indicators, anode "A" markers, board name and revision label. Silkscreen elements are filtered against exclusion zones (pads, fiducials, vias) to prevent overlaps. Board name/rev tries 5 candidate positions (corners + center) and picks the first non-colliding one.

**Validation:** 5 checks — schema, trace-to-trace clearance, via clearance, connectivity (union-find), no-shorts.

### Post-Routing Approval Gate

After routing completes, the pipeline serves an interactive board viewer in the browser and blocks until the user approves. The viewer is served by an ephemeral HTTP server (`orchestrator/approval_server.py`) that connects the browser UI to the CLI pipeline.

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
- **STEP AP214** (`step_exporter.py`): Bare PCB 3D model as an extruded polygon solid. Supports arbitrary board outlines. Generated directly in ISO 10303-21 text format (no CAD kernel dependency). Future: add 3D component models from EDA libraries or generated from datasheets.
- **Gerber ZIP** (`gerber_exporter.py`): All Gerbers + drill packaged for manufacturer upload.

**Dependencies:** `gerber-writer>=0.4` (Gerber generation). All other formats use Python stdlib.

### Planned Features

- **Gradio GUI**: ✅ Implemented. `pcb-creator gui` launches Gradio web UI with chat input, file upload (drag & drop), step progress panel, embedded board viewer, AI model settings (provider presets), and Export/Import KiCad buttons. Uses `run_workflow_with_gradio()` generator for progressive UI updates.
- **Agent-mode flag**: ✅ Implemented. `--agent-mode` flag on `pcb-creator run` skips browser approval gate. Vision-based autonomous approval (render to PNG, LLM review) is a future step.
- **Silkscreen overlap fix**: Board name/revision label collides with component designators on dense boards. Need to: (1) truncate long project names for silkscreen, (2) check board name candidate positions against designator bounding boxes, not just pads/vias. Affects `optimizers/router.py` silkscreen generation.
- **MCP server for agent integration**: Expose the pipeline as an MCP (Model Context Protocol) server so any AI agent can design PCBs. Tools: `design_pcb(description, settings)` → streams step events, `get_project_status(project)`, `export_kicad(project)`, `get_drc_report(project)`, `list_projects()`. The existing `run_workflow_with_gradio()` generator yields structured JSON events that map directly to streaming tool results. `--agent-mode` already skips the blocking approval gate. Preferred over a Claude Code skill because MCP is agent-agnostic — works with Claude Code, Agent SDK, and any MCP client.
- **GUI conversational refinement**: After the LLM translates a circuit description, show a summary to the user and allow feedback/corrections before starting the pipeline. Uses the existing `RequirementsGatherer._summarize()` flow but via a chat-style interface instead of terminal `input()`. Enables back-and-forth clarification of ambiguous requirements (e.g., "which ATtiny variant?", "what LED forward voltage?") before committing to the full pipeline run.
- **Manufacturer quoting (Step 7)**: Auto-submit BOM, Gerbers, assembly files to manufacturer APIs (JLCPCB, PCBWay, OSH Park) for fabrication + assembly quotes. Present comparison to user. Steps 5-6 produce submission-ready files in manufacturer-expected formats.
- **3D populated board model**: Add component 3D models to the STEP file from EDA libraries (KiCad 3D library, SnapEDA, Ultra Librarian) or generated parametrically from datasheet dimensions. STEP assembly places models at placement coordinates with rotation.
- **Pre-route GND as fill before signal routing**: Build GND fill first so the router can use fill connectivity for GND pads, freeing grid space for signal routing.

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

`visualizers/placement_viewer.py` generates interactive HTML/SVG board views with:
- Color-coded components by type with pad shapes (rectangles for each pin, derived from pad_geometry)
- Ratsnest lines (MST per net, color-coded by net class)
- Routed traces (width-proportional, colored by net, layer-aware opacity, supports diagonal segments)
- Vias (concentric circles: annular ring + drill hole)
- Silkscreen elements (designator labels, pin 1 dots, anode "A" markers)
- Routing progress bar in header (green/yellow/red by completion %)
- Hover tooltips with value, specs, and description (from BOM)
- Pan/zoom, side panel with routing statistics and component table
- Fiducial markers

See `VISUALIZATION.md` for the data format reference (useful for building custom visualizations).

## LLM Provider Support

### Cloud (OpenRouter)
Default: `openrouter/qwen/qwen3.5-27b`. Also tested with `openrouter/x-ai/grok-4.1-fast`.

### Local (oMLX / Ollama)
Use `--model openai/<model-name> --api-base http://localhost:8000/v1` for local models.
API key loaded from `.env` file via `python-dotenv`.

Environment variables: `PCB_GENERATE_MODEL`, `PCB_REVIEW_MODEL`, `PCB_GATHER_MODEL`, `PCB_API_BASE`, `PCB_API_KEY`.

## Key Learnings

1. **IC pinouts must be in requirements.** LLMs don't reliably know pin mappings for specific IC packages. The datasheet lookup step should resolve these during gather, before the user approves requirements.

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
