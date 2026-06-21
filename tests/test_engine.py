import json

import pytest

from franky.engine import (
    CLAUDE_TOKEN_VAR,
    DEFAULT_ENGINE,
    PI_PROVIDER_VARS,
    ClaudeEngine,
    PiEngine,
    _fallback_pr_url,
    resolve_engine,
)

PR_URL = "https://github.com/octocat/hello/pull/7"


def test_pi_inner_argv_without_model():
    assert PiEngine().inner_argv("do it", None) == ["pi", "-p", "do it", "--mode", "json"]


def test_pi_inner_argv_with_model():
    assert PiEngine().inner_argv("do it", "gpt-x") == [
        "pi", "-p", "do it", "--mode", "json", "--model", "gpt-x",
    ]


def test_claude_inner_argv_without_model():
    assert ClaudeEngine().inner_argv("do it", None) == [
        "claude", "-p", "do it", "--output-format", "stream-json", "--dangerously-skip-permissions",
    ]


def test_claude_inner_argv_with_model():
    argv = ClaudeEngine().inner_argv("do it", "claude-x")
    assert argv[-2:] == ["--model", "claude-x"]
    assert "--dangerously-skip-permissions" in argv
    assert "--verbose" not in argv


def test_default_engine_is_pi():
    assert DEFAULT_ENGINE == "pi"
    assert isinstance(resolve_engine(None, {}), PiEngine)


def test_resolve_engine_precedence(monkeypatch):
    # flag wins over env
    assert isinstance(resolve_engine("claude", {"FRANKY_ENGINE": "pi"}), ClaudeEngine)
    # env wins over default
    assert isinstance(resolve_engine(None, {"FRANKY_ENGINE": "claude"}), ClaudeEngine)
    # default
    assert isinstance(resolve_engine(None, {}), PiEngine)


def test_resolve_engine_unknown_raises():
    with pytest.raises(ValueError, match="unknown engine"):
        resolve_engine("bogus", {})


def test_pi_parse_pr_url_from_jsonl():
    lines = [
        json.dumps({"type": "system", "msg": "starting"}),
        "not json at all",
        json.dumps({"type": "tool_result", "content": f"opened {PR_URL}"}),
        json.dumps({"type": "done"}),
    ]
    assert PiEngine().parse_pr_url("\n".join(lines)) == PR_URL


def test_claude_parse_pr_url_from_stream_json():
    line = json.dumps({"type": "assistant", "message": {"text": f"PR is up: {PR_URL}"}})
    assert ClaudeEngine().parse_pr_url(line) == PR_URL


def test_fallback_regex_on_plain_text():
    text = f"some log\nfinal: {PR_URL}\n"
    assert _fallback_pr_url(text) == PR_URL


def test_fallback_returns_last_match():
    older = "https://github.com/octocat/hello/pull/1"
    text = f"{older}\nlater {PR_URL}"
    assert _fallback_pr_url(text) == PR_URL


def test_parse_pr_url_none_when_absent():
    assert PiEngine().parse_pr_url('{"type":"done"}\nno url here') is None


def test_claude_required_env():
    assert ClaudeEngine().required_env() == [CLAUDE_TOKEN_VAR]


def test_pi_required_env_returns_present_subset(monkeypatch):
    for v in PI_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    present = PiEngine().required_env()
    assert "OPENROUTER_API_KEY" in present
    assert "OPENAI_API_KEY" in present
    assert "ANTHROPIC_API_KEY" not in present


def test_pi_required_env_empty_when_none_set(monkeypatch):
    for v in PI_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    assert PiEngine().required_env() == []
