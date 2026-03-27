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

- **Natural language input** — describe your circuit, get a PCB
- **Freerouting autorouter** — production-quality push-and-shove routing (auto-downloads, requires Java 17+)
- **Built-in A\* router** — fallback if Freerouting unavailable
- **IPC-2221 trace sizing** — automatic trace width calculation for current capacity
- **DRC with DFM profiles** — checks against JLCPCB, PCBWay, OSH Park manufacturing rules
- **Manufacturer-ready output** — Gerber RS-274X, Excellon drill, BOM CSV, pick-and-place CSV, STEP 3D model
- **Interactive board viewer** — HTML/SVG visualization with traces, copper fills, component hover tooltips, DRC results
- **Approval gate** — review the routed board in your browser before generating output files
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

Run the pipeline:

```bash
# Interactive mode
pcb-creator design --project my_board

# From a requirements file
pcb-creator run --requirements tests/test_switch_led.json --project led_test
```

The pipeline will:
1. Generate schematic, BOM, and layout using the LLM
2. Route the board with Freerouting
3. Run DRC against your manufacturer's DFM profile
4. Open an interactive viewer in your browser for approval
5. Generate Gerber files, drill file, BOM CSV, pick-and-place, and STEP model

## Requirements

- **Python 3.11+**
- **Java 17+** (for Freerouting autorouter — falls back to built-in router if unavailable)
- **LLM API access** — any OpenAI-compatible API (OpenRouter, Ollama, oMLX, OpenAI, etc.)

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
| `PCB_LLM_API_BASE` | *(none)* | API base URL (for local models) |
| `PCB_LLM_API_KEY` | *(none)* | API key |
| `PCB_ROUTER_ENGINE` | `freerouting` | `freerouting` or `builtin` |
| `PCB_FREEROUTING_TIMEOUT` | `300` | Freerouting timeout (seconds) |
| `PCB_MAX_REWORK` | `5` | Max LLM rework attempts per step |

## Output Files

Generated in `projects/<name>/output/`:

| File | Format | Purpose |
|------|--------|---------|
| `*-F_Cu.gbr`, `*-B_Cu.gbr` | Gerber | Copper layers |
| `*-F_SilkS.gbr` | Gerber | Silkscreen (stroke font) |
| `*-F_Mask.gbr`, `*-B_Mask.gbr` | Gerber | Solder mask |
| `*-Edge_Cuts.gbr` | Gerber | Board outline |
| `*.drl` | Excellon | Drill file |
| `*_bom.csv` | CSV | Bill of materials (JLCPCB format) |
| `*_cpl.csv` | CSV | Pick-and-place (JLCPCB CPL format) |
| `*_board.step` | STEP | Bare PCB 3D model |
| `*_gerbers.zip` | ZIP | All Gerbers + drill for upload |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed documentation of the pipeline, data formats, algorithms, and design decisions.

## License

MIT
