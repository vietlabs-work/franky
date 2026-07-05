"""Tests for `franky gh` - the host-side gh passthrough (issue #62).

No real `gh`, no network, no live creds: the subprocess runner is a fake that records the
argv + env it was handed and returns a canned (returncode, stdout, stderr). The load-bearing
invariants under test are (1) the token reaches gh via the ENV, never the argv, and (2) every
printed byte is redacted.
"""

import json
import subprocess

import franky.cli as cli
import franky.github as github
from click.testing import CliRunner

TOKEN = "gho_supersecrettokenvalue123"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(record, *, returncode=0, stdout="", stderr=""):
    """A subprocess.run stand-in that records (argv, env) and returns a canned FakeProc."""

    def runner(argv, **kwargs):
        record["argv"] = argv
        record["env"] = kwargs.get("env")
        record["kwargs"] = kwargs
        return FakeProc(returncode, stdout, stderr)

    return runner


# ---------------------------------------------------------------------------
# run_gh unit behavior
# ---------------------------------------------------------------------------


def test_run_gh_prefixes_gh_and_forwards_args():
    rec = {}
    code, out, err = github.run_gh(
        ("pr", "list", "--repo", "o/r"), {"GH_TOKEN": TOKEN}, runner=_fake_runner(rec, stdout="ok")
    )
    assert rec["argv"] == ["gh", "pr", "list", "--repo", "o/r"]
    assert (code, out, err) == (0, "ok", "")


def test_run_gh_passes_token_via_env_never_argv():
    rec = {}
    github.run_gh(
        ("api", "/user"), {"GH_TOKEN": TOKEN, "PATH": "/usr/bin"}, runner=_fake_runner(rec)
    )
    # Token is in the child env...
    assert rec["env"]["GH_TOKEN"] == TOKEN
    # ...and NEVER on the argv (would be visible in `ps`).
    assert not any(TOKEN in part for part in rec["argv"])


def test_run_gh_captures_output():
    rec = {}
    github.run_gh(("x",), {}, runner=_fake_runner(rec, returncode=3, stdout="o", stderr="e"))
    assert rec["kwargs"]["capture_output"] is True
    assert rec["kwargs"]["text"] is True


def test_run_gh_forwards_timeout():
    rec = {}
    github.run_gh(("x",), {}, runner=_fake_runner(rec), timeout=30)
    assert rec["kwargs"]["timeout"] == 30


# ---------------------------------------------------------------------------
# _resolve_gh_timeout policy
# ---------------------------------------------------------------------------


def test_resolve_gh_timeout():
    assert cli._resolve_gh_timeout({}) == cli._GH_DEFAULT_TIMEOUT  # unset -> default
    assert cli._resolve_gh_timeout({"FRANKY_GH_TIMEOUT": "45"}) == 45.0
    assert cli._resolve_gh_timeout({"FRANKY_GH_TIMEOUT": "0"}) is None  # 0 -> no cap
    assert cli._resolve_gh_timeout({"FRANKY_GH_TIMEOUT": "-5"}) is None  # negative -> no cap
    # A typo falls back to the default rather than silently removing the watchdog.
    assert cli._resolve_gh_timeout({"FRANKY_GH_TIMEOUT": "garbage"}) == cli._GH_DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# franky gh command
# ---------------------------------------------------------------------------


def _invoke(monkeypatch, args, *, env, runner):
    monkeypatch.setattr(cli.os, "environ", dict(env))
    monkeypatch.setattr(cli, "run_gh", lambda a, e, **k: runner(a, e))
    # Neutralize the config-file merge so tests are hermetic (no ~/.franky/config read).
    monkeypatch.setattr(cli, "load_config_file", lambda e: None)
    return CliRunner().invoke(cli.main, ["gh", *args])


def test_gh_missing_token_exits_5(monkeypatch):
    res = _invoke(monkeypatch, ["pr", "list"], env={}, runner=lambda a, e: (0, "", ""))
    assert res.exit_code == 5
    assert "GH_TOKEN" in res.stderr


def test_gh_passes_exit_code_through(monkeypatch):
    res = _invoke(
        monkeypatch,
        ["pr", "view", "9"],
        env={"GH_TOKEN": TOKEN},
        runner=lambda a, e: (2, "", "boom"),
    )
    assert res.exit_code == 2


