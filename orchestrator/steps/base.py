"""Base step interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from orchestrator.config import OrchestratorConfig
from orchestrator.llm.base import LLMClient
from orchestrator.project import ProjectManager
from orchestrator.prompts.builder import PromptBuilder


@dataclass
class StepResult:
    success: bool
    output_path: str | None = None
    error: str | None = None
    qa_report: dict | None = None
    needs_approval: bool = False


class StepBase(ABC):
    def __init__(
        self,
        project: ProjectManager,
        llm: LLMClient,
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

    @property
    @abstractmethod
    def step_number(self) -> int: ...

    @property
    @abstractmethod
    def step_name(self) -> str: ...

    @abstractmethod
    def execute(self) -> StepResult: ...
