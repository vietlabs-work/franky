"""Unit tests for the pure machine-contract module (franky/result.py)."""

from franky import result as result_mod
from franky.economics import Usage
from franky.result import (
    EXIT_AGENT,
    EXIT_AUTH,
    EXIT_CODES,
    EXIT_CONFIG,
    EXIT_DOCKER,
    EXIT_NETWORK,
    EXIT_SUCCESS,
    EXIT_TASK_REJECTED,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    AuthError,
    ConfigError,
    DockerError,
    FrankyError,
    NetworkError,
    TaskRejected,
    build_error,
    build_result,
)


def test_exit_code_values_are_stable():
    # These are a SemVer contract - pin them so a renumber is a deliberate, test-breaking act.
    assert EXIT_SUCCESS == 0
    assert EXIT_USAGE == 2
    assert EXIT_CONFIG == 3
    assert EXIT_TASK_REJECTED == 4
    assert EXIT_AUTH == 5
    assert EXIT_DOCKER == 6
    assert EXIT_AGENT == 7
    assert EXIT_NETWORK == 8
    assert EXIT_TIMEOUT == 9


def test_exit_codes_map_covers_every_exit_constant():
    # EXIT_CODES is the single source of truth for exit-code docs; every EXIT_* constant must
    # have a string meaning (discovered via reflection so a new code can't slip through).
    exit_values = [
        v for k, v in vars(result_mod).items() if k.startswith("EXIT_") and k != "EXIT_CODES"
    ]
    for code in exit_values:
        assert isinstance(code, int)
        assert code in EXIT_CODES
        assert isinstance(EXIT_CODES[code], str) and EXIT_CODES[code]


def test_franky_error_is_value_error():
    # Every existing `except ValueError` / pytest.raises(ValueError) must still catch these.
    assert issubclass(FrankyError, ValueError)
    assert isinstance(ConfigError("x"), ValueError)


def test_subclass_codes_and_kinds():
    assert ConfigError("x").code == EXIT_CONFIG
    assert ConfigError("x").kind == "config_error"
    assert TaskRejected("x").code == EXIT_TASK_REJECTED
    assert TaskRejected("x").kind == "task_rejected"
    assert AuthError("x").code == EXIT_AUTH
    assert AuthError("x").kind == "auth_error"
    assert DockerError("x").code == EXIT_DOCKER
    assert DockerError("x").kind == "docker_error"
    assert NetworkError("x").code == EXIT_NETWORK
    assert NetworkError("x").kind == "network_error"


def test_franky_error_message_preserved_and_hint_default():
    exc = ConfigError("the message")
    assert str(exc) == "the message"
    assert exc.hint == ""


def test_franky_error_explicit_overrides():
    exc = FrankyError("m", code=2, kind="interactive_input_required", hint="pass --yes")
    assert exc.code == 2
    assert exc.kind == "interactive_input_required"
    assert exc.hint == "pass --yes"


def test_build_result_pr_opened_shape():
    usage = Usage(input_tokens=100, output_tokens=50, cost_usd=0.001)
    r = build_result(
        status="pr_opened",
        pr_url="https://github.com/me/repo/pull/11",
        reason="PR opened",
        exit_code=0,
        usage=usage,
        duration=1.23456,
        log_path="tasks/x.log",
        engine="pi",
        repo="me/repo",
    )
    assert r["status"] == "pr_opened"
    assert r["pr_url"] == "https://github.com/me/repo/pull/11"
    assert r["branch"] is None
    assert r["exit_code"] == 0
    assert r["log_path"] == "tasks/x.log"
    assert r["engine"] == "pi"
    assert r["repo"] == "me/repo"
    assert r["economics"] == {
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.001,
        "duration_s": 1.235,
    }


def test_build_result_no_pr_and_unknown_economics():
    r = build_result(
        status="no_pr",
        pr_url=None,
        reason="agent produced no PR URL",
        exit_code=7,
        usage=Usage(),
        duration=2.0,
        log_path="tasks/y.log",
        engine="pi",
        repo="me/repo",
    )
    assert r["status"] == "no_pr"
    assert r["pr_url"] is None
    assert r["exit_code"] == 7
    assert r["economics"]["tokens_in"] is None
    assert r["economics"]["cost_usd"] is None
    assert r["economics"]["duration_s"] == 2.0


def test_build_result_omits_replay_of_when_none():
    # A plain build/iterate must emit the exact same keys as before replay support existed -
    # replay_of simply does not appear (mirrors the `attempts` byte-identical contract).
    r = build_result(
        status="pr_opened",
        pr_url="https://github.com/me/repo/pull/11",
        reason="PR opened",
        exit_code=0,
        usage=Usage(),
        duration=1.0,
        log_path="tasks/x.log",
        engine="pi",
        repo="me/repo",
    )
    assert "replay_of" not in r
    assert set(r.keys()) == {
        "status",
        "pr_url",
        "branch",
        "reason",
        "exit_code",
        "economics",
        "log_path",
        "engine",
        "repo",
        "job_id",
    }


def test_build_result_includes_replay_of_when_set():
    r = build_result(
        status="replay_complete",
        pr_url=None,
        reason="reproduce-only replay pass complete",
        exit_code=0,
        usage=Usage(),
        duration=1.0,
        log_path="tasks/x.log",
        engine="pi",
        repo="me/repo",
        job_id="newid01",
        replay_of="origid01",
    )
    assert r["replay_of"] == "origid01"
    assert r["status"] == "replay_complete"


def test_build_result_iterate_complete_uses_input_url():
    r = build_result(
        status="iterate_complete",
        pr_url="https://github.com/me/repo/pull/11",
        reason="iterate pass complete",
        exit_code=0,
        usage=Usage(),
        duration=0.0,
        log_path="tasks/z.log",
        engine="claude",
        repo="me/repo",
    )
    assert r["status"] == "iterate_complete"
    assert r["pr_url"] == "https://github.com/me/repo/pull/11"
    assert r["branch"] is None


def test_build_error_shape():
    e = build_error(3, "config_error", "bad config", "fix the file")
    assert e == {
        "error": {
            "code": 3,
            "kind": "config_error",
            "message": "bad config",
            "hint": "fix the file",
        }
    }


def test_build_error_default_hint():
    e = build_error(4, "task_rejected", "off allowlist")
    assert e["error"]["hint"] == ""
