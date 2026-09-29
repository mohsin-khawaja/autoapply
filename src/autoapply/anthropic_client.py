"""Anthropic API client for free-text and constrained answer generation.

Drop-in replacement for :class:`autoapply.ollama.OllamaClient`: same ``chat``,
``health``, and ``ensure_available`` surface, so
:mod:`autoapply.filling.llm_answers` works against either provider unchanged.
Pick one with ``AUTOAPPLY_LLM=anthropic|ollama`` (see :func:`autoapply.llm.make_client`).

Why this exists: a local 7B model was the bottleneck on answer quality and
latency — slow generations pushed applications into the per-job timeout, and
weak constrained-choice answers left fields unresolved, which blocks the submit
gate. This trades tokens for both.

Cost control: answers are short, so requests run at ``effort: "low"`` with a
small ``max_tokens``. The system prompt carries ``cache_control`` since it is
byte-identical across every question in a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import anthropic

#: Answers are a few sentences at most; a form field is not an essay.
_MAX_TOKENS = 1024

#: ``output_config.effort`` is rejected with a 400 on the small/older models.
#: Only the Opus family, Sonnet 5 and Fable accept it.
_EFFORT_MODELS = ("claude-opus-", "claude-sonnet-5", "claude-fable-", "claude-mythos-")


def supports_effort(model: str) -> bool:
    return model.startswith(_EFFORT_MODELS)


class AnthropicError(RuntimeError):
    """The Anthropic API is unreachable, unauthenticated, or refused."""


@dataclass(slots=True)
class AnthropicClient:
    """Chat client backed by the Anthropic Messages API.

    ``model`` and ``timeout`` mirror :class:`OllamaClient` so callers can swap
    providers without touching call sites.
    """

    model: str = "claude-opus-5"
    timeout: float = 60.0
    max_tokens: int = _MAX_TOKENS
    effort: str = "low"
    #: Required for identity-linked keys; ignored when empty.
    workspace_id: str = ""
    _client: anthropic.Anthropic | None = field(default=None, repr=False)

    def _api(self) -> anthropic.Anthropic:
        if self._client is None:
            # Zero-arg constructor resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
            # or an `ant auth login` profile — never hardcode a key here.
            headers = (
                {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
            )
            self._client = anthropic.Anthropic(timeout=self.timeout, default_headers=headers)
        return self._client

    # ---- parity surface with OllamaClient ---------------------------------

    def health(self) -> tuple[bool, str]:
        """Return (ok, message). ok False => message explains the fix."""
        if not _has_credentials():
            return False, (
                "No Anthropic credentials. Run `ant auth login`, or export "
                "ANTHROPIC_API_KEY. Set AUTOAPPLY_LLM=ollama to stay local."
            )
        try:
            self._api().models.retrieve(self.model)
        except anthropic.AuthenticationError:
            return False, "Anthropic credentials rejected — check your API key."
        except anthropic.NotFoundError:
            return False, f"Model {self.model!r} is not available to this account."
        except anthropic.APIConnectionError as e:
            return False, f"Cannot reach the Anthropic API ({e})."
        except anthropic.BadRequestError as e:
            if "anthropic-workspace-id" in str(e):
                return False, (
                    "This is an identity-linked API key — it needs a workspace id. "
                    "Add ANTHROPIC_WORKSPACE_ID=wrkspc_... to .env "
                    "(Console -> Settings -> Workspaces)."
                )
            return False, f"Anthropic API error 400: {e.message}"
        except anthropic.APIStatusError as e:
            return False, f"Anthropic API error {e.status_code}: {e.message}"
        return True, f"Anthropic OK ({self.model})"

    def ensure_available(self) -> None:
        ok, msg = self.health()
        if not ok:
            raise AnthropicError(msg)

    def resolve_model(self, *, fallback: str | None = None) -> str | None:
        """Parity with the Ollama client; the API model is not host-dependent."""
        return self.model

    def installed_models(self) -> list[str]:
        try:
            return [m.id for m in self._api().models.list()]
        except anthropic.APIError:
            return []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,  # noqa: ARG002 - removed on current models
        model: str | None = None,
    ) -> str:
        """Send a chat completion and return the assistant text.

        Accepts the Ollama-shaped message list (system entries inline) and
        splits the system turns out, which the Messages API takes separately.
        ``temperature`` is accepted for call-site parity but ignored: sampling
        parameters are rejected on current models.
        """
        system, turns = _split_system(messages)
        if not turns:
            raise AnthropicError("chat() requires at least one user message")
        try:
            name = model or self.model
            extra: dict[str, object] = {}
            if self.effort and supports_effort(name):
                # Short, well-specified answers: minimum depth is enough and
                # keeps per-application cost near the floor. Rejected outright
                # by Haiku, hence the guard.
                extra["output_config"] = {"effort": self.effort}
            resp = self._api().messages.create(
                model=name,
                max_tokens=self.max_tokens,
                system=system,
                messages=turns,
                **extra,
            )
        except anthropic.AuthenticationError as e:
            raise AnthropicError(f"Anthropic auth failed: {e}") from e
        except anthropic.RateLimitError as e:
            raise AnthropicError(f"Anthropic rate limited: {e}") from e
        except anthropic.APIConnectionError as e:
            raise AnthropicError(f"Anthropic connection failed: {e}") from e
        except anthropic.APIStatusError as e:
            raise AnthropicError(f"Anthropic API error {e.status_code}: {e.message}") from e

        # A safety decline returns HTTP 200 — never read content without checking.
        if resp.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def _split_system(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Split Ollama-style messages into (system blocks, user/assistant turns)."""
    system_text = "\n\n".join(
        m["content"] for m in messages if m.get("role") == "system" and m.get("content")
    )
    turns = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    if not system_text:
        return [], turns
    # Identical across every question in a run, so it is worth caching.
    return (
        [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        turns,
    )


def _has_credentials() -> bool:
    """True when the SDK has something to authenticate with.

    An unset ANTHROPIC_API_KEY does not mean unauthenticated — an `ant auth
    login` profile on disk works with the zero-arg constructor.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path

    return Path.home().joinpath(".config", "anthropic").exists()