def test_gh_redacts_token_in_output(monkeypatch):
    leaky = f"authenticated as {TOKEN}"
    res = _invoke(
        monkeypatch, ["api", "/user"], env={"GH_TOKEN": TOKEN}, runner=lambda a, e: (0, leaky, "")
    )
    assert res.exit_code == 0
    assert TOKEN not in res.output
    assert TOKEN not in res.stdout
    assert "***REDACTED***" in res.stdout


def test_gh_stdout_and_stderr_are_separated(monkeypatch):
    """gh stdout -> stdout (so `--json` output stays clean), gh stderr -> stderr."""
    res = _invoke(
        monkeypatch,
        ["pr", "list", "--json", "number"],
        env={"GH_TOKEN": TOKEN},
        runner=lambda a, e: (0, '[{"number":1}]', "a warning line\n"),
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == [{"number": 1}]  # stdout is pure JSON
    assert "a warning line" in res.stderr


def test_gh_forwards_unknown_options_to_gh(monkeypatch):
    """--json is gh's flag here, not Franky's: it must reach gh untouched."""
    seen = {}

    def runner(a, e):
        seen["args"] = a
        return (0, "[]", "")

    res = _invoke(
        monkeypatch,
        ["pr", "list", "--json", "number", "--limit", "5"],
        env={"GH_TOKEN": TOKEN},
        runner=runner,
    )
    assert res.exit_code == 0
    assert list(seen["args"]) == ["pr", "list", "--json", "number", "--limit", "5"]


def test_gh_not_installed_exits_6(monkeypatch):
    def boom(a, e):
        raise FileNotFoundError("gh")

    res = _invoke(monkeypatch, ["pr", "list"], env={"GH_TOKEN": TOKEN}, runner=boom)
    assert res.exit_code == 6
    assert "gh" in res.stderr and "install" in res.stderr.lower()


def test_gh_config_file_token_is_used(monkeypatch):
    """A token that only exists in ~/.franky/config (merged by load_config_file) works."""
    monkeypatch.setattr(cli.os, "environ", {})
    monkeypatch.setattr(cli, "run_gh", lambda a, e, **k: (0, "ok", ""))

    def fake_merge(env):
        env.setdefault("GH_TOKEN", TOKEN)

    monkeypatch.setattr(cli, "load_config_file", fake_merge)
    res = CliRunner().invoke(cli.main, ["gh", "pr", "list"])
    assert res.exit_code == 0
    assert res.stdout.strip() == "ok"


def test_gh_malformed_config_file_exits_3(monkeypatch):
    """A malformed ~/.franky/config surfaces as a clean ConfigError (exit 3), no traceback -
    mirrors the build/iterate config-file path."""
    monkeypatch.setattr(cli.os, "environ", {"GH_TOKEN": TOKEN})
    monkeypatch.setattr(cli, "run_gh", lambda a, e, **k: (0, "ok", ""))

    def boom(env):
        raise ValueError("bad toml at line 3")

    monkeypatch.setattr(cli, "load_config_file", boom)
    res = CliRunner().invoke(cli.main, ["gh", "pr", "list"])
    assert res.exit_code == 3
    assert "config file error" in res.stderr


def test_gh_timeout_exits_9(monkeypatch):
    """The subprocess watchdog: a TimeoutExpired maps to a clean exit 9 (not a traceback)."""

    def boom(a, e):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=120)

    res = _invoke(monkeypatch, ["run", "watch"], env={"GH_TOKEN": TOKEN}, runner=boom)
    assert res.exit_code == 9
    assert "FRANKY_GH_TIMEOUT" in res.stderr


def test_gh_empty_output_prints_nothing(monkeypatch):
    """Both streams empty -> nothing on stdout (no spurious blank line); exit code still passes."""
    res = _invoke(
        monkeypatch, ["pr", "close", "9"], env={"GH_TOKEN": TOKEN}, runner=lambda a, e: (0, "", "")
    )
    assert res.exit_code == 0
    assert res.stdout == ""


def test_gh_help_forwards_to_gh(monkeypatch):
    """add_help_option=False + ignore_unknown_options: `franky gh --help` must reach gh, not
    Click's own help (so the caller sees gh's help surface)."""
    seen = {}

    def runner(a, e):
        seen["args"] = a
        return (0, "gh help text", "")

    res = _invoke(monkeypatch, ["--help"], env={"GH_TOKEN": TOKEN}, runner=runner)
    assert res.exit_code == 0
    assert list(seen["args"]) == ["--help"]  # forwarded verbatim, Click did not intercept it
    assert "gh help text" in res.stdout
