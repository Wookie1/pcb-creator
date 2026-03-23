"""Step 0: Requirements — validate input and create project files."""

import json
import shutil
from pathlib import Path

from orchestrator.gather.calculator import calculate_requirements
from orchestrator.gather.schema import validate_requirements

from .base import StepBase, StepResult


class RequirementsStep(StepBase):
    @property
    def step_number(self) -> int:
        return 0

    @property
    def step_name(self) -> str:
        return "Requirements"

    def execute(
        self,
        requirements_path: Path | None = None,
        requirements: dict | None = None,
        attach_files: list[Path] | None = None,
    ) -> StepResult:
        """Validate requirements and create project files.

        Accepts either a path to a JSON file or a dict directly.
        attach_files: list of file paths to copy into the project directory.
        """
        # Load requirements
        if requirements is None:
            if requirements_path is None:
                return StepResult(success=False, error="No requirements provided")
            requirements = json.loads(requirements_path.read_text())

        # Validate against schema
        errors = validate_requirements(requirements)
        if errors:
            return StepResult(
                success=False,
                error=f"Requirements validation failed:\n" + "\n".join(f"  - {e}" for e in errors),
            )

        # Run engineering calculations
        requirements = calculate_requirements(requirements)

        # Create project directory and files
        self.project.initialize(requirements)

        # Copy attachment files to project directory
        if attach_files:
            self._copy_attachments(attach_files)

        # Also copy any attachments referenced in requirements JSON
        if requirements.get("attachments") and requirements_path:
            self._copy_referenced_attachments(requirements, requirements_path.parent)

        self.project.update_status(0, "COMPLETE")

        return StepResult(
            success=True,
            output_path=str(self.project.project_dir / "REQUIREMENTS.md"),
        )

    def _copy_attachments(self, files: list[Path]) -> None:
        """Copy explicit attachment files to project directory."""
        for src in files:
            if src.exists():
                dest = self.project.project_dir / src.name
                shutil.copy2(src, dest)
                print(f"  Copied attachment: {src.name}")
            else:
                print(f"  Warning: attachment not found: {src}")

    def _copy_referenced_attachments(
        self, requirements: dict, base_dir: Path
    ) -> None:
        """Copy files referenced in requirements attachments array."""
        for att in requirements.get("attachments", []):
            filename = att.get("filename", "")
            dest = self.project.project_dir / filename
            if dest.exists():
                continue  # Already copied (e.g., via --attach)
            # Try to find the file relative to the requirements file
            src = base_dir / filename
            if src.exists():
                shutil.copy2(src, dest)
                print(f"  Copied referenced attachment: {filename}")
            else:
                print(f"  Warning: referenced attachment not found: {filename}")
