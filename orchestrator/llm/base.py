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

    def generate_long(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        expected_min_length: int = 5000,
        partial_output: str | None = None,
    ) -> str:
        """Generate with forced continuation for models that stop early on long outputs.

        Falls back to generate() by default. Override for provider-specific logic.
        """
        return self.generate(system_prompt, user_prompt, max_tokens, temperature)

    def generate_with_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Send prompt with images, return raw text response.

        Args:
            images: List of PNG image bytes to include in the message.
        """
        raise NotImplementedError("This LLM client does not support vision.")
