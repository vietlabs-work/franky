import json

import pytest

from franky.engine import (
    CLAUDE_TOKEN_VAR,
    CODEX_AUTH_VOLUME,
    CODEX_PROVIDER_VARS,
    DEFAULT_ENGINE,
    ENGINES,
    FRANKY_CODEX_AUTH_VOLUME_VAR,
    OPENCODE_PROVIDERS,
    PI_PROVIDER_VARS,
    PR_URL_RE,
    ClaudeEngine,
    CodexEngine,
    Engine,
    OpenCodeEngine,
    PiEngine,
    _fallback_pr_url,
    codex_auth_volume,
    opencode_provider,
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
        "--ignore-user-config",
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
    # OPENAI_API_KEY remains a pi credential but is not a direct codex exec credential.
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert CodexEngine().required_env() == ["CODEX_API_KEY"]


def test_codex_required_env_ignores_openai_key(monkeypatch):
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert CodexEngine().required_env() == []


def test_codex_required_env_empty_when_none_set(monkeypatch):
    for v in CODEX_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    assert CodexEngine().required_env() == []


def test_codex_cred_hint_names_its_vars():
    hint = CodexEngine().cred_hint()
    assert "CODEX_API_KEY" in hint
    assert "OPENAI_API_KEY" not in hint
    # codex must NOT advertise the other engines' creds.
    assert "ANTHROPIC_API_KEY" not in hint
    assert CLAUDE_TOKEN_VAR not in hint


def test_codex_exec_ignores_persisted_user_config():
    argv = CodexEngine().inner_argv("do it", None)
    assert "--ignore-user-config" in argv


def test_codex_subscription_uses_chatgpt_and_refresh_hosts_only():
    hosts = CodexEngine().provider_hosts({"FRANKY_CODEX_SUBSCRIPTION": "1"})
    assert hosts == ["chatgpt.com", "auth.openai.com"]


# --- opencode engine --------------------------------------------------------


def test_opencode_inner_argv_requires_explicit_model():
    assert OpenCodeEngine().inner_argv("do it", "openrouter/anthropic/claude-x") == [
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
        "--pure",
        "--model",
        "openrouter/anthropic/claude-x",
        "do it",
    ]


def test_opencode_registered_and_resolvable():
    assert ENGINES.get("opencode") is OpenCodeEngine
    assert isinstance(resolve_engine("opencode", {}), OpenCodeEngine)
    assert isinstance(resolve_engine(None, {"FRANKY_ENGINE": "opencode"}), OpenCodeEngine)


def test_opencode_provider_selects_supported_prefix():
    assert OPENCODE_PROVIDERS == {
        "moonshotai": ("MOONSHOT_API_KEY", "api.moonshot.ai"),
        "openrouter": ("OPENROUTER_API_KEY", "openrouter.ai"),
    }
    assert opencode_provider("moonshotai/kimi-k3") == (
        "MOONSHOT_API_KEY",
        "api.moonshot.ai",
    )
    assert opencode_provider("openrouter/moonshotai/kimi-k3") == (
        "OPENROUTER_API_KEY",
        "openrouter.ai",
    )
    assert opencode_provider("other/model") is None


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "moonshotai",
        "moonshotai/",
        "moonshotai//kimi-k3",
        "moonshotai/kimi k3",
        "moonshotai/not-kimi-k3",
        "moonshotai/kimi-k3/extra",
    ],
)
def test_opencode_provider_rejects_unsupported_or_malformed_model(model):
    assert opencode_provider(model) is None


@pytest.mark.parametrize(
    ("model", "credential", "host"),
    [
        ("moonshotai/kimi-k3", "MOONSHOT_API_KEY", "api.moonshot.ai"),
        ("openrouter/moonshotai/kimi-k3", "OPENROUTER_API_KEY", "openrouter.ai"),
    ],
)
def test_opencode_auth_and_provider_follow_model(model, credential, host):
    engine = OpenCodeEngine()
    env = {"OPENROUTER_API_KEY": "or", "MOONSHOT_API_KEY": "moon"}
    assert engine.required_env(env, model) == [credential]
    assert engine.provider_hosts(env, model) == [host]
    assert "OPENROUTER_API_KEY" in engine.cred_hint()
    assert "MOONSHOT_API_KEY" in engine.cred_hint()


def test_opencode_parse_pr_url_is_repo_scoped():
    output = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "state": {
                    "output": "https://github.com/attacker/evil/pull/1 "
                    "https://github.com/octocat/hello/pull/7"
                }
            },
        }
    )
    assert OpenCodeEngine().parse_pr_url(output, "octocat/hello") == (
        "https://github.com/octocat/hello/pull/7"
    )


def test_opencode_does_not_claim_steering_support():
    assert OpenCodeEngine().supports_steering is False


