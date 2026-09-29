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
    """Configured default. The concrete client depends on health — see fallback tests."""
    s = Settings()
    assert s.llm_provider == "anthropic"
    assert s.anthropic_model == "claude-haiku-4-5"


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


def test_a_rejected_key_falls_back_to_ollama_instead_of_failing_the_run(monkeypatch):
    """A 401 must not fail every field of every application for a whole batch."""
    from autoapply.ollama import OllamaClient

    monkeypatch.setattr(
        AnthropicClient, "health", lambda self: (False, "credentials rejected")
    )
    monkeypatch.setattr(OllamaClient, "health", lambda self: (True, "Ollama OK"))
    assert isinstance(make_client(Settings()), OllamaClient)


def test_a_working_key_is_used(monkeypatch):
    monkeypatch.setattr(AnthropicClient, "health", lambda self: (True, "OK"))
    assert isinstance(make_client(Settings()), AnthropicClient)


def test_both_down_reports_the_configured_provider(monkeypatch):
    """The error should name Anthropic, not confusingly blame Ollama."""
    from autoapply.ollama import OllamaClient

    monkeypatch.setattr(AnthropicClient, "health", lambda self: (False, "rejected"))
    monkeypatch.setattr(OllamaClient, "health", lambda self: (False, "not running"))
    assert isinstance(make_client(Settings()), AnthropicClient)


def test_dotenv_is_loaded_but_never_overrides_a_real_env_var(tmp_path, monkeypatch):
    """launchd inherits almost no environment, so .env is how a scheduled run
    sees the key. An explicit export must still win."""
    import os

    from autoapply import config

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ANTHROPIC_API_KEY=from-file\n"
        'export AUTOAPPLY_ANTHROPIC_MODEL="claude-haiku-4-5"\n'
        "AUTOAPPLY_LLM=anthropic\n"
        "malformed line with no equals\n"
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AUTOAPPLY_LLM", "ollama")  # already set => file must not win
    config._load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"
    assert os.environ["AUTOAPPLY_ANTHROPIC_MODEL"] == "claude-haiku-4-5"  # quotes stripped
    assert os.environ["AUTOAPPLY_LLM"] == "ollama"


def test_missing_dotenv_is_not_an_error(tmp_path):
    from autoapply import config

    config._load_dotenv(tmp_path / "nope.env")  # must not raise


def test_default_model_is_the_cost_efficient_one():
    assert Settings().anthropic_model == "claude-haiku-4-5"


def test_effort_is_only_sent_to_models_that_accept_it():
    """Haiku 4.5 rejects output_config.effort with a 400 — seen live."""
    from autoapply.anthropic_client import supports_effort

    assert not supports_effort("claude-haiku-4-5")
    assert supports_effort("claude-opus-5")
    assert supports_effort("claude-sonnet-5")


def test_haiku_request_omits_output_config():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return _fake_response()

    c = _stub(create)
    c.model = "claude-haiku-4-5"
    c.chat([{"role": "user", "content": "q"}])
    assert "output_config" not in seen


def test_effort_model_still_gets_output_config():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return _fake_response()

    c = _stub(create)
    c.model = "claude-opus-5"
    c.chat([{"role": "user", "content": "q"}])
    assert seen["output_config"] == {"effort": "low"}
