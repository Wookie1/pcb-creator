# PCB-Creator

AI-driven PCB design: natural-language requirements → netlist → BOM → placement → routing → Gerber/drill/BOM/CPL/STEP. Steps 0–2 use an LLM; steps 3–6 are deterministic algorithms. See `README.md` (features/env vars), `ARCHITECTURE.md` (deep design), `FLOW.md` (pipeline steps/status machine).

## Setup & run

- `./install.sh` creates `.venv` and installs (`pip install -e ".[dxf]"`). All Python commands use `.venv/bin/python`.
- Entry points: `orchestrator.cli:main` (CLI, also `./pcb-creator` root launcher script) and `mcp_server:main` (MCP stdio server).
- Full pipeline from a requirements file: `pcb-creator run --requirements <file.json> --project <name> --skip-approval` (add `--agent-mode --json-output` for non-interactive structured output). A mandatory browser approval gate blocks after DRC unless `--skip-approval`/`--agent-mode`.
- LLM config comes from `.env` / `PCB_*` env vars (see README table). `.env` holds real API keys — never commit or echo them (it is gitignored).
- Freerouting autorouter needs Java 17+ (auto-downloads the jar). `kicad-cli` is optional: DRC uses it when present, internal validator otherwise.

## Tests

- Unit tests live in `tests/` only. Run from repo root: `.venv/bin/python -m pytest tests/ -q`; single file: `pytest tests/test_ipc7351.py -q`. They need no network/LLM/Java; Java-dependent tests self-skip.
- `test/` is NOT tests — dev notes and the E2E eval-board suite (`test/run_all.sh`, requires a live LLM). `test/*` is gitignored except `test/requirements/` (gitignore uses `test/*` deliberately — git can't re-include under a wholesale-ignored `test/` dir; don't "fix" it).
- `tests/conftest.py` isolates `PCB_COMPONENT_CACHE_PATH` per test so exports don't mutate `~/.pcb-creator/component_cache.json` — don't remove it.
- `.coveragerc` targets 100% of the deterministic logic core; IO/glue modules (LLM client, GUI, CLI, HTTP server, network lookups) are deliberately omitted. Don't chase coverage there.
- `fastmcp>=3` is pinned because tests call `@mcp.tool()` functions directly; 2.x FunctionTool objects are not callable.

## Architecture boundaries

- `orchestrator/` — pipeline engine (CLI, steps in `steps/`, LLM client, prompt templates in `prompts/templates/*.j2`, quoting). `optimizers/` — placement/routing engines, pad geometry. `exporters/` — Gerber/KiCad/STEP/BOM output. `validators/` — DRC + footprint verification. `visualizers/` — HTML/SVG viewer. `mcp_server.py` — monolithic MCP layer over the same engines; `mcp_envelope.py` wraps its tool responses (`next_step`/`remediation` envelope — keep it).
- Pipeline flow: `FLOW.md` steps 0–6; per-step code in `orchestrator/steps/`, status machine in `FLOW.md`. Long-running pipeline steps have QA-review + max-5-rework loops (`PCB_MAX_REWORK`).
- MCP server runs design/routing in background threads; clients poll `get_project_status`. Project state lives in `projects/` (CLI) or `~/.pcb-creator/projects/` (MCP, override via `PCB_PROJECTS_DIR`).
- `[tool.setuptools] packages` lists only `exporters, optimizers, orchestrator` + module `mcp_server`; new packages must be added there to be importable after install.

## LLM-agent roster (workflow personas)

Prompt-template agents used by the design workflow; full prompts in the referenced files (add new agents here + `FLOW.md`):

- **AE** (orchestrator): `skills/ae_orchestrate.md` — only agent allowed to edit `STATUS.json`; builds specialist prompts from `STANDARDS.md`/`REQUIREMENTS.md` excerpts (specialists don't read those directly).
- **Schematic Engineer** (step 1): `skills/schematic_engineer.md` → netlist JSON.
- **Component Engineer** (step 2): `orchestrator/prompts/templates/bom_generate.md.j2` → BOM.
- **Layout Engineer** (step 3): `orchestrator/prompts/templates/layout_generate.md.j2`; overlap failures auto-repaired by SA optimizer before counting as rework.
- **Routing** (step 4): no LLM — `optimizers/freerouter.py` (Freerouting/Specctra DSN) + `optimizers/router.py` fallback; GND connected by copper fill, not traces.
- **QA** (after every step): `skills/qa_review.md`.
