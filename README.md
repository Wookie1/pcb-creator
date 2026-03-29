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
- **Natural language input** — describe your circuit, get a PCB
- **Design planning** — the LLM proposes a design with assumptions and asks clarifying questions before generating
- **Tiered component lookup** — resolves footprints and specs from KiCad library, IPC-7351B, EasyEDA/LCSC, and curated tables before falling back to LLM
- **Parallel LLM enrichment** — remaining spec/footprint lookups run concurrently instead of sequentially
- **Freerouting autorouter** — production-quality push-and-shove routing (auto-downloads, requires Java 17+)
- **Built-in A\* router** — fallback if Freerouting unavailable
- **IPC-2221 trace sizing** — automatic trace width calculation for current capacity
- **DRC with DFM profiles** — checks against JLCPCB, PCBWay, OSH Park manufacturing rules
- **Manufacturer-ready output** — Gerber RS-274X, Excellon drill, BOM CSV, pick-and-place CSV, STEP 3D model
- **Interactive board viewer** — HTML/SVG visualization with traces, copper fills, component hover tooltips, DRC results
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
```

The pipeline will:
1. Plan the design and ask clarifying questions (power source, IC choices, LED colors, etc.)
2. Generate schematic, BOM, and layout using the LLM
3. Route the board with Freerouting
4. Run DRC against your manufacturer's DFM profile
5. Open an interactive viewer for approval
6. Generate Gerber files, drill file, BOM CSV, pick-and-place, and STEP model

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
| `PCB_KICAD_LIBRARY_PATH` | *(none)* | KiCad footprint library root (for tiered lookup) |
| `PCB_COMPONENT_CACHE_PATH` | `~/.pcb-creator/component_cache.json` | Resolved component cache |
| `PCB_LLM_ENRICHMENT_WORKERS` | `4` | Parallel LLM calls for spec/footprint enrichment |

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

## MCP Server

PCB-Creator includes an MCP server so AI agents (Claude Desktop, Claude Code, etc.) can design PCBs programmatically.

**Tools exposed:** `design_pcb`, `list_projects`, `get_project_status`, `get_drc_report`, `export_kicad`, `get_board_image`

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

The MCP server runs in agent mode with vision-based board approval. Projects are stored in `~/.pcb-creator/projects/` (configurable via `PCB_PROJECTS_DIR`).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed documentation of the pipeline, data formats, algorithms, and design decisions.

## License

MIT
