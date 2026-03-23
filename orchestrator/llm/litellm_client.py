"""LLM client using litellm for provider-agnostic API calls."""

import litellm

from .base import LLMClient

# Allow litellm to pass through non-standard params (e.g. "thinking")
# rather than raising UnsupportedParamsError
litellm.drop_params = True


class LiteLLMClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_body: dict | None = None,
    ):
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.extra_body = extra_body or {}

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        accumulated = ""
        max_continuations = 4

        kwargs: dict = {}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        for _ in range(max_continuations + 1):
            response = litellm.completion(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            chunk = response.choices[0].message.content or ""
            accumulated += chunk

            finish_reason = response.choices[0].finish_reason
            if finish_reason != "length":
                break

            # Output was truncated — ask the model to continue
            messages.append({"role": "assistant", "content": chunk})
            messages.append({"role": "user", "content": "Continue exactly where you left off. Do not repeat any content."})

        return accumulated
