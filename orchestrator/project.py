"""Project directory and file management. All file I/O goes through here."""

import json
from datetime import datetime, timezone
from pathlib import Path


class ProjectManager:
    def __init__(self, project_name: str, projects_dir: Path):
        self.project_name = project_name
        self.project_dir = projects_dir / project_name

    def initialize(self, requirements: dict) -> None:
        """Create project directory, REQUIREMENTS.md, and STATUS.json."""
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self._write_requirements_md(requirements)
        self._write_initial_status()

    def update_status(
        self,
        step: int,
        status: str,
        rework_count: int = 0,
        validator_errors: list[str] | None = None,
        validator_warnings: list[str] | None = None,
    ) -> None:
        """Update STATUS.json with new step status.

        validator_errors/validator_warnings are persisted when a step is
        BLOCKED or ERROR so MCP callers can understand what went wrong.
        """
        status_path = self.project_dir / "STATUS.json"
        data = json.loads(status_path.read_text()) if status_path.exists() else {
            "project_name": self.project_name,
            "current_step": 0,
            "current_status": status,
            "steps": {},
        }

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        step_key = str(step)

        data["current_step"] = step
        data["current_status"] = status

        if step_key not in data["steps"]:
            data["steps"][step_key] = {}

        data["steps"][step_key]["status"] = status
        data["steps"][step_key]["timestamp"] = now
        if rework_count > 0:
            data["steps"][step_key]["rework_count"] = rework_count
        if validator_errors is not None:
            data["steps"][step_key]["validator_errors"] = validator_errors
        if validator_warnings is not None:
            data["steps"][step_key]["validator_warnings"] = validator_warnings

        status_path.write_text(json.dumps(data, indent=2) + "\n")

    def write_output(self, filename: str, content: str) -> Path:
        """Write a step output file to the project directory. Returns the path."""
        path = self.project_dir / filename
        path.write_text(content)
        return path

    def write_quality(self, qa_report: dict) -> None:
        """Write or append QA report to QUALITY.json."""
        quality_path = self.project_dir / "QUALITY.json"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if quality_path.exists():
            data = json.loads(quality_path.read_text())
        else:
            data = {"project_name": self.project_name, "reviews": []}

        review = dict(qa_report)
        review["timestamp"] = now
        data["reviews"].append(review)

        quality_path.write_text(json.dumps(data, indent=2) + "\n")

    def read_requirements(self) -> str:
        """Read REQUIREMENTS.md content for prompt injection."""
        return (self.project_dir / "REQUIREMENTS.md").read_text()

    def read_requirements_json(self) -> dict:
        """Read the raw requirements JSON."""
        path = self.project_dir / f"{self.project_name}_requirements.json"
        return json.loads(path.read_text())

    def get_output_path(self, filename: str) -> Path:
        """Return absolute path to an output file."""
        return self.project_dir / filename

    def _write_requirements_md(self, requirements: dict) -> None:
        """Format requirements dict as REQUIREMENTS.md."""
        lines = [f"# Requirements: {requirements.get('project_name', self.project_name)}\n"]

        if desc := requirements.get("description"):
            lines.append(f"## Circuit Description\n\n{desc}\n")

        if power := requirements.get("power"):
            lines.append("## Power\n")
            lines.append(f"- Voltage: {power.get('voltage', 'N/A')}")
            lines.append(f"- Source: {power.get('source', 'N/A')}\n")

        if components := requirements.get("components"):
            lines.append("## Components\n")
            for comp in components:
                specs = comp.get("specs", {})
                spec_str = ", ".join(f"{k}={v}" for k, v in specs.items())
                line = f"- **{comp.get('ref', '?')}**: {comp.get('type', 'unknown')}"
                if value := comp.get("value"):
                    line += f", {value}"
                if package := comp.get("package"):
                    line += f" ({package})"
                if purpose := comp.get("purpose"):
                    line += f" — {purpose}"
                if spec_str:
                    line += f" [{spec_str}]"
                lines.append(line)
            lines.append("")

        if connections := requirements.get("connections"):
            lines.append("## Connections\n")
            for conn in connections:
                net_name = conn.get("net_name", "?")
                net_class = conn.get("net_class", "signal")
                pins = ", ".join(conn.get("pins", []))
                lines.append(f"- **{net_name}** ({net_class}): {pins}")
            lines.append("")

        if calcs := requirements.get("calculations"):
            lines.append("## Calculations\n")
            for name, calc in calcs.items():
                lines.append(f"- {name}: {calc.get('formula', '')} = {calc.get('value', '')}")
                if power_val := calc.get("power"):
                    lines.append(f"  Power: {power_val}")
            lines.append("")

        if packages := requirements.get("packages"):
            lines.append(f"## Packages\n\n{packages}\n")

        if board := requirements.get("board"):
            lines.append("## Board\n")
            if w := board.get("width_mm"):
                lines.append(f"- Width: {w}mm")
            if h := board.get("height_mm"):
                lines.append(f"- Height: {h}mm")
            if cr := board.get("corner_radius_mm"):
                lines.append(f"- Corner radius: {cr}mm")
            if layers := board.get("layers"):
                lines.append(f"- Layers: {layers}")
            if ot := board.get("outline_type"):
                lines.append(f"- Outline type: {ot}")
            lines.append("")

        if hints := requirements.get("placement_hints"):
            lines.append("## Placement Hints\n")
            for hint in hints:
                ref = hint.get("ref", "?")
                parts = [f"**{ref}**"]
                if "x_mm" in hint and "y_mm" in hint:
                    parts.append(f"at ({hint['x_mm']}, {hint['y_mm']})mm")
                if rot := hint.get("rotation_deg"):
                    parts.append(f"rotated {rot}°")
                if edge := hint.get("edge"):
                    parts.append(f"on {edge} edge")
                if near := hint.get("near"):
                    parts.append(f"near {near}")
                lines.append(f"- {' '.join(parts)}")
            lines.append("")

        if attachments := requirements.get("attachments"):
            lines.append("## Attachments\n")
            for att in attachments:
                line = f"- **{att['filename']}** ({att['type']}): {att['purpose']}"
                if steps := att.get("used_by_steps"):
                    line += f" [steps: {', '.join(str(s) for s in steps)}]"
                lines.append(line)
            lines.append("")

        (self.project_dir / "REQUIREMENTS.md").write_text("\n".join(lines))

        # Also save raw JSON for programmatic access
        raw_path = self.project_dir / f"{self.project_name}_requirements.json"
        raw_path.write_text(json.dumps(requirements, indent=2) + "\n")

    def _write_initial_status(self) -> None:
        """Create initial STATUS.json."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = {
            "project_name": self.project_name,
            "current_step": 0,
            "current_status": "NOT_STARTED",
            "steps": {},
        }
        (self.project_dir / "STATUS.json").write_text(
            json.dumps(status, indent=2) + "\n"
        )
