# PCB-Creator

AI-driven PCB design tool that takes natural language circuit descriptions and produces manufacturer-ready files (Gerber, drill, BOM, pick-and-place, STEP). No EDA software required.

## What it does

```
"I need a green LED controlled by a pushbutton, powered by 3.3V"
    ↓
Requirements → Schematic → BOM → Layout → Routing → DRC → Output Files
    ↓
Upload to JLCPCB and order your board
```

**Steps 0-2** use an LLM to translate requirements, generate the schematic netlist, and select components. **Steps 3-6** are purely algorithmic — placement optimization, autorouting, design rule checking, and file generation.

## Features

- **Web GUI** — Gradio chat interface with live board preview, step progress, and approval flow
- **Natural language input** — describe your circuit, get a PCB (tested end-to-end with Arduino Uno-class complexity on local 27B model)
- **Multi-turn design planning** — the LLM proposes a design, asks clarifying questions, and looks up real component specs; iterate until the plan is right, then proceed to generation
- **Tiered component lookup** — resolves footprints and specs from KiCad library, IPC-7351B, EasyEDA/LCSC, and curated tables before falling back to LLM
- **Parallel LLM enrichment** — remaining spec/footprint lookups run concurrently instead of sequentially
- **4-layer PCB support** — GND and power planes on inner layers (In1.Cu/In2.Cu), antipad cutouts, power stitching vias. `board.plane_layers` (or `optimize_placement(plane_layers=...)`) chooses the stackup: 2 = both inner planes (default, route on outer layers); 1 = GND plane + In2 used as a 3rd **signal** layer for dense/many-signal boards; 0 = all-signal inner
- **Freerouting autorouter (primary engine)** — production-quality push-and-shove routing via headless mode (auto-downloads, requires Java 17+); `effort` levels (fast/normal/best), live pass-by-pass progress, and an automatic re-place-and-reroute retry when a route comes back incomplete
- **Built-in A\* router** — 2-layer fallback used only when `PCB_ROUTER_ENGINE=builtin`
- **Two-sided placement** — small SMD passives can move to the bottom side (`optimize_placement(two_sided=True)`); especially effective on 4-layer boards where the inner planes free both outer layers for signal
- **Agent-driven MCP interface** — drive the pipeline from any MCP client with an incremental circuit builder, structured `next_step`/`remediation` responses, and a workflow guide (see [MCP Server](#mcp-server))
- **Small-model friendly** — chunked netlist generation, reasoning-tag stripping, and a `PCB_MODEL_PROFILE=small` mode make the LLM steps work with local 9B–35B models
- **Fine-pitch escape routing** — boards with a tight-pitch part (≤0.8mm, e.g. a 0.5mm-pitch FFC or QFN) automatically route at the manufacturer-minimum trace/clearance AND get a full dog-bone **escape breakout** (pad → stub → staggered via → protected fanout to a clean grid, GND pins dropped to the plane) handed to Freerouting as protected wiring, so the autorouter starts clear of the pad field instead of shorting across it. Auto-enabled when a fine-pitch part is present; ordinary boards keep the robust defaults
- **Short-cleanup pass** — after routing, the nets KiCad's DRC reports as shorting (or left incomplete) are ripped up and re-routed with everything else (including all escape wiring) held as protected wiring; reliably clears the through-hole-pad shorts Freerouting leaves in congestion. Driven by kicad-cli's authoritative DRC; no-op without it
- **IPC-2221 trace sizing** — automatic trace width calculation for current capacity, propagated through series inductors/fuses
- **DRC with DFM profiles** — checks against JLCPCB standard, JLCPCB 4-layer, PCBWay, OSH Park manufacturing rules. When kicad-cli is installed, DRC runs KiCad's own engine (the same one the fab uses, with poured zones and the board's actual routed rules) for an authoritative result; a portable internal validator is the fallback
- **DXF board outline** — attach a DXF file to define non-rectangular board shapes
- **Assembly drawing PDF** — print-friendly component placement reference with BOM table for manufacturing
- **Manufacturer-ready output** — Gerber RS-274X, Excellon drill, BOM CSV, pick-and-place CSV, assembly PDF, STEP 3D model
- **Interactive board viewer** — HTML/SVG visualization with traces, copper fills, component hover tooltips, DRC results
- **Incremental routing** — `route_board(keep_existing=True)` protects the current routing as fixed wiring and routes only the UNROUTED nets, so the tool *finishes* a partly-routed (or KiCad-imported) board instead of redoing it
- **KiCad export/import** — export to KiCad for manual editing, re-import to continue the pipeline

## Quick Start

```bash
git clone <repo-url> && cd pcb-creator
./install.sh
source .venv/bin/activate
```

Configure your LLM provider (edit `.env`):

```bash
# OpenRouter (cloud)
PCB_LLM_API_KEY=sk-or-your-key-here

# Or Ollama (local, free)
PCB_LLM_API_BASE=http://localhost:11434/v1
PCB_GENERATE_MODEL=ollama/qwen3.5:27b
```

Run it:

```bash
# Web GUI (recommended) — chat interface with live board preview
pcb-creator gui

# CLI interactive — describe your circuit in the terminal
pcb-creator design --project my_board

# CLI from a requirements file — skip the conversation
pcb-creator run --requirements tests/test_switch_led.json --project led_test

# Batch/CI mode — skip approval gate entirely
pcb-creator run --requirements tests/test_switch_led.json --project led_test --skip-approval

# Fast mode — skip LLM QA reviews (validators still run)
pcb-creator run --requirements tests/test_switch_led.json --project led_test --skip-qa
```

The pipeline will:
1. Plan the design and ask clarifying questions (power source, IC choices, LED colors, etc.)
2. Generate schematic, BOM, and layout using the LLM
3. Route the board with Freerouting
4. Run DRC against your manufacturer's DFM profile
5. Open an interactive viewer for approval
6. Generate Gerber files, drill file, BOM CSV, pick-and-place, assembly PDF, and STEP model

## Requirements

- **Python 3.11+**
- **Java 17+** — required for the Freerouting autorouter (the default engine). Without it, set `PCB_ROUTER_ENGINE=builtin` to use the built-in A\* router (2-layer only; 4-layer boards require Freerouting)
- **LLM API access** — any OpenAI-compatible API (OpenRouter, Ollama, oMLX, OpenAI, etc.)
- Works with models as small as 9B parameters (tested with Qwen 3.5 9B); 27B+ recommended for complex boards

## Project Structure

```
orchestrator/    — Pipeline engine (CLI, steps, LLM client, prompts)
optimizers/      — Placement optimizer, routing engines, pad geometry
exporters/       — Gerber, Excellon, KiCad, DSN/SES, BOM CSV, STEP
validators/      — DRC checks, DFM profiles, routing/placement validation
visualizers/     — Interactive HTML/SVG board viewer
schemas/         — JSON Schema definitions
tests/           — Test fixtures and unit tests
```

## Configuration

All settings via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `PCB_GENERATE_MODEL` | `openrouter/qwen/qwen3.5-27b` | LLM model for generation |
| `PCB_MODEL_PROFILE` | `normal` | `small` lowers the chunked-generation threshold and batch sizes for weaker local models (≤14B dense / low-active-param MoE) |
| `PCB_LLM_API_BASE` | *(none)* | API base URL (for local models) |
| `PCB_LLM_API_KEY` | *(none)* | API key |
| `PCB_ROUTER_ENGINE` | `freerouting` | `freerouting` or `builtin` |
| `PCB_FREEROUTING_TIMEOUT` | `300` | Freerouting timeout (seconds) |
| `PCB_FREEROUTING_HEAP_MB` | *(auto: ~55% RAM, 1024–6144)* | JVM max-heap cap for Freerouting; prevents OOM-killing the host on dense boards |
| `PCB_ESCAPE_FANOUT` | *(auto)* | Tri-state: unset = auto-enable when the board has a fine-pitch part; `true`/`false` force on/off. Pre-generates dog-bone escape breakouts for single-row fine-pitch parts as protected wiring before routing |
| `PCB_SHORT_CLEANUP` | `true` | After routing, rip+re-route the nets kicad-cli DRC reports as shorting/incomplete (escapes preserved). No-op without kicad-cli |
| `PCB_OPTIMIZER_ITERATIONS` | *(auto: 200×movable, ≤8000)* | Override the SA placement iteration cap |
| `PCB_CUSTOM_FOOTPRINT_DIR` | *(none)* | Global writable dir for agent-registered custom footprints (tier 0) |
| `PCB_MAX_REWORK` | `5` | Max LLM rework attempts per step |
| `PCB_SKIP_QA` | `false` | Skip per-step LLM QA reviews (validators still run) |
| `PCB_LLM_TIMEOUT` | `1800` | LLM request timeout in seconds |
| `PCB_KICAD_LIBRARY_PATH` | *(none)* | KiCad footprint library root (for tiered lookup) |
| `PCB_COMPONENT_CACHE_PATH` | `~/.pcb-creator/component_cache.json` | Resolved component cache |
| `PCB_LLM_ENRICHMENT_WORKERS` | `4` | Parallel LLM calls for spec/footprint enrichment |

## Output Files

Generated in `projects/<name>/output/`:

| File | Format | Purpose |
|------|--------|---------|
| `*-F_Cu.gbr`, `*-B_Cu.gbr` | Gerber | Outer copper layers |
| `*-In1_Cu.gbr`, `*-In2_Cu.gbr` | Gerber | Inner copper planes (4-layer boards only: GND and power) |
| `*-F_SilkS.gbr` | Gerber | Silkscreen (stroke font) |
| `*-F_Mask.gbr`, `*-B_Mask.gbr` | Gerber | Solder mask |
| `*-Edge_Cuts.gbr` | Gerber | Board outline |
| `*.drl` | Excellon | Drill file |
| `*_bom.csv` | CSV | Bill of materials (JLCPCB format) |
| `*_cpl.csv` | CSV | Pick-and-place (JLCPCB CPL format) |
| `*_board.step` | STEP | Populated PCB 3D model |
| `*_assembly.pdf` | PDF | Assembly drawing (component placement + BOM) |
| `*_gerbers.zip` | ZIP | All Gerbers + drill for upload |

## Agent / Programmatic Usage

PCB-Creator can be driven by AI agents or scripts in two ways: **CLI** (recommended for shell-based agents) or **MCP server** (for MCP-compatible clients).

### CLI (recommended for agents)

Agents write a requirements JSON file, then run the pipeline:

```bash
# 1. Get the schema to understand the expected format
pcb-creator schema > schema.json

# 2. Write requirements to a file (agent constructs this JSON)
# 3. Run the pipeline with structured JSON output
pcb-creator run --requirements requirements.json --agent-mode --skip-qa --json-output

# With a DXF board outline (or other attachments like datasheets, sketches):
pcb-creator run --requirements requirements.json --attach board_outline.dxf --agent-mode --skip-qa --json-output
```

`--json-output` prints a structured result to stdout with success status, routing stats, DRC summary, and output file paths. `--project` is optional (auto-generated from the requirements JSON). `--attach` can be repeated for multiple files.

### MCP Server

For MCP-compatible clients (Claude Desktop, Claude Code, Cline, etc.).
Designed to work well with small local models: every tool response carries a
machine-readable `next_step` (the exact call to make next) and, on failure,
`remediation` options; long operations report live progress via
`get_project_status` (`status_hint`, `poll_again_in_s`). Call
`get_workflow_guide()` first for the step-by-step tool order.

**Three workflows:**

1. **Build from scratch (recommended for agents):** `create_circuit` →
   `add_component` (returns the pin table; validates the footprint
   immediately) → `connect_pins` (by pin number or name, e.g. `"D1.anode"`,
   `"U1.VCC"`) → `finalize_circuit` (full validation) → `optimize_placement`
   → `route_board` → `run_drc` → `export_outputs`. Many small validated
   calls — no giant netlist JSON. Rework tools: `list_circuit`,
   `remove_component`, `disconnect_pins`, `mark_no_connect`. Components that
   must sit at exact coordinates (edge connectors, mounting holes) are fixed
   with `place_component` — validated immediately against board bounds and
   other pinned parts, and never moved by the optimizer.
2. **Import KiCad:** `import_kicad_netlist` → `verify_footprints` /
   `provide_footprint` → placement → routing → DRC → export.
3. **Autonomous:** `design_pcb` runs the full LLM pipeline in the background
   (poll `get_project_status`). Prefer `requirements_json` (schema from
   `get_requirements_schema()`) over plain text — it skips LLM translation.

Boards whose components don't fit on top can use
`optimize_placement(two_sided=True)` — the optimizer may move small SMD
passives (R/C/diodes) to the bottom side. Connectors, ICs, LEDs, and
through-hole parts always stay on top, and all outputs (Gerbers incl.
B_Paste, CPL, KiCad) carry the side. Note: on 2-layer boards the bottom is
the router's escape layer, so use two-sided to make parts FIT; prefer a
larger board when routing completion is the problem. On **4-layer** boards
two-sided is much more favorable: the inner layers carry GND/power planes, so
both outer layers are free for signal and bottom-side parts route well
(power/ground pins reach the inner planes through via-in-pad stitching). The
optimizer flips freely on 4-layer and reluctantly on 2-layer.

`route_board(effort="fast"|"normal"|"best")` trades quality vs wait time
(~2/5/15 min caps); routing progress streams pass-by-pass from Freerouting.
A fine-pitch part auto-triggers escape-breakout pre-routing, and after the
main route a short-cleanup phase rips+re-routes any shorting/incomplete nets
(a brief no-progress gap while it exports + DRCs is normal — wait for
`routing_state` to reach `complete`). By default an incomplete route also
triggers one automatic re-place (extra clearance + congestion penalty) and
re-route, keeping the better result. `run_drc` returns a severity-ranked
summary with a remediation hint per failing rule; when kicad-cli is present
it is KiCad's authoritative DRC (`get_drc_report(verbose=True)` for the full
report).

Add to your MCP client config (e.g., `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pcb-creator": {
      "command": "pcb-creator-mcp",
      "env": {
        "PCB_LLM_API_KEY": "your-api-key"
      }
    }
  }
}
```

Or run directly:

```bash
python mcp_server.py
```

The MCP server runs in agent mode with QA reviews and vision review skipped by default (the calling agent reviews results via `get_project_status` and `get_board_image`). LLM timeout is 5 min per call, max 3 rework attempts. Projects are stored in `~/.pcb-creator/projects/` (configurable via `PCB_PROJECTS_DIR`).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed documentation of the pipeline, data formats, algorithms, and design decisions.

## License

MIT
