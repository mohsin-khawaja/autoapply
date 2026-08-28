"""LLM provider selection.

One place decides which backend answers application questions. Both clients
expose the same ``chat``/``health``/``ensure_available`` surface, so callers
never branch on provider.

    AUTOAPPLY_LLM=anthropic   # Anthropic API (default)
    AUTOAPPLY_LLM=ollama      # local Ollama, no tokens spent

Discovery never calls this — it is pure HTTP and must stay LLM-free so it can
run unattended from launchd.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from autoapply.config import Settings


@runtime_checkable
class ChatClient(Protocol):
    """The surface :mod:`autoapply.filling.llm_answers` depends on."""

    def chat(
        self, messages: list[dict[str, str]], *, temperature: float = ..., model: str | None = ...
    ) -> str: ...

    def health(self) -> tuple[bool, str]: ...

    def ensure_available(self) -> None: ...


def make_client(settings: Settings, *, timeout: float | None = None) -> ChatClient:
    """Build the configured chat client."""
    if settings.llm_provider == "ollama":
        from autoapply.ollama import OllamaClient

        return OllamaClient(
            host=settings.ollama_host,
            model=settings.ollama_model,
            timeout=timeout if timeout is not None else 120.0,
        )

    from autoapply.anthropic_client import AnthropicClient

    return AnthropicClient(
        model=settings.anthropic_model,
        timeout=timeout if timeout is not None else 60.0,
    )
