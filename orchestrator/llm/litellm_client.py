"""LLM client using litellm for provider-agnostic API calls."""

import base64
import json
import logging
import time

import litellm

logger = logging.getLogger(__name__)

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
        self._max_retries = 3
        self._retry_base_delay = 5  # seconds

    def _call_with_retry(self, **kwargs) -> "litellm.ModelResponse":
        """Call litellm.completion with retry on transient API errors."""
        for attempt in range(1, self._max_retries + 1):
            try:
                return litellm.completion(**kwargs)
            except Exception as e:
                err_str = str(e).lower()
                is_transient = any(s in err_str for s in (
                    "timeout", "rate limit", "429", "500", "502", "503", "504",
                    "connection", "unable to get", "server error", "overloaded",
                ))
                if not is_transient or attempt == self._max_retries:
                    raise
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %ds: %s",
                    attempt, self._max_retries, delay, e,
                )
                print(f"    [LLM] Transient error (attempt {attempt}/{self._max_retries}), "
                      f"retrying in {delay}s: {e}")
                time.sleep(delay)

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

        for iteration in range(max_continuations + 1):
            response = self._call_with_retry(
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
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", "?")
            completion_tokens = getattr(usage, "completion_tokens", "?")
            logger.info(
                "LLM generate iteration=%d finish_reason=%s chunk_len=%d accumulated_len=%d "
                "model=%s max_tokens=%d prompt_tokens=%s completion_tokens=%s",
                iteration, finish_reason, len(chunk), len(accumulated),
                self.model, max_tokens, prompt_tokens, completion_tokens,
            )
            print(
                f"    [LLM] iter={iteration} finish={finish_reason!r} "
                f"chunk={len(chunk)} total={len(accumulated)} "
                f"tokens(in={prompt_tokens} out={completion_tokens})"
            )

            if finish_reason != "length":
                if finish_reason == "stop" and len(accumulated) < 5000:
                    logger.warning(
                        "LLM stopped early with only %d chars (finish_reason=stop). "
                        "Output may be truncated.", len(accumulated)
                    )
                    print(
                        f"    [LLM] WARNING: model stopped early with only {len(accumulated)} chars "
                        f"(finish_reason=stop, completion_tokens={completion_tokens}). "
                        f"Output likely truncated."
                    )
                break

            # Output was truncated — ask the model to continue
            logger.info("Continuation %d: output truncated at %d chars, requesting more...",
                        iteration + 1, len(accumulated))
            print(f"    [LLM] finish=length — requesting continuation {iteration + 1}...")
            messages.append({"role": "assistant", "content": chunk})
            messages.append({"role": "user", "content": "Continue exactly where you left off. Do not repeat any content."})

        logger.info("LLM generate complete: total_len=%d iterations=%d", len(accumulated), iteration + 1)
        return accumulated

    def generate_long(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        expected_min_length: int = 5000,
        partial_output: str | None = None,
    ) -> str:
        """Generate with forced continuation when the model stops early on long JSON."""
        # Start from partial output or do a fresh generate
        if partial_output is not None:
            accumulated = partial_output
        else:
            accumulated = self.generate(system_prompt, user_prompt, max_tokens, temperature)

        # Check if output looks like truncated JSON (object or array)
        max_continuations = 3
        for cont in range(max_continuations):
            stripped = accumulated.strip()
            if not (stripped.startswith("{") or stripped.startswith("[")):
                break  # Not JSON, nothing to continue
            try:
                json.loads(stripped)
                break  # Valid JSON, done
            except json.JSONDecodeError:
                pass

            if len(stripped) >= expected_min_length:
                break  # Long enough, let caller handle parse

            # Build continuation request
            tail = stripped[-300:] if len(stripped) > 300 else stripped
            cont_prompt = (
                f"Your JSON output was cut short at {len(stripped)} characters. "
                f"The complete output needs approximately {expected_min_length} characters. "
                f"Continue generating the JSON exactly from where you stopped. "
                f"Do NOT restart from the beginning. Do NOT repeat any content. "
                f"Begin immediately with the next characters after:\n...{tail}"
            )

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})
            messages.append({"role": "assistant", "content": stripped})
            messages.append({"role": "user", "content": cont_prompt})

            kwargs: dict = {}
            if self.api_base:
                kwargs["api_base"] = self.api_base
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.extra_body:
                kwargs["extra_body"] = self.extra_body

            logger.info(
                "generate_long continuation %d: accumulated=%d expected_min=%d",
                cont + 1, len(stripped), expected_min_length,
            )
            print(
                f"    [generate_long] cont={cont + 1} partial={len(stripped)} "
                f"expected_min={expected_min_length}"
            )
            response = self._call_with_retry(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=self.timeout,
                **kwargs,
            )
            chunk = response.choices[0].message.content or ""
            finish = response.choices[0].finish_reason
            usage = getattr(response, "usage", None)
            completion_tokens = getattr(usage, "completion_tokens", "?")
            accumulated = stripped + chunk
            logger.info(
                "generate_long continuation %d: got %d chars, total now %d",
                cont + 1, len(chunk), len(accumulated),
            )
            print(
                f"    [generate_long] cont={cont + 1} got={len(chunk)} "
                f"finish={finish!r} tokens_out={completion_tokens} total={len(accumulated)}"
            )

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

        response = self._call_with_retry(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.timeout,
            **kwargs,
        )
        return response.choices[0].message.content or ""
