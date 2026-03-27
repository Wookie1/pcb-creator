"""LLM client using litellm for provider-agnostic API calls."""

import base64

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
        timeout: float | None = None,
    ):
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.extra_body = extra_body or {}
        # Default 10min for cloud, but local models generating large JSON
        # (e.g. 21-component Arduino netlist ~30KB) can need 20-30min
        self.timeout = timeout or 1800  # 30 minutes

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
                timeout=self.timeout,
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

    def generate_with_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Send prompt with images using litellm's multimodal message format."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Build multimodal content: text + images
        content: list[dict] = [{"type": "text", "text": user_prompt}]
        for img_bytes in images:
            b64_data = base64.b64encode(img_bytes).decode("utf-8")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64_data}"},
            })
        messages.append({"role": "user", "content": content})

        kwargs: dict = {}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        response = litellm.completion(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.timeout,
            **kwargs,
        )
        return response.choices[0].message.content or ""
