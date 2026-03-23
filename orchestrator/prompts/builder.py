"""Prompt builder: loads Jinja2 templates and injects STANDARDS.md excerpts."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .excerpts import load_standards


class PromptBuilder:
    def __init__(self, base_dir: Path):
        templates_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.standards = load_standards(base_dir / "STANDARDS.md")
        self.engineering_rules = self._load_engineering_rules()

    def _load_engineering_rules(self) -> str:
        """Load the shared engineering rules document."""
        rules_path = Path(__file__).parent / "engineering_rules.md"
        return rules_path.read_text().strip()

    def render(self, template_name: str, context: dict) -> str:
        """Render a template with the given context.

        The standards sections are automatically available as
        standards_1, standards_2, etc. Engineering rules are
        available as engineering_rules.
        """
        full_context = {
            f"standards_{k}": v for k, v in self.standards.items()
        }
        full_context["engineering_rules"] = self.engineering_rules
        full_context.update(context)
        template = self.env.get_template(f"{template_name}.md.j2")
        return template.render(full_context)

    def get_validation_rules(self) -> str:
        """Get STANDARDS.md Section 4 (Validation Rules) for QA prompts."""
        return self.standards.get("4", "")
