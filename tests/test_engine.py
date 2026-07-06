import json

import pytest

from franky.engine import (
    CLAUDE_TOKEN_VAR,
    CODEX_PROVIDER_VARS,
    DEFAULT_ENGINE,
    ENGINES,
    PI_PROVIDER_VARS,
    PR_URL_RE,
    ClaudeEngine,
    CodexEngine,
    Engine,
    PiEngine,
    _fallback_pr_url,
    resolve_engine,
)

PR_URL = "https://github.com/octocat/hello/pull/7"


def test_pi_inner_argv_without_model():
    assert PiEngine().inner_argv("do it", None) == ["pi", "-p", "do it", "--mode", "json"]


def test_pi_inner_argv_with_model():
    assert PiEngine().inner_argv("do it", "gpt-x") == [
        "pi",
        "-p",
        "do it",
        "--mode",
        "json",
        "--model",
        "gpt-x",
    ]


def test_claude_inner_argv_without_model():
    assert ClaudeEngine().inner_argv("do it", None) == [
        "claude",
        "-p",
        "do it",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]


def test_claude_inner_argv_with_model():
    argv = ClaudeEngine().inner_argv("do it", "claude-x")
    assert argv[-2:] == ["--model", "claude-x"]
    assert "--dangerously-skip-permissions" in argv
    assert "--verbose" in argv


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
    assert _fallback_pr_url(text, PR_URL_RE) == PR_URL


def test_fallback_returns_last_match():
    older = "https://github.com/octocat/hello/pull/1"
    text = f"{older}\nlater {PR_URL}"
    assert _fallback_pr_url(text, PR_URL_RE) == PR_URL


def test_parse_pr_url_none_when_absent():
    assert PiEngine().parse_pr_url('{"type":"done"}\nno url here') is None


def test_parse_pr_url_scoped_to_repo_ignores_other_repo():
    # A PR URL for a different repo must be ignored when a target repo is given.
    hostile = "https://github.com/attacker/evil/pull/1"
    good = "https://github.com/octocat/hello/pull/7"
    out = "\n".join(
        [
            json.dumps({"type": "tool_result", "content": f"see {hostile}"}),
            json.dumps({"type": "assistant", "message": {"text": f"opened {good}"}}),
        ]
    )
    assert PiEngine().parse_pr_url(out, repo="octocat/hello") == good
    # the hostile-only output yields nothing when scoped to our repo
    only_hostile = json.dumps({"type": "tool_result", "content": hostile})
    assert PiEngine().parse_pr_url(only_hostile, repo="octocat/hello") is None


def test_parse_pr_url_unscoped_when_no_repo():
    # No repo -> generic match (last wins), preserving prior behaviour.
    out = json.dumps({"type": "done", "url": PR_URL})
    assert PiEngine().parse_pr_url(out) == PR_URL


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


def test_pi_cred_hint_lists_provider_vars():
    hint = PiEngine().cred_hint()
    # The hint must enumerate the BYOK provider vars so a keyless pi user knows the options.
    assert "OPENROUTER_API_KEY" in hint
    assert "ANTHROPIC_API_KEY" in hint
    for v in PI_PROVIDER_VARS:
        assert v in hint


def test_claude_cred_hint_names_its_token():
    hint = ClaudeEngine().cred_hint()
    assert CLAUDE_TOKEN_VAR in hint
    # claude must NOT advertise pi's provider vars.
    assert "OPENROUTER_API_KEY" not in hint


def test_engine_base_cred_hint_not_implemented():
    # The base contract is abstract so a new engine that forgets cred_hint fails loudly.
    with pytest.raises(NotImplementedError):
        Engine().cred_hint()


# --- codex engine -----------------------------------------------------------


