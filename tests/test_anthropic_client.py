"""Anthropic client: message shaping, provider switch, error mapping."""

from __future__ import annotations

import anthropic
import pytest

from autoapply.anthropic_client import AnthropicClient, AnthropicError, _split_system
from autoapply.config import Settings
from autoapply.llm import ChatClient, make_client


def test_system_turns_are_split_out_of_the_message_list():
    """Ollama inlines system messages; the Messages API takes them separately."""
    system, turns = _split_system([
        {"role": "system", "content": "You are the candidate."},
        {"role": "user", "content": "Why this job?"},
    ])
    assert turns == [{"role": "user", "content": "Why this job?"}]
    assert system[0]["text"] == "You are the candidate."
    # Byte-identical across every question in a run, so it should cache.
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_multiple_system_messages_are_merged():
    system, turns = _split_system([
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "q"},
    ])
    assert system[0]["text"] == "A\n\nB"
    assert len(turns) == 1


def test_no_system_message_sends_no_system_block():
    system, turns = _split_system([{"role": "user", "content": "q"}])
    assert system == []
    assert len(turns) == 1


def test_chat_requires_a_user_turn():
    c = AnthropicClient()
    with pytest.raises(AnthropicError):
        c.chat([{"role": "system", "content": "only a system prompt"}])


def _fake_response(stop_reason="end_turn", text="hello"):
    class _Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Resp:
        content = [_Block(text)]

    _Resp.stop_reason = stop_reason
    return _Resp()


def _stub(create):
    """An AnthropicClient with its SDK client replaced (slots blocks method patching)."""
    c = AnthropicClient()
    c._client = type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()
    return c


def test_chat_returns_concatenated_text():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return _fake_response(text="  an answer  ")

    assert _stub(create).chat([{"role": "user", "content": "q"}], temperature=0.7) == "an answer"
    # Sampling params are rejected on current models — never send one.
    assert "temperature" not in seen
    assert seen["model"] == "claude-opus-5"
    assert seen["output_config"] == {"effort": "low"}


def test_a_refusal_yields_no_answer_rather_than_raising():
    """A safety decline returns HTTP 200 — reading content blindly would fill junk."""
    c = _stub(lambda **kw: _fake_response(stop_reason="refusal", text="I can't help"))
    # Empty => the field stays needs_input instead of being filled with a refusal.
    assert c.chat([{"role": "user", "content": "q"}]) == ""


def test_api_errors_become_anthropic_error():
    """The runner catches one exception type to degrade a field to manual."""

    def boom(**kw):
        raise anthropic.APIConnectionError(request=None)

    with pytest.raises(AnthropicError):
        _stub(boom).chat([{"role": "user", "content": "q"}])


# ---- provider switch -------------------------------------------------------


def test_default_provider_is_anthropic():
    client = make_client(Settings())
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-opus-5"


def test_ollama_is_still_selectable():
    from autoapply.ollama import OllamaClient

    s = Settings()
    s.llm_provider = "ollama"
    assert isinstance(make_client(s), OllamaClient)


def test_both_clients_satisfy_the_shared_protocol():
    """llm_answers depends on the protocol, not on either concrete client."""
    from autoapply.ollama import OllamaClient

    assert isinstance(AnthropicClient(), ChatClient)
    assert isinstance(OllamaClient(), ChatClient)
