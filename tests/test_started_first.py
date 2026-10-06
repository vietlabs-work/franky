"""The `started` event is the FIRST stderr line of every command (callers that track jobs read the job
id from line 1); Atlassian and profile notes print after it, exactly once."""

import importlib.metadata
import json

import pytest
from click.testing import CliRunner

import franky.atlassian as atlassian
import franky.cli as cli
import franky.jobs as jobs

from tests import test_cli as tc

PR_URL = tc.PR_URL
ON_NOTE = "Atlassian tools on"
SCOPE_NOTE = "WARNING granted scopes unknown"
PRIVATE = {"v": True}  # what review-pr's PR fetch reports
BROAD = {"v": False}  # whether the stubbed grant includes write scopes


def _claude(env):
    env = {k: v for k, v in env.items() if k != "OPENROUTER_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-fake"
    return env


def _gate(monkeypatch, case):
    """Stub the connection store, the privacy lookup and the token for one verdict."""
    stored = case != "not_connected"
    monkeypatch.setattr(cli.atlassian, "stored_secrets", lambda env: ["stored-x"] if stored else [])
    monkeypatch.setattr(cli, "repo_is_private", lambda *a, **k: case != "not_private")
    monkeypatch.setitem(PRIVATE, "v", case != "not_private")
    hints = {
        "expired": atlassian.HINT_REJECTED,
        "network": atlassian.HINT_NETWORK,
        "busy": atlassian.HINT_BUSY,
    }
    BROAD["v"] = False

    def token(env, echo=None, **k):
        echo(f"franky: {SCOPE_NOTE}")  # the note access_token itself would emit
        if case in hints:
            return atlassian.Token(hint=hints[case], reason=case)
        return atlassian.Token("tok", expires_at=3 * 3600, broad=BROAD["v"])

    monkeypatch.setattr(cli.atlassian, "access_token", token)


def _run(args, *extra):
    """Insert option flags before a review-pr `--`-separated PR URL."""
    if "TAIL" in args:
        i = args.index("TAIL")
        args = [*args[:i], *extra, "--", *args[i + 1 :]]
    else:
        args = [*args, *extra]
    return CliRunner().invoke(cli.main, args)


def _first_event(res):
    lines = res.stderr.splitlines()
    return json.loads(lines[0]), lines[1:]


def _command(name, monkeypatch, tmp_path):
    """Hermetic claude-engine invocation of one command; returns (args, expected command)."""
    if name == "build":
        env = {**_claude(tc._build_env()), "FRANKY_RUNS_DIR": str(tmp_path / "runs")}
        monkeypatch.setattr(cli.os, "environ", env)
        monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
        monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
        monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
        monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))
        return ["build", "do it", "--repo", "me/repo", "--engine", "claude"], "build"
    if name == "iterate":
        env = {**_claude(tc._iterate_env()), "FRANKY_RUNS_DIR": str(tmp_path / "runs")}
        monkeypatch.setattr(cli.os, "environ", env)
        monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
        monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
        monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, "pushed"))
        return ["iterate", PR_URL, "--engine", "claude"], "iterate"
    if name == "review-pr":
        tc._fix_review_nonce(monkeypatch)
        env = {**_claude(tc._review_env()), "FRANKY_RUNS_DIR": str(tmp_path / "runs")}
        tc._mc_review_setup(monkeypatch, env=env)

        def fetch(repo, number, e, **k):
            if k.get("meta_sink") is not None:
                k["meta_sink"].update(title="t", body="", head_ref="", private=PRIVATE["v"])
            return tc.LIVE_SHA

        monkeypatch.setattr(cli, "fetch_pr_head_sha", fetch)
        return ["review-pr", "--engine", "claude", "--no-publish", "TAIL", PR_URL], "review-pr"
    if name == "replay":
        env = tc._replay_env(monkeypatch, tmp_path, (0, "reproduced"))
        monkeypatch.setattr(cli.os, "environ", _claude(env))
        job_id = tc._write_replayable_run(cli.os.environ, tmp_path)
        return ["job", "replay", job_id, "--engine", "claude"], "replay"
    if name == "resume":
        env = tc._resume_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"))
        monkeypatch.setattr(cli.os, "environ", _claude(env))
        job_id = tc._write_resumable_run(cli.os.environ, tmp_path)
        return ["job", "resume", job_id, "--engine", "claude"], "resume"
    if name == "diagnose":
        diag = '{"root_cause": "x", "category": "other", "retryable": false, "confidence": "low"}'
        out = f"FRANKY_DIAG_n_BEGIN{diag}FRANKY_DIAG_n_END"
        env = tc._diag_setup(monkeypatch, tmp_path, (0, out), "n")
        monkeypatch.setattr(cli.os, "environ", _claude(env))
        job_id = tc._write_failed_run(cli.os.environ, tmp_path)
        return ["job", "diagnose", job_id, "--engine", "claude"], "diagnose"
    raise AssertionError(name)


