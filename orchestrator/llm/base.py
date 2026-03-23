"""Abstract LLM client interface."""

from abc import ABC, abstractmethod


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> str:
        """Send prompt, return raw text response."""
        ...
