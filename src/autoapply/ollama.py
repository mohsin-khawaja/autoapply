"""Thin client for a local Ollama server (SPEC.md §2).

Used by the scorer's optional relevance pass and by workstream D's free-text
answer generation. No third-party SDK — just the HTTP chat endpoint. Fails
gracefully with actionable instructions when Ollama is down or the model is
absent (never falls back to a paid API — SPEC.md §1).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class OllamaError(RuntimeError):
    """Ollama is unreachable, the model is missing, or a request failed."""


@dataclass(slots=True)
class OllamaClient:
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct"
    timeout: float = 120.0

    def health(self) -> tuple[bool, str]:
        """Return (ok, message). ok False => message explains the fix."""
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return False, (
                f"Ollama not reachable at {self.host} ({e}). "
                "Install from https://ollama.com and run `ollama serve`."
            )
        models = {m.get("name", "") for m in r.json().get("models", [])}
        if not any(m == self.model or m.startswith(self.model) for m in models):
            return False, (
                f"Model {self.model!r} not found. Run `ollama pull {self.model}`. "
                f"Installed: {', '.join(sorted(models)) or '(none)'}"
            )
        return True, f"Ollama OK ({self.model})"

    def ensure_available(self) -> None:
        """Raise :class:`OllamaError` with fix instructions if not ready."""
        ok, msg = self.health()
        if not ok:
            raise OllamaError(msg)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> str:
        """Send a chat completion and return the assistant text.

        ``messages`` is a list of ``{"role": ..., "content": ...}``. Non-streaming.
        """
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise OllamaError(f"Ollama chat request failed: {e}") from e
        return r.json().get("message", {}).get("content", "").strip()
