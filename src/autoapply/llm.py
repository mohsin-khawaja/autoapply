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


def _ollama(settings: Settings, timeout: float | None) -> ChatClient:
    from autoapply.ollama import OllamaClient

    return OllamaClient(
        host=settings.ollama_host,
        model=settings.ollama_model,
        timeout=timeout if timeout is not None else 120.0,
    )


def make_client(settings: Settings, *, timeout: float | None = None) -> ChatClient:
    """Build the configured chat client, falling back to Ollama if needed.

    A rejected API key or an unreachable API would otherwise fail every field of
    every application for the whole run — an unattended batch would burn hours
    producing nothing. The provider is checked once here (cheap: no network call
    when credentials are simply absent) and a local model is used instead, so a
    run degrades in quality rather than collapsing.
    """
    if settings.llm_provider == "ollama":
        return _ollama(settings, timeout)

    from autoapply.anthropic_client import AnthropicClient

    client = AnthropicClient(
        model=settings.anthropic_model,
        timeout=timeout if timeout is not None else 60.0,
        workspace_id=settings.anthropic_workspace_id,
    )
    ok, msg = client.health()
    if ok:
        return client

    fallback = _ollama(settings, timeout)
    ok_local, _ = fallback.health()
    if ok_local:
        print(f"[llm] Anthropic unavailable ({msg}) — falling back to local Ollama.")
        return fallback
    # Neither works: return the configured client so the failure names the
    # provider the user actually asked for.
    return client
