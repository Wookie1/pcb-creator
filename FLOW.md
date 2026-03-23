# PCB Design Workflow

## Process Steps

| Step | Name                | Specialist          | Input                      | Output                          | Status    |
|------|---------------------|---------------------|----------------------------|---------------------------------|-----------|
| 0    | Requirements        | AE                  | User prompt + attachments  | REQUIREMENTS.md                 | Implemented |
| 1    | Schematic/Netlist   | Schematic Engineer  | REQUIREMENTS.md            | {project}_netlist.json          | Implemented |
| 2    | Component Selection | Component Engineer  | netlist.json               | {project}_bom.json              | Implemented |
| 3    | Board Layout        | Layout Engineer     | netlist.json, bom.json     | {project}_placement.json        | Implemented |
| 4    | Routing             | Routing Engineer    | placement.json, netlist.json | {project}_routed.json         | Implemented |
| 4b   | Copper Fills        | Routing Engineer    | routed.json (integrated)   | {project}_routed.json (updated) | Implemented |
| 5    | DRC                 | DRC Engineer        | routed.json + DFM profile  | {project}_drc_report.json       | Implemented |
| 6    | Output Generation   | Output Engineer     | routed.json + bom.json     | Gerbers, drill, BOM CSV, CPL, STEP | Implemented |

New steps can be inserted by adding rows to this table and creating a corresponding skill file under `skills/`.

## Status Identifiers

| Status ID          | Meaning                                    |
|--------------------|--------------------------------------------|
| NOT_STARTED        | Step has not begun                         |
| IN_PROGRESS        | Specialist is working                      |
| AWAITING_QA        | Specialist done, QA has not started        |
| QA_IN_PROGRESS     | QA is reviewing                            |
| QA_PASSED          | QA approved the output                     |
| QA_FAILED          | QA found issues, needs rework              |
| REWORK_IN_PROGRESS | Specialist is fixing QA issues             |
| BLOCKED            | Cannot proceed, AE intervention needed     |
| AWAITING_APPROVAL  | Step done, waiting for user approval       |
| COMPLETE           | Step fully done and approved               |

## Failure Limits

- `max_rework_attempts`: 5 (default, configurable via `PCB_MAX_REWORK` env var)
- If a step fails QA this many times, the workflow stops and the AE reports to the user with details of the failure.
- The user may provide guidance and restart, which resets the failure counter for that step.

## Workflow Rules

1. Steps execute sequentially — Step N must reach COMPLETE before Step N+1 begins.
2. Every step output goes through QA review before the step is marked COMPLETE.
3. The AE is the only agent that updates STATUS.json.
4. The AE constructs specialist prompts by injecting relevant excerpts from STANDARDS.md and REQUIREMENTS.md — specialists do not read these files directly.
5. Step 3 has automatic repair: if the LLM generates overlapping placements, a simulated annealing algorithm resolves overlaps before counting it as a failed attempt.
6. After Step 3 succeeds, the SA optimizer runs to minimize wire length/crossings (with decoupling cap proximity, crystal proximity, and functional grouping cost terms), then fiducial markers are added to each populated board side.
7. Step 4 (Routing) supports two engines: **Freerouting** (default, external autorouter via Specctra DSN/SES) and **built-in** (8-connected A* with rip-up-and-retry). Freerouting auto-downloads on first use and requires Java 17+. Falls back to built-in on failure. GND net is excluded from trace routing and connected via copper fill (both layers) with thermal relief and island removal. IPC-2221 trace widths are computed before routing and passed to Freerouting via DSN net classes (preventing current-capacity DRC violations). Configure via `PCB_ROUTER_ENGINE` env var (`freerouting` or `builtin`).
8. Step 5 (DRC) runs automatically after routing. Checks electrical rules (clearance, connectivity, shorts), DFM rules (trace width, via drill, annular ring, silkscreen vs manufacturer profile), mechanical rules (hole spacing, copper-to-edge), and current capacity (IPC-2221). DFM profile resolved from `requirements.manufacturing.manufacturer`.
9. After DRC, a **mandatory approval gate** opens an interactive board viewer in the browser. The user reviews traces, copper fills, per-net stats, and DRC results. Three actions: **Export KiCad** (download `.kicad_pcb` for manual editing), **Import KiCad** (upload edited `.kicad_pcb` to re-import), **Continue to Output** (approve and proceed to Step 6). Pipeline blocks until Continue.
10. Step 6 (Output Generation) produces manufacturer-ready files: 8 Gerber layers (copper, silkscreen with stroke font, solder mask, paste, edge cuts), Excellon drill, BOM CSV, pick-and-place CSV (with fiducials), bare PCB STEP model, and a ZIP package for upload. Silkscreen text avoids pad/fiducial exclusion zones.