def test_codex_inner_argv_without_model():
    # The dangerous-bypass flag is load-bearing: codex self-sandboxes (Landlock/seccomp) and
    # prompts for approval; both must be off for a headless run inside Franky's container.
    assert CodexEngine().inner_argv("do it", None) == [
        "codex",
        "exec",
        "do it",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_codex_inner_argv_with_model():
    argv = CodexEngine().inner_argv("do it", "gpt-5.4")
    assert argv[-2:] == ["--model", "gpt-5.4"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--json" in argv


def test_codex_registered_and_resolvable():
    assert ENGINES.get("codex") is CodexEngine
    # both the flag and the FRANKY_ENGINE env path must reach codex
    assert isinstance(resolve_engine("codex", {}), CodexEngine)
    assert isinstance(resolve_engine(None, {"FRANKY_ENGINE": "codex"}), CodexEngine)


def test_codex_parse_pr_url_from_jsonl():
    # codex --json emits JSONL, so the shared scanner handles it like the other engines.
    line = json.dumps({"type": "item.completed", "text": f"opened {PR_URL}"})
    assert CodexEngine().parse_pr_url(line) == PR_URL


def test_codex_required_env_returns_present_subset(monkeypatch):
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-fake")
    assert CodexEngine().required_env() == ["CODEX_API_KEY"]


def test_codex_required_env_both_keys_present(monkeypatch):
    # The shared OPENAI_API_KEY makes the both-set case the non-obvious one: both are returned,
    # in declaration order (CODEX_API_KEY first).
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert CodexEngine().required_env() == ["CODEX_API_KEY", "OPENAI_API_KEY"]


def test_codex_required_env_accepts_openai_key(monkeypatch):
    # OPENAI_API_KEY also authenticates codex (it doubles as the codex key).
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert CodexEngine().required_env() == ["OPENAI_API_KEY"]


def test_codex_required_env_empty_when_none_set(monkeypatch):
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    assert CodexEngine().required_env() == []


def test_codex_cred_hint_names_its_vars():
    hint = CodexEngine().cred_hint()
    assert "CODEX_API_KEY" in hint
    assert "OPENAI_API_KEY" in hint
    # codex must NOT advertise the other engines' creds.
    assert "ANTHROPIC_API_KEY" not in hint
    assert CLAUDE_TOKEN_VAR not in hint


# ---------------------------------------------------------------------------
# distill_line: per-engine distilled progress renderers
# ---------------------------------------------------------------------------


def test_base_engine_distill_line_returns_none():
    """Base Engine.distill_line always returns None (no distillation)."""
    assert Engine().distill_line("anything") is None
    assert Engine().distill_line('{"type":"result"}') is None


def test_distill_line_non_json_returns_none():
    for engine in (PiEngine(), ClaudeEngine(), CodexEngine()):
        assert engine.distill_line("not json at all") is None
        assert engine.distill_line("") is None
        assert engine.distill_line("   ") is None


def test_distill_line_unknown_event_returns_none():
    line = json.dumps({"type": "usage", "tokens": 42})
    for engine in (PiEngine(), ClaudeEngine(), CodexEngine()):
        assert engine.distill_line(line) is None


# --- ClaudeEngine distill_line ---


def test_claude_distill_line_edit_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/app.py"}}]
        },
    }
    result = ClaudeEngine().distill_line(json.dumps(event))
    assert result == "franky: editing src/app.py"


def test_claude_distill_line_bash_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "make test\nextra"}}
            ]
        },
    }
    result = ClaudeEngine().distill_line(json.dumps(event))
    assert result == "franky: running: make test"


def test_claude_distill_line_write_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "out.txt"}}]
        },
    }
    result = ClaudeEngine().distill_line(json.dumps(event))
    assert result == "franky: writing out.txt"


def test_claude_distill_line_result_event():
    event = {"type": "result"}
    assert ClaudeEngine().distill_line(json.dumps(event)) == "franky: agent complete"


def test_claude_distill_line_unknown_tool_uses_name():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "CustomTool", "input": {}}]},
    }
    result = ClaudeEngine().distill_line(json.dumps(event))
    assert result == "franky: customtool"


def test_claude_distill_line_assistant_text_no_tool_use():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I will now edit the file."}]},
    }
    assert ClaudeEngine().distill_line(json.dumps(event)) is None


def test_claude_distill_line_no_message_key():
    event = {"type": "assistant"}
    assert ClaudeEngine().distill_line(json.dumps(event)) is None


# --- PiEngine distill_line ---


def test_pi_distill_line_top_level_tool_use():
    event = {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}
    result = PiEngine().distill_line(json.dumps(event))
    assert result == "franky: running: pytest -q"


def test_pi_distill_line_message_with_tool_use():
    event = {
        "type": "message",
        "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "main.py"}}],
    }
    result = PiEngine().distill_line(json.dumps(event))
    assert result == "franky: editing main.py"


def test_pi_distill_line_done_event():
    event = {"type": "done", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    assert PiEngine().distill_line(json.dumps(event)) == "franky: agent complete"


def test_pi_distill_line_message_no_tool_use():
    event = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert PiEngine().distill_line(json.dumps(event)) is None


# --- CodexEngine distill_line ---


def test_codex_distill_line_exec_action():
    event = {
        "type": "action",
        "action": {"type": "exec", "command": {"cmd": "git status\nmore"}},
    }
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == "franky: running: git status"


def test_codex_distill_line_file_write_action():
    event = {"type": "action", "action": {"type": "file_write", "path": "src/util.py"}}
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == "franky: writing src/util.py"


def test_codex_distill_line_file_read_action():
    event = {"type": "action", "action": {"type": "file_read", "path": "README.md"}}
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == "franky: reading README.md"


def test_codex_distill_line_result_event():
    event = {"type": "result", "output": {"type": "success"}}
    assert CodexEngine().distill_line(json.dumps(event)) == "franky: agent complete"


def test_codex_distill_line_unknown_action_type():
    event = {"type": "action", "action": {"type": "something_new"}}
    assert CodexEngine().distill_line(json.dumps(event)) is None


# --- supports_steering (issue #72, mid-run steering via `franky job attach`) ---


def test_base_engine_defaults_to_no_steering():
    assert Engine().supports_steering is False


def test_pi_claude_codex_all_support_steering():
    assert PiEngine().supports_steering is True
    assert ClaudeEngine().supports_steering is True
    assert CodexEngine().supports_steering is True
