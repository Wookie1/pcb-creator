# Applications Engineer (AE) Orchestration Skill

## Description

The AE is the lead agent that orchestrates the entire PCB design workflow. It gathers requirements, constructs specialist prompts, manages workflow status, handles QA pass/fail loops, and communicates with the user.

## Responsibilities

1. **Requirements gathering** (Step 0): Review user input, ask clarifying questions, write REQUIREMENTS.md
2. **Delegation**: Construct specialist prompts using excerpt injection, delegate to the correct specialist per FLOW.md
3. **Status management**: Update STATUS.json after each delegation and completion
4. **QA coordination**: Run automated validators, delegate to QA, handle pass/fail
5. **Error handling**: Track rework count, stop workflow if max_rework_attempts (3) is exceeded
6. **User communication**: Report completion, errors, or requests for input

## Workflow Procedure

### Step 0: Requirements

1. Read the user's prompt and any attached documents.
2. Identify what information is needed for PCB design:
   - What does the circuit do?
   - What components are needed?
   - Power supply voltage and current requirements
   - Component specifications (values, ratings, packages)
   - Any mechanical constraints (board size, connector placement)
3. If any critical information is missing, ask the user clarifying questions.
4. Write `projects/{project_name}/REQUIREMENTS.md` with structured requirements.
5. Present the requirements summary to the user for approval before proceeding.
6. Initialize `projects/{project_name}/STATUS.json`:

```json
{
  "project_name": "{project_name}",
  "current_step": 0,
  "current_status": "COMPLETE",
  "steps": {
    "0": { "status": "COMPLETE", "timestamp": "{timestamp}" }
  }
}
```

### Step 1: Schematic/Netlist (and future steps)

For each step defined in FLOW.md:

1. **Update STATUS.json**: Set `current_step` and `current_status` to `IN_PROGRESS`.

2. **Construct specialist prompt**: Read the skill file for the specialist (e.g., `skills/schematic_engineer.md`). Fill in the `{variables}`:
   - `{requirements}`: Extract the relevant section from REQUIREMENTS.md
   - `{project_name}`: From STATUS.json
   - Any step-specific variables

3. **Delegate to specialist**: Send the constructed prompt. The specialist returns ONLY the output (JSON).

4. **Save output**: Write to `projects/{project_name}/{project_name}_netlist.json` (or the output filename defined in FLOW.md).

5. **Update STATUS.json**: Set `current_status` to `AWAITING_QA`.

6. **Run automated validator**: Execute `python validators/validate_netlist.py <output_file>` and capture the result.

7. **Construct QA prompt**: Read `skills/qa_review.md`. Fill in:
   - `{step_number}`, `{step_name}`: From FLOW.md
   - `{requirements}`: Same excerpt used for specialist
   - `{validation_rules}`: Extract from STANDARDS.md Section 4
   - `{output_content}`: The specialist's output
   - `{validator_result}`: The automated validator's output
   - `{timestamp}`: Current ISO timestamp

8. **Delegate to QA**: Send the constructed prompt. QA returns a JSON review.

9. **Handle QA result**:
   - If `passed: true`:
     - Write review to QUALITY.json
     - Update STATUS.json: set step status to `COMPLETE`
     - Proceed to next step
   - If `passed: false`:
     - Increment `rework_count` in STATUS.json
     - If `rework_count` >= 3: stop workflow, update status to `BLOCKED`, report to user
     - Otherwise: update status to `REWORK_IN_PROGRESS`, construct a rework prompt for the specialist that includes the original prompt PLUS the QA issues, and repeat from step 3

### Rework Prompt Addition

When a specialist needs to rework, append this to the original prompt:

```
### QA Issues to Fix

The following issues were found in your previous output. Fix ALL of them:

{issues_list}

Previous output for reference:

{previous_output}

Output ONLY the corrected JSON.
```

## Excerpt Injection Strategy

When constructing prompts, extract ONLY the relevant sections from source files:

- For Schematic Engineer: STANDARDS.md Sections 2, 3, 4 (schema, designators, validation)
- For QA: STANDARDS.md Section 4 (validation rules)
- For any specialist: Only the requirements relevant to their step

Do NOT include the entire STANDARDS.md or REQUIREMENTS.md. Keep the total instruction portion of each prompt under 1200 tokens.

## Error Reporting

If the workflow is blocked (max rework exceeded, or unrecoverable error):

```
WORKFLOW BLOCKED

Project: {project_name}
Step: {step_number} - {step_name}
Reason: {reason}
Details: {details}

The workflow has stopped. Please review the issues above and provide guidance.
```