@pytest.mark.parametrize(
    ("tool", "input_data", "expected"),
    [
        ("read", {"filePath": "src/app.py"}, "franky: reading src/app.py"),
        ("write", {"filePath": "out.txt"}, "franky: writing out.txt"),
        ("edit", {"filePath": "src/app.py"}, "franky: editing src/app.py"),
        ("bash", {"command": "pytest -q\nignored"}, "franky: running: pytest -q"),
        ("search", {"pattern": "needle"}, "franky: searching: needle"),
    ],
)
def test_opencode_distills_tool_use(tool, input_data, expected):
    event = {
        "type": "tool_use",
        "part": {"tool": tool, "state": {"input": input_data}},
    }
    assert OpenCodeEngine().distill_line(json.dumps(event)) == expected


def test_opencode_distilled_tool_detail_is_bounded():
    event = {
        "type": "tool_use",
        "part": {"tool": "read", "state": {"input": {"filePath": "x" * 200}}},
    }
    result = OpenCodeEngine().distill_line(json.dumps(event))
    assert result == f"franky: reading {'x' * 60}"


def test_opencode_unknown_tool_name_is_bounded():
    event = {
        "type": "tool_use",
        "part": {"tool": "X" * 200, "state": {"input": {}}},
    }
    assert OpenCodeEngine().distill_line(json.dumps(event)) == f"franky: {'x' * 60}"


def test_opencode_distills_step_finish_and_error_without_payload():
    engine = OpenCodeEngine()
    assert engine.distill_line(json.dumps({"type": "step_finish", "part": {}})) == (
        "franky: step complete"
    )
    error = engine.distill_line(
        json.dumps({"type": "error", "error": "secret failure payload", "data": {"key": "x"}})
    )
    assert error == "franky: agent error"
    assert "secret" not in error


@pytest.mark.parametrize(
    "event",
    [
        {"type": "tool_use"},
        {"type": "tool_use", "part": []},
        {"type": "tool_use", "part": {"state": []}},
        {"type": "tool_use", "part": {"tool": "read", "state": {"input": []}}},
        {"type": "text", "part": {"text": "unbounded prose"}},
    ],
)
def test_opencode_suppresses_malformed_and_prose_events(event):
    assert OpenCodeEngine().distill_line(json.dumps(event)) is None


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


@pytest.mark.parametrize("name", [None, ["not-a-string"]])
def test_distillers_non_string_tool_names_use_generic_summary(name):
    cases = [
        (PiEngine(), {"type": "tool_use", "name": name, "input": {}}),
        (
            ClaudeEngine(),
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
            },
        ),
        (
            OpenCodeEngine(),
            {"type": "tool_use", "part": {"tool": name, "state": {"input": {}}}},
        ),
    ]
    for engine, event in cases:
        assert engine.distill_line(json.dumps(event)) == "franky: tool call"


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


def test_codex_distill_line_command_started():
    event = {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "git status\nmore",
            "status": "in_progress",
        },
    }
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == "franky: running: git status"


def test_codex_distill_line_file_change_completed():
    event = {
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "file_change",
            "changes": [{"path": "src/util.py", "kind": "update"}],
            "status": "completed",
        },
    }
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == "franky: writing src/util.py"


def test_codex_distill_line_malformed_file_changes_returns_none():
    event = {
        "type": "item.completed",
        "item": {"type": "file_change", "changes": 42},
    }
    assert CodexEngine().distill_line(json.dumps(event)) is None


def test_codex_distill_line_turn_completed():
    event = {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}
    assert CodexEngine().distill_line(json.dumps(event)) == "franky: agent complete"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "turn.failed", "error": {"message": "secret detail"}}, "franky: agent failed"),
        ({"type": "error", "message": "secret detail"}, "franky: agent error"),
    ],
)
def test_codex_distill_line_failure_hides_payload(event, expected):
    result = CodexEngine().distill_line(json.dumps(event))
    assert result == expected
    assert "secret detail" not in result


# --- supports_steering (issue #72, mid-run steering via `franky job attach`) ---


def test_base_engine_defaults_to_no_steering():
    assert Engine().supports_steering is False


def test_pi_claude_codex_all_support_steering():
    assert PiEngine().supports_steering is True
    assert ClaudeEngine().supports_steering is True
    assert CodexEngine().supports_steering is True


# --- codex_auth_volume (per-instance override, FRANKY_CODEX_AUTH_VOLUME) ---


def test_codex_auth_volume_unset_returns_default():
    assert codex_auth_volume({}) == CODEX_AUTH_VOLUME


def test_codex_auth_volume_accepts_valid_override():
    assert codex_auth_volume({FRANKY_CODEX_AUTH_VOLUME_VAR: "franky-team-codex-auth"}) == (
        "franky-team-codex-auth"
    )


def test_codex_auth_volume_rejects_empty_string():
    with pytest.raises(ValueError, match=FRANKY_CODEX_AUTH_VOLUME_VAR):
        codex_auth_volume({FRANKY_CODEX_AUTH_VOLUME_VAR: ""})


def test_codex_auth_volume_rejects_trailing_newline():
    with pytest.raises(ValueError, match=FRANKY_CODEX_AUTH_VOLUME_VAR):
        codex_auth_volume({FRANKY_CODEX_AUTH_VOLUME_VAR: "myvol\n"})