COMMANDS = ["build", "iterate", "review-pr", "replay", "resume", "diagnose"]


CASES = ["on", "not_connected", "expired", "network", "busy", "not_private"]


@pytest.mark.parametrize("name", COMMANDS)
@pytest.mark.parametrize("case", CASES)
def test_started_is_first_and_carries_the_verdict(monkeypatch, tmp_path, name, case):
    args, command = _command(name, monkeypatch, tmp_path)
    _gate(monkeypatch, case)  # NO JIRA_* in env: the verdict does not depend on the classic vars
    res = _run(args, "--json")
    assert res.exit_code == 0, res.output
    event, rest = _first_event(res)
    assert event["event"] == "started" and event["command"] == command
    assert event["atlassian"] == case
    assert "atlassian_warning" not in event
    json.loads(res.stdout)  # stdout stays one result object
    assert rest == []  # no Atlassian prose under --json


@pytest.mark.parametrize("name", ["build", "review-pr"])
@pytest.mark.parametrize("case", CASES)
def test_json_run_stderr_is_the_event_line_only(monkeypatch, tmp_path, name, case):
    args, _ = _command(name, monkeypatch, tmp_path)
    _gate(monkeypatch, case)
    monkeypatch.setenv("JIRA_BASE_URL", "x")  # even with the classic vars, no prose
    res = _run(args, "--json")
    assert res.exit_code == 0, res.output
    lines = res.stderr.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "started"
    assert "Atlassian" not in res.stderr


