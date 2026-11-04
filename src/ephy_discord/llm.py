"""OpenAI-compatible chat client for the ephy-runtime Gateway or llama.cpp."""

from __future__ import annotations

import httpx

from ephy_discord.config import LlmConfig
from ephy_discord.text import strip_thinking


class LlmError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LlmResult:
    def __init__(self, text: str, model: str) -> None:
        self.text = text
        self.model = model


class LlmClient:
    """POST `{base_url}/chat/completions`. `base_url` already includes `/v1`.

    Gateway requests set `metadata.web_search` to false. O-1 never asks the
    Gateway to call Grok, OpenAI, or the worker web-search bridge.
    """

    def __init__(self, config: LlmConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, messages: list[dict]) -> LlmResult:
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if self.config.via_gateway:
            body["metadata"] = {"mode": self.config.gateway_mode, "web_search": False}
        url = f"{self.config.base_url}/chat/completions"
        try:
            response = await self._client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise LlmError("llm_timeout") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LlmError("llm_unavailable") from exc
        text = strip_thinking(_message_text(payload))
        if not text:
            raise LlmError("llm_empty")
        model = str(payload.get("model") or self.config.model)
        return LlmResult(text=text, model=model)


def _message_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LlmError("llm_bad_response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LlmError("llm_bad_response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LlmError("llm_bad_response")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    raise LlmError("llm_bad_response")
