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

    @property
    @abstractmethod
    def step_number(self) -> int: ...

    @property
    @abstractmethod
    def step_name(self) -> str: ...

    @abstractmethod
    def execute(self) -> StepResult: ...
