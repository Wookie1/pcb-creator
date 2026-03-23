# PCB-Creator Agents

## Overview

Each agent has a dedicated skill file under `skills/` containing its full prompt template and instructions. This file serves as a brief index.

## Agent Roster

### Applications Engineer (AE)
- **Role**: Project manager and orchestrator
- **Skill file**: `skills/ae_orchestrate.md`
- **Responsibilities**: Gather requirements, validate completeness, construct specialist prompts, manage workflow status, handle QA pass/fail, communicate with user
- **Runs**: Throughout the entire workflow

### Schematic Engineer
- **Role**: Circuit design specialist
- **Skill file**: `skills/schematic_engineer.md`
- **Responsibilities**: Convert requirements into a valid circuit netlist JSON
- **Runs**: Step 1 (Schematic/Netlist)

### Component Engineer
- **Role**: Component selection and BOM specialist
- **Skill file**: `orchestrator/prompts/templates/bom_generate.md.j2`
- **Responsibilities**: Generate Bill of Materials with detailed procurement specs from netlist
- **Runs**: Step 2 (Component Selection)

### Layout Engineer
- **Role**: Board layout and component placement specialist
- **Skill file**: `orchestrator/prompts/templates/layout_generate.md.j2`
- **Responsibilities**: Place components on PCB board respecting placement rules, user hints, and board boundaries
- **Runs**: Step 3 (Board Layout)

### Routing Engineer
- **Role**: PCB trace routing specialist (algorithmic, no LLM)
- **Skill file**: N/A (purely algorithmic — `optimizers/router.py`)
- **Responsibilities**: Route copper traces between component pads using grid-based A*, auto-calculate trace widths via IPC-2221, manage via placement and layer transitions
- **Runs**: Step 4 (Routing)

### QA Engineer
- **Role**: Quality assurance reviewer
- **Skill file**: `skills/qa_review.md`
- **Responsibilities**: Validate step outputs against requirements and standards, run validators, report issues
- **Runs**: After every step output

## Adding New Agents

1. Create a new skill file under `skills/` (e.g., `skills/layout_engineer.md`)
2. Add an entry to this roster
3. Add the corresponding step to `FLOW.md`
