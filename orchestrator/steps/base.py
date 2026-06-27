"""Base step interface."""

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.config import OrchestratorConfig
from orchestrator.project import ProjectManager
from orchestrator.prompts.builder import PromptBuilder

if TYPE_CHECKING:
    from orchestrator.llm.litellm_client import LiteLLMClient


@dataclass
class StepResult:
    success: bool
    output_path: str | None = None
    error: str | None = None
    qa_report: dict | None = None


class StepBase(ABC):
    def __init__(
        self,
        project: ProjectManager,
        llm: "LiteLLMClient",
        prompt_builder: PromptBuilder,
        config: OrchestratorConfig,
    ):
        self.project = project
        self.llm = llm
        self.prompt_builder = prompt_builder
        self.config = config
        # Optional progress sink set by the runner (e.g. the async design_pcb
        # job's _on_progress). None = no reporting. Lets a long-running step
        # surface sub-phase progress to a polling agent in real time.
        self.progress_callback = None

    def _report_progress(self, **fields) -> None:
        """Emit a progress update if a callback is wired; never raise."""
        cb = getattr(self, "progress_callback", None)
        if cb is None:
            return
        try:
            cb(fields)
        except Exception:
            pass  # progress reporting must never break the pipeline

    def _run_validator_cmd(self, cmd: list[str]) -> dict:
        """Run a validator subprocess and parse its JSON stdout (crash-tolerant)."""
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(self.config.base_dir)
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "valid": False,
                "errors": [f"Validator crashed: {result.stderr or result.stdout}"],
                "warnings": [],
                "summary": "Validator execution failed",
            }

    @property
    @abstractmethod
    def step_number(self) -> int: ...

    @property
    @abstractmethod
    def step_name(self) -> str: ...

    @abstractmethod
    def execute(self) -> StepResult: ...