@pytest.mark.parametrize("name", ["build", "review-pr"])
def test_broad_grant_is_an_event_field_not_a_line(monkeypatch, tmp_path, name):
    args, _ = _command(name, monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    BROAD["v"] = True
    res = _run(args, "--json")
    event, rest = _first_event(res)
    assert event["atlassian"] == "on" and event["atlassian_warning"] == "broad_scope"
    assert rest == []


def test_quiet_run_prints_no_atlassian_prose(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    res = _run(args, "--quiet")
    assert res.exit_code == 0, res.output
    assert "Atlassian" not in res.stderr and SCOPE_NOTE not in res.stderr


@pytest.mark.parametrize("name", COMMANDS)
def test_plain_output_prints_started_before_the_notes(monkeypatch, tmp_path, name):
    args, _ = _command(name, monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    res = _run(args)
    assert res.exit_code == 0, res.output
    lines = res.stderr.splitlines()
    first_note = next(i for i, line in enumerate(lines) if SCOPE_NOTE in line)
    mark = "diagnosing job" if name == "diagnose" else "started"
    assert any(mark in line for line in lines[:first_note])
    assert sum(ON_NOTE in line for line in lines) == 1


def test_verdict_is_absent_for_pi_frozen_and_no_repo(monkeypatch, tmp_path):
    _gate(monkeypatch, "on")
    # pi engine: the gate never runs
    env = {**tc._build_env(), "FRANKY_RUNS_DIR": str(tmp_path / "runs")}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))
    res = CliRunner().invoke(cli.main, ["build", "x", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    event, rest = _first_event(res)
    assert "atlassian" not in event and not any(ON_NOTE in line for line in rest)
    # frozen eval review: no tools, no verdict
    tc._frozen_setup(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _claude(cli.os.environ))
    res = CliRunner().invoke(
        cli.main, ["review-pr", "--engine", "claude", *tc.FROZEN, "--", PR_URL]
    )
    assert res.exit_code == 0, res.output
    event, _ = _first_event(res)
    assert event["event"] == "started" and "atlassian" not in event
    # no repo: the gate returns before any verdict
    cfg = cli.load_config("claude", _claude(tc._build_env()))
    with cli.main.make_context("main", ["version"]) as ctx, ctx:
        cli._enable_atlassian(cfg, None, [])
        assert cli._pre_start().atlassian is None


def test_non_announcing_exits_print_the_notes_once(monkeypatch, tmp_path):
    # plan succeeds without ever announcing a job
    tc._fix_plan_nonce(monkeypatch)
    env = {**_claude(tc._build_env()), "FRANKY_RUNS_DIR": str(tmp_path / "runs")}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    plan = {"fits_one_pr": True, "subtasks": [], "rationale": "small"}
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, tc._plan_block(plan)))
    _gate(monkeypatch, "on")
    res = CliRunner().invoke(cli.main, ["plan", "tiny", "--repo", "me/repo", "--engine", "claude"])
    assert res.exit_code == 0, res.output
    assert res.stderr.count(ON_NOTE) == 1 and res.stderr.count(SCOPE_NOTE) == 1


def test_already_open_build_prints_no_verdict_notes(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: PR_URL)
    _gate(monkeypatch, "on")
    res = CliRunner().invoke(cli.main, [*args, "--json"])
    assert json.loads(res.stdout)["status"] == "already_open"
    assert ON_NOTE not in res.stderr  # the short-circuit precedes the gate


def test_an_error_flushes_the_notes_once_before_the_message(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")

    def boom(*a, **k):
        raise cli.ConfigError("boom")

    monkeypatch.setattr(cli, "_load_profile_bundle", boom)
    res = CliRunner().invoke(cli.main, args)
    assert res.exit_code != 0
    lines = res.stderr.splitlines()
    assert sum(ON_NOTE in line for line in lines) == 1
    assert max(i for i, line in enumerate(lines) if ON_NOTE in line) < next(
        i for i, line in enumerate(lines) if "boom" in line
    )


def test_profile_warning_prints_after_started(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    scan = type("Scan", (), {"files": [], "root": "/x"})()
    spec = type(
        "Spec",
        (),
        {"setup_scans": {"claude": scan}, "mcp_domains": [], "all_files": lambda self: []},
    )()
    monkeypatch.setattr(cli, "load_profile", lambda path: spec)
    monkeypatch.setattr(cli, "validate_mcp_engine", lambda *a: None)
    monkeypatch.setattr(cli, "resolve_mcp_credentials", lambda *a: {})
    monkeypatch.setattr(cli, "build_setup_block", lambda spec: "")
    monkeypatch.setattr(cli, "claude_mcp_config_path", lambda spec: None)
    _gate(monkeypatch, "on")
    (tmp_path / "p.toml").write_text("")
    res = _run(args, "--json", "--profile", str(tmp_path / "p.toml"))
    event, rest = _first_event(res)
    assert event["event"] == "started"
    assert sum("matched no files" in line for line in rest) == 1
    assert event["atlassian"] == "on" and ON_NOTE not in res.stderr


def test_pending_notes_do_not_leak_between_invocations(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    first = _run(args)
    assert first.stderr.count(ON_NOTE) == 1
    _gate(monkeypatch, "not_connected")
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
    second = _run(args, "--json")
    event, rest = _first_event(second)
    assert event["atlassian"] == "not_connected"
    assert ON_NOTE not in second.stderr and SCOPE_NOTE not in second.stderr


def test_update_hint_prints_after_started(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "maybe_auto_update", lambda out=print, **k: out("franky: update 9.9"))
    res = CliRunner().invoke(cli.main, args)
    lines = res.stderr.splitlines()
    assert "started" in lines[0] and "franky: update 9.9" in lines[1:]
    assert jobs  # keep import used


def test_resume_fallback_message_prints_after_started(monkeypatch, tmp_path):
    args, _ = _command("resume", monkeypatch, tmp_path)
    rec_id = args[2]
    rec = jobs.read_record(rec_id, cli.os.environ)
    rec["threaded"] = True
    jobs.write_record(rec, cli.os.environ)
    monkeypatch.setattr(cli, "_resume_session", lambda *a, **k: (None, "no_session"))
    res = CliRunner().invoke(cli.main, args)
    lines = res.stderr.splitlines()
    assert "started" in lines[0]
    assert any("without its engine session" in line for line in lines[1:])


@pytest.mark.parametrize("name", ["replay", "resume"])
def test_already_open_replay_and_resume_make_no_privacy_or_token_call(monkeypatch, tmp_path, name):
    args, _ = _command(name, monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: PR_URL)

    def forbidden(*a, **k):
        raise AssertionError("the already_open short-circuit must precede the Atlassian gate")

    monkeypatch.setattr(cli, "repo_is_private", forbidden)
    monkeypatch.setattr(cli.atlassian, "access_token", forbidden)
    res = CliRunner().invoke(
        cli.main, [*args, "--json"] + (["--open-pr"] if name == "replay" else [])
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["status"] == "already_open"


def test_plan_first_shows_the_notes_before_the_plan_and_the_prompt(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    seen: list[str] = []

    def run(*a, **k):
        seen.append("planning")
        return (0, "THE-PLAN")

    monkeypatch.setattr(cli, "run_in_container", run)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def confirm(*a, **k):
        seen.append("prompt")
        return False

    monkeypatch.setattr(cli.click, "confirm", confirm)
    res = CliRunner().invoke(cli.main, [*args, "--plan-first"])
    assert res.exit_code == 0, res.output
    lines = res.stderr.splitlines()
    note = next(i for i, line in enumerate(lines) if SCOPE_NOTE in line)
    plan = next(i for i, line in enumerate(lines) if "THE-PLAN" in line)
    assert note < plan and seen == ["planning", "prompt"]
    assert res.stderr.count(ON_NOTE) == 1 and not any("started" in line for line in lines)


@pytest.mark.parametrize("exc", [RuntimeError("kaboom"), KeyboardInterrupt()])
def test_uncaught_exceptions_still_print_the_notes_once_first(monkeypatch, tmp_path, exc):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli, "_load_profile_bundle", boom)
    res = CliRunner().invoke(cli.main, args)
    assert res.exit_code != 0
    assert res.stderr.count(ON_NOTE) == 1 and res.stderr.count(SCOPE_NOTE) == 1
    assert isinstance(res.exception, (SystemExit, type(exc)))


def test_retry_json_announces_each_pass_without_prose(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (1, "no pr here"))
    res = _run(args, "--json", "--retry", "1")
    events = [json.loads(line) for line in res.stderr.splitlines()]
    assert events and events[0]["event"] == "started" and events[0]["atlassian"] == "on"
    # attempt 1, then the retry's diagnose pass: each announces once, no prose in between
    assert [e["command"] for e in events] == ["build", "diagnose"]
    assert all(e["atlassian"] == "on" and "atlassian_warning" not in e for e in events)
    assert "Atlassian" not in res.stderr


def test_version_skew_line_prints_after_started(monkeypatch, tmp_path):
    import franky

    args, _ = _command("build", monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli, "resolve_image", lambda *a, **k: ("franky", franky.franky_version())[0]
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    res = CliRunner().invoke(cli.main, args)
    lines = res.stderr.splitlines()
    assert "started" in lines[0]
    assert any("version skew" in line for line in lines[1:])


def test_a_malformed_store_is_not_connected_not_network(monkeypatch, tmp_path):
    args, _ = _command("build", monkeypatch, tmp_path)
    _gate(monkeypatch, "on")
    monkeypatch.setattr(
        cli.atlassian, "access_token", lambda env, **k: atlassian.Token(reason="missing")
    )
    event, rest = _first_event(_run(args, "--json"))
    assert event["atlassian"] == "not_connected" and rest == []
