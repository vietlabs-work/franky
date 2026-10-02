import json
import os
import stat
import subprocess
import tarfile
import io
from pathlib import Path

import pytest

import franky.cli as cli
import franky.jobs as jobs
from click.testing import CliRunner

from franky import __version__
from franky.reviewpr import RESOLVE_MUTATION, THREADS_QUERY, build_review_findings
from franky.schema import build_schema
from franky._install import Install

PR_URL = "https://github.com/me/repo/pull/11"


def test_help_exit_zero():
    res = CliRunner().invoke(cli.main, ["--help"])
    assert res.exit_code == 0


def test_apparmor_profile_prints_packaged_policy():
    res = CliRunner().invoke(cli.main, ["apparmor-profile"])

    assert res.exit_code == 0
    assert res.output == cli.TASK_APPARMOR.read_text()


def test_version_prints_version():
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.output


# ---------------------------------------------------------------------------
# Enriched version command
# ---------------------------------------------------------------------------


def _make_env(extra=None):
    """Minimal env that resolves engine cleanly (defaults to pi, no creds needed for version).

    Carries FRANKY_CONFIG_FILE from the real environment, where conftest's autouse
    _hermetic_config_file fixture points it at a non-existent tmp path. `version` merges the
    config file into its own copy of this dict, so without that key the command would read
    the developer's real ~/.franky/config and these assertions would follow whatever that
    file says.
    """
    env = {"FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    if extra:
        env.update(extra)
    return env


def test_version_text_contains_required_sections(monkeypatch):
    """Text output has: version line, provenance line, engine line, image line."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("dev checkout", "/fake/repo"))
    monkeypatch.setattr(cli.os, "environ", _make_env())
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0, res.output
    assert __version__ in res.output
    assert "provenance:" in res.output
    assert "/fake/repo" in res.output  # the provenance PATH is rendered, not just the kind
    assert "engine:" in res.output
    assert "image:" in res.output


def test_version_text_shows_host_binary_when_resolvable(monkeypatch):
    """When the host engine binary resolves, its version is shown on the engine line."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: "pi 9.9.9")
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("dev checkout", "/fake/repo"))
    monkeypatch.setattr(cli.os, "environ", _make_env())
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0, res.output
    assert "host binary: pi 9.9.9" in res.output


def test_version_json_has_required_keys(monkeypatch):
    """--json output is valid JSON with the pinned top-level keys."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env())
    res = CliRunner().invoke(cli.main, ["version", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert "franky" in data
    assert "provenance" in data
    assert "kind" in data["provenance"]
    assert "path" in data["provenance"]
    assert "engine" in data
    assert "name" in data["engine"]
    assert "resolved" in data["engine"]
    assert "host_binary_version" in data["engine"]
    assert "image" in data
    assert data["image"].endswith(f":{__version__}-pi")


def test_version_json_franky_image_override(monkeypatch):
    """FRANKY_IMAGE env var must flow through to info['image']."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env({"FRANKY_IMAGE": "local-tag"}))
    res = CliRunner().invoke(cli.main, ["version", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["image"] == "local-tag"


def test_version_bad_engine_exits_zero_and_unresolved(monkeypatch):
    """A bad FRANKY_ENGINE must not crash version - exit 0, engine.resolved false."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env({"FRANKY_ENGINE": "bogus"}))
    res = CliRunner().invoke(cli.main, ["version", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["engine"]["resolved"] is False
    assert "bogus" in data["engine"]["name"]
    assert data["image"].endswith(f":{__version__}")


def test_version_bad_engine_text_mentions_name_and_unresolved(monkeypatch):
    """Text mode also renders the unresolved hint for a bad engine."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env({"FRANKY_ENGINE": "bogus"}))
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0, res.output
    assert "bogus" in res.output
    assert "unresolved" in res.output


def test_version_host_binary_absent_no_crash(monkeypatch):
    """If the host binary is not found, host_binary_version is null and no crash."""

    def raising_runner(argv, **_kw):
        raise FileNotFoundError("pi: command not found")

    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    # Inject the raising runner directly into _version_info.
    info = cli._version_info(_make_env(), runner=raising_runner)
    assert info["engine"]["host_binary_version"] is None


def _write_config(tmp_path, body):
    """Write a [franky] config file and return its path."""
    path = tmp_path / "franky-config"
    path.write_text(body)
    return path


def test_version_engine_follows_the_config_file(monkeypatch, tmp_path):
    """FRANKY_ENGINE set only in ~/.franky/config must reach the reported engine AND image.

    Before this merge, `version` reported the default engine and a `-pi` image while
    build/iterate/review-pr all ran the engine the config file names.
    """
    path = _write_config(tmp_path, '[franky]\nFRANKY_ENGINE = "claude"\n')
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env({"FRANKY_CONFIG_FILE": str(path)}))

    res = CliRunner().invoke(cli.main, ["version", "--json"])

    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["engine"]["name"] == "claude"
    assert data["engine"]["resolved"] is True
    assert data["image"].endswith("-claude")


def test_version_process_env_beats_the_config_file(monkeypatch, tmp_path):
    """Precedence is unchanged: an exported FRANKY_ENGINE still wins over the file."""
    path = _write_config(tmp_path, '[franky]\nFRANKY_ENGINE = "claude"\n')
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(
        cli.os,
        "environ",
        _make_env({"FRANKY_CONFIG_FILE": str(path), "FRANKY_ENGINE": "codex"}),
    )

    res = CliRunner().invoke(cli.main, ["version", "--json"])

    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["engine"]["name"] == "codex"


def test_version_survives_a_malformed_config_file(monkeypatch, tmp_path):
    """`version` is what an operator runs when something is wrong, so a broken config file
    degrades to the pre-merge answer instead of a traceback - and says so on stderr, because
    a silent fallback would report an engine that no real run resolves. stdout stays a
    single JSON value for the machines reading it."""
    path = _write_config(tmp_path, "this is not TOML {[")
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", _make_env({"FRANKY_CONFIG_FILE": str(path)}))

    res = CliRunner().invoke(cli.main, ["version", "--json"])

    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["engine"]["name"] == "pi"
    assert "ignoring the config file" in res.stderr


def test_version_does_not_mutate_the_process_environment(monkeypatch, tmp_path):
    """The merge lands on a copy. `version` reports; it never changes what a later call sees."""
    path = _write_config(tmp_path, '[franky]\nFRANKY_ENGINE = "claude"\n')
    env = _make_env({"FRANKY_CONFIG_FILE": str(path)})
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("pip", "/usr/bin/python3"))
    monkeypatch.setattr(cli.os, "environ", env)

    CliRunner().invoke(cli.main, ["version", "--json"])

    assert "FRANKY_ENGINE" not in env


def test_engine_binary_version_filenotfound():
    """_engine_binary_version returns None when the binary is not on PATH."""

    def raising_runner(argv, **_kw):
        raise FileNotFoundError("no such binary")

    assert cli._engine_binary_version("pi", runner=raising_runner) is None


def test_engine_binary_version_returncode_nonzero():
    """_engine_binary_version returns None when the binary exits non-zero."""

    def bad_runner(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="error")

    assert cli._engine_binary_version("pi", runner=bad_runner) is None


def test_engine_binary_version_returns_first_line():
    """_engine_binary_version returns the first non-empty stripped line."""

    def good_runner(argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, stdout="1.2.3\nignored\n", stderr="")

    assert cli._engine_binary_version("pi", runner=good_runner) == "1.2.3"


def test_version_dev_checkout_text_shows_dev_checkout(monkeypatch):
    """Text output says 'dev checkout' when detect_install returns that kind."""
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    monkeypatch.setattr(cli, "detect_install", lambda **_kw: Install("dev checkout", "/my/repo"))
    monkeypatch.setattr(cli.os, "environ", _make_env())
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0, res.output
    assert "dev checkout" in res.output


def test_build_help_shows_engine():
    res = CliRunner().invoke(cli.main, ["build", "--help"])
    assert res.exit_code == 0
    assert "--engine" in res.output


def test_build_reaches_pr_url(monkeypatch):
    # hermetic: valid fake env, image present, container mocked to return a PR url
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():  # keep the tasks/ log out of the repo
        res = runner.invoke(cli.main, ["build", "add a --json flag", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert PR_URL in res.output


def test_build_invokes_auto_update_hint(monkeypatch):
    # build must call maybe_auto_update at the top; failures there must never break the build.
    seen = {"called": False}
    monkeypatch.setattr(cli, "maybe_auto_update", lambda *a, **k: seen.update(called=True))
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert seen["called"] is True


def test_build_engine_flag_selects_engine(monkeypatch):
    # --engine claude must reach config + select ClaudeEngine end-to-end through the CLI.
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))

    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["engine"] = cfg.engine.name
        seen["argv0"] = inner_argv[0]
        seen["image"] = k["image"]
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--engine", "claude"])
    assert res.exit_code == 0, res.output
    assert seen["engine"] == "claude"
    assert seen["argv0"] == "claude"
    assert seen["image"].endswith(f":{__version__}-claude")


def test_build_scopes_pr_url_to_target_repo(monkeypatch):
    # A hostile PR URL for another repo in the output must NOT be reported; only the
    # target repo's PR URL is.
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    hostile = "https://github.com/attacker/repo/pull/1"
    good = "https://github.com/me/repo/pull/7"
    monkeypatch.setattr(
        cli,
        "run_in_container",
        lambda *a, **k: (0, f"saw {good} then {hostile}"),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert good in res.output
    assert "attacker" not in res.output


def _plan_first_env():
    return {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def test_build_plan_first_help_shows_flag():
    res = CliRunner().invoke(cli.main, ["build", "--help"])
    assert res.exit_code == 0
    assert "--plan-first" in res.output


def test_build_plan_first_declined_does_not_execute(monkeypatch):
    # Declining the gate: the planning pass runs ONCE, the plan is shown, and no build pass
    # or PR follows.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return 0, "PLAN: step 1, step 2"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first"], input="n\n"
        )
    assert res.exit_code == 0, res.output
    assert calls["n"] == 1  # only the planning pass ran
    assert "PLAN: step 1, step 2" in res.output  # the plan was surfaced
    assert "aborted" in res.output
    assert PR_URL not in res.output


def test_build_plan_first_approved_executes_and_prs(monkeypatch):
    # Approving the gate: planning pass THEN build pass; the build pass's PR URL is reported.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    outputs = ["PLAN: do the thing", f"opened {PR_URL}"]

    def fake_run(*a, **k):
        return 0, outputs.pop(0)

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first"], input="y\n"
        )
    assert res.exit_code == 0, res.output
    assert not outputs  # both passes ran
    assert PR_URL in res.output


def test_build_plan_first_non_interactive_fails_closed(monkeypatch):
    # No TTY to answer the gate (CI / piped) must fail FAST: exit 2 BEFORE the planning pass
    # runs at all (never-hang), and certainly no build pass or PR.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return 0, "PLAN: step 1"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first"])
    assert res.exit_code == 2  # fail fast, never-hang
    assert calls["n"] == 0  # no container run at all
    assert PR_URL not in res.output


def test_build_plan_first_planning_failure_aborts_before_gate(monkeypatch):
    # A planning pass that errors must NOT proceed to a build pass.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return 1, "boom"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first"], input="y\n"
        )
    assert res.exit_code != 0
    assert calls["n"] == 1  # never reached the build pass
    assert "planning pass" in res.output


def test_build_plan_first_timeout_maps_to_exit_9(monkeypatch):
    # A timed-out planning pass (124 sentinel) maps to the dedicated timeout contract
    # (EXIT_TIMEOUT=9), not the generic agent_error (7), and never reaches the build pass.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return cli.CONTAINER_TIMEOUT_CODE, "franky: container timed out after 1s"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main,
            ["build", "do it", "--repo", "me/repo", "--plan-first", "--max-duration", "1"],
            input="y\n",
        )
    assert res.exit_code == 9
    assert calls["n"] == 1  # never reached the build pass


def test_build_off_allowlist_clean_error(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, "x"))

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "stranger/repo"])
    assert res.exit_code != 0
    assert "allowlist" in res.output


def test_build_missing_creds_clean_error_no_secret_leak(monkeypatch):
    env = {"FRANKY_ALLOWED_REPOS": "me/repo", "GH_TOKEN": "ghp_fake"}  # no provider key
    monkeypatch.setattr(cli.os, "environ", env)

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code != 0
    assert "ghp_fake" not in res.output  # no secret value in the error path


def test_build_image_auth_error_clean_message(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "ghcr.io/vietlabs-work/franky:0.1.0")
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "auth"))

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code != 0
    assert "docker login ghcr.io" in res.output


def test_build_image_pull_failed_clean_message(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "ghcr.io/vietlabs-work/franky:0.1.0")
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "pull-failed"))

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code != 0
    assert "could not be pulled" in res.output


def test_build_image_no_docker_clean_message(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "ghcr.io/vietlabs-work/franky:0.1.0")
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "no-docker"))

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code != 0
    assert "docker is not available" in res.output


# ---------------------------------------------------------------------------
# _write_log (redacted transcript location, issue: tasks/ moved under FRANKY_RUNS_DIR)
# ---------------------------------------------------------------------------


def test_write_log_lands_under_runs_dir_tasks_with_run_id_in_name(tmp_path):
    runs_dir = tmp_path / "caller-runs"
    env = {"FRANKY_RUNS_DIR": str(runs_dir)}
    path = cli._write_log("hello", [], run_id="abc123def456", env=env)
    assert path.is_absolute()
    assert path.parent == (runs_dir / "tasks").resolve()
    assert path.name.endswith("-abc123def456.log")
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_write_log_same_second_distinct_run_ids_never_clobber(tmp_path, monkeypatch):
    import datetime as dt_mod

    frozen = dt_mod.datetime(2026, 1, 1, 12, 0, 0)

    class _FrozenDateTime(dt_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(cli, "datetime", _FrozenDateTime)
    env = {"FRANKY_RUNS_DIR": str(tmp_path / "caller-runs")}
    p1 = cli._write_log("one", [], run_id="run1", env=env)
    p2 = cli._write_log("two", [], run_id="run2", env=env)
    assert p1 != p2
    assert p1.read_text(encoding="utf-8") == "one\n"
    assert p2.read_text(encoding="utf-8") == "two\n"


def test_write_log_without_run_id_uses_microsecond_suffix(tmp_path):
    # The `plan`/plan-first passes have no job_id yet - the microsecond fallback still keeps
    # same-second logs from two such passes distinct.
    env = {"FRANKY_RUNS_DIR": str(tmp_path / "caller-runs")}
    path = cli._write_log("hello", [], env=env)
    suffix = path.stem.rsplit("-", 1)[-1]
    assert suffix.isdigit() and len(suffix) == 6


def test_write_log_creates_dirs_0700_and_file_0600(tmp_path):
    runs_dir = tmp_path / "caller-runs"
    env = {"FRANKY_RUNS_DIR": str(runs_dir)}
    path = cli._write_log("hello", [], run_id="abc123", env=env)
    assert stat.S_IMODE(runs_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((runs_dir / "tasks").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_log_unwritable_runs_dir_raises_config_error(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    runs_dir = tmp_path / "locked"
    runs_dir.mkdir(mode=0o500)
    env = {"FRANKY_RUNS_DIR": str(runs_dir)}
    with pytest.raises(cli.ConfigError, match=str(runs_dir / "tasks")):
        cli._write_log("hello", [], env=env)


def test_build_honors_config_file_runs_dir_for_transcript(tmp_path, monkeypatch):
    # A config-file FRANKY_RUNS_DIR (not in the process env) must govern the transcript
    # location exactly like it governs the job registry - the process-env snapshot taken
    # before load_config_file injects it must never be what _write_log sees.
    cfg_path = tmp_path / "config"
    runs_dir = tmp_path / "from-config-file"
    cli.write_config_file(cfg_path, {"FRANKY_RUNS_DIR": str(runs_dir)})
    env = _build_env()
    env["FRANKY_CONFIG_FILE"] = str(cfg_path)
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    logs = list((runs_dir / "tasks").glob("*.log"))
    assert logs, "transcript not written under the config-file FRANKY_RUNS_DIR"


# ---------------------------------------------------------------------------
# update command (wiring only - force_update logic is covered in test_update_check)
# ---------------------------------------------------------------------------


def test_update_help_shows_force():
    res = CliRunner().invoke(cli.main, ["update", "--help"])
    assert res.exit_code == 0
    assert "--force" in res.output


def test_update_propagates_exit_code_and_force_flag(monkeypatch):
    seen = {}

    def fake_force_update(*, force, out):
        seen["force"] = force
        out("franky: updated to v9.9.9")
        return 0

    monkeypatch.setattr(cli, "force_update", fake_force_update)
    res = CliRunner().invoke(cli.main, ["update", "--force"])
    assert res.exit_code == 0, res.output
    assert seen["force"] is True
    assert "updated to v9.9.9" in res.output


def test_update_nonzero_exit_propagates(monkeypatch):
    monkeypatch.setattr(cli, "force_update", lambda *, force, out: 1)
    res = CliRunner().invoke(cli.main, ["update"])
    assert res.exit_code == 1


# ---------------------------------------------------------------------------
# JIRA input via CLI
# ---------------------------------------------------------------------------


def test_build_jira_reaches_pr_url(monkeypatch):
    """franky build jira FOO-123 --repo me/repo fetches the issue and reaches the PR URL."""
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_EMAIL": "user@example.com",
        "JIRA_API_TOKEN": "tok-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))
    monkeypatch.setattr(cli, "fetch_jira_issue", lambda key, env: f"[{key}] do it")

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "jira", "FOO-123", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert PR_URL in res.output


def test_build_jira_fetch_failure_clean_error_no_container(monkeypatch):
    """A JIRA fetch failure must surface as a clean ClickException and never reach the container."""
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_EMAIL": "user@example.com",
        "JIRA_API_TOKEN": "tok-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    ran = {"container": False}

    def _no_container(*a, **k):
        ran["container"] = True
        return 0, ""

    monkeypatch.setattr(cli, "run_in_container", _no_container)

    def _boom(key, env):
        raise ValueError("JIRA issue FOO-123 not found")

    monkeypatch.setattr(cli, "fetch_jira_issue", _boom)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "jira", "FOO-123", "--repo", "me/repo"])
    assert res.exit_code != 0
    assert "not found" in res.output
    assert ran["container"] is False  # fetch failure short-circuits before any container run


# ---------------------------------------------------------------------------
# Economics (tokens, est. cost, duration)
# ---------------------------------------------------------------------------


def _build_env():
    return {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def test_build_prints_economics_line_and_writes_to_log(monkeypatch):
    """build prints an economics line and writes it into the tasks log file."""
    import json as _json

    agent_output = (
        _json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.001,
            }
        )
        + f"\nopened {PR_URL}"
    )

    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, agent_output))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
        assert res.exit_code == 0, res.output
        # Economics line must appear in the combined output.
        assert "economics" in res.output
        # The log file must also contain the economics text (now under FRANKY_RUNS_DIR/tasks,
        # patched to a tmp dir by the autouse _hermetic_runs_dir fixture).
        logs = list((jobs.runs_dir() / "tasks").glob("*.log"))
        assert logs, "no log file written"
        log_text = logs[0].read_text()
        assert "economics" in log_text


def test_build_plan_first_approved_emits_exactly_one_economics_line(monkeypatch):
    """--plan-first approved run emits exactly ONE economics line (build pass only)."""
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    outputs = ["PLAN: do the thing", f"opened {PR_URL}"]

    def fake_run(*a, **k):
        return 0, outputs.pop(0)

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first"], input="y\n"
        )
    assert res.exit_code == 0, res.output
    # Exactly one economics line in the combined output.
    assert res.output.count("franky: economics") == 1


def test_build_unknown_usage_degrades_gracefully(monkeypatch):
    """When agent output has no parseable usage, build exits 0 and still prints economics."""
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    # Output with no JSON usage at all.
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert "economics" in res.output
    assert "unknown" in res.output


def test_build_prints_economics_even_when_agent_exits_nonzero(monkeypatch):
    """A non-zero agent exit still raises, but the economics summary must be printed first
    (emitted before the ClickException), so spend is always visible even on failure."""
    import json as _json

    agent_output = _json.dumps(
        {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}, "cost": 0.002}
    )

    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (1, agent_output))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
        # Non-zero agent exit surfaces as a non-zero CLI exit...
        assert res.exit_code != 0
        # ...but the economics line was still emitted (and logged) before the failure.
        assert "franky: economics" in res.output
        logs = list((jobs.runs_dir() / "tasks").glob("*.log"))
        assert logs and "economics" in logs[0].read_text()


# ---------------------------------------------------------------------------
# iterate command (follow-up pass on an existing PR)
# ---------------------------------------------------------------------------
# Reuses the module-level PR_URL (https://github.com/me/repo/pull/11) - me/repo is allowlisted.


def _iterate_env():
    return {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def test_iterate_help_shows_engine():
    res = CliRunner().invoke(cli.main, ["iterate", "--help"])
    assert res.exit_code == 0
    assert "--engine" in res.output


def test_iterate_reaches_completion_and_economics(monkeypatch):
    # Hermetic: valid env, image present, container mocked to a clean exit with usage.
    agent_output = (
        json.dumps({"type": "result", "usage": {"input_tokens": 80, "output_tokens": 40}})
        + "\npushed follow-up commits"
    )
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, agent_output))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])
        assert res.exit_code == 0, res.output
        # Labeled completion line (not a bare success URL) + economics, and the URL is shown.
        assert "iterate pass complete" in res.output
        assert PR_URL in res.output
        assert "franky: economics" in res.output
        logs = list((jobs.runs_dir() / "tasks").glob("*.log"))
        assert logs and "economics" in logs[0].read_text()


def test_iterate_invokes_auto_update_hint(monkeypatch):
    seen = {"called": False}
    monkeypatch.setattr(cli, "maybe_auto_update", lambda *a, **k: seen.update(called=True))
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, "ok"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])
    assert res.exit_code == 0, res.output
    assert seen["called"] is True


def test_iterate_engine_environment_selects_engine_image(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
        "FRANKY_ENGINE": "claude",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))

    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["engine"] = cfg.engine.name
        seen["argv0"] = inner_argv[0]
        seen["image"] = k["image"]
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])
    assert res.exit_code == 0, res.output
    assert seen["engine"] == "claude"
    assert seen["argv0"] == "claude"
    assert seen["image"].endswith(f":{__version__}-claude")


def test_iterate_off_allowlist_clean_error(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    ran = {"container": False}
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: ran.update(container=True) or (0, "")
    )

    res = CliRunner().invoke(cli.main, ["iterate", "https://github.com/stranger/repo/pull/1"])
    assert res.exit_code != 0
    assert "allowlist" in res.output
    assert ran["container"] is False  # gate refused before any container run


def test_iterate_invalid_url_clean_error(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, ""))

    # An issue URL is not a PR URL.
    res = CliRunner().invoke(cli.main, ["iterate", "https://github.com/me/repo/issues/1"])
    assert res.exit_code != 0
    assert "PR URL" in res.output


def test_iterate_missing_creds_clean_error_no_secret_leak(monkeypatch):
    env = {"FRANKY_ALLOWED_REPOS": "me/repo", "GH_TOKEN": "ghp_fake"}  # no provider key
    monkeypatch.setattr(cli.os, "environ", env)

    res = CliRunner().invoke(cli.main, ["iterate", PR_URL])
    assert res.exit_code != 0
    assert "ghp_fake" not in res.output  # no secret value on the error path


def test_iterate_nonzero_exit_emits_economics_and_writes_log(monkeypatch):
    # A non-zero agent exit raises, but economics is emitted + logged first, and no bare URL is
    # presented as a fresh success artifact (the completion line is labeled).
    agent_output = json.dumps({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}})
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (1, agent_output))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])
        assert res.exit_code != 0
        assert "franky: economics" in res.output
        logs = list((jobs.runs_dir() / "tasks").glob("*.log"))
        assert logs and "economics" in logs[0].read_text()


def test_iterate_image_no_docker_clean_message(monkeypatch):
    # iterate shares _ensure_images with build; confirm the image gate fires on the iterate
    # path too (clean message, no container run).
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "ghcr.io/vietlabs-work/franky:0.1.0")
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "no-docker"))
    ran = {"container": False}
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: ran.update(container=True) or (0, "")
    )

    res = CliRunner().invoke(cli.main, ["iterate", PR_URL])
    assert res.exit_code != 0
    assert "docker is not available" in res.output
    assert ran["container"] is False


def test_engine_image_pull_failure_does_not_try_full_image(monkeypatch):
    images = []

    def unavailable(image):
        images.append(image)
        return False, "pull-failed"

    monkeypatch.setattr(cli, "ensure_image_available", unavailable)
    with pytest.raises(cli.DockerError):
        cli._ensure_images({}, "pi")
    assert images == [f"ghcr.io/vietlabs-work/franky:{__version__}-pi"]


def test_engine_image_pull_timeout_names_the_timeout(monkeypatch):
    monkeypatch.setattr(cli, "ensure_image_available", lambda image: (False, "pull-timeout"))
    with pytest.raises(cli.DockerError, match="pull did not finish") as info:
        cli._ensure_images({}, "codex")
    exc = info.value
    assert isinstance(exc, cli.ImagePullTimeout)
    assert exc.kind == "image_pull_timeout"
    assert exc.code == 6
    assert "safe" in exc.hint and "docker pull ghcr.io/" in exc.hint
    # Text output prints only the message, so the remediation must be there too.
    assert "retrying is safe" in str(exc) and "docker pull ghcr.io/" in str(exc)


@pytest.mark.parametrize("cmd", [["build", "do it", "--repo", "me/repo"], ["iterate", PR_URL]])
def test_pull_timeout_json_error_kind_and_exit(monkeypatch, cmd):
    env = {**_mc_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "pull-timeout"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, [*cmd, "--json"])
    assert res.exit_code == 6
    err = json.loads(res.stdout)["error"]
    assert err["kind"] == "image_pull_timeout"
    assert err["code"] == 6
    assert "docker pull" in err["hint"]


def test_pull_images_pulls_both_and_reports_on_stderr(monkeypatch, tmp_path):
    pulled = []
    monkeypatch.setattr(cli.os, "environ", {"FRANKY_ENGINE": "codex", **_cfg_env(tmp_path)})
    monkeypatch.setattr(
        cli, "ensure_image_available", lambda image: pulled.append(image) or (True, "")
    )
    res = CliRunner().invoke(cli.main, ["pull-images"])
    assert res.exit_code == 0, res.output
    assert pulled == [
        f"ghcr.io/vietlabs-work/franky:{__version__}-codex",
        f"ghcr.io/vietlabs-work/franky-proxy:{__version__}",
    ]
    assert res.stdout == ""


def test_pull_images_failure_exits_with_error_code_and_docker_pull(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.os, "environ", _cfg_env(tmp_path))
    monkeypatch.setattr(cli, "ensure_image_available", lambda image: (False, "pull-timeout"))
    res = CliRunner().invoke(cli.main, ["pull-images"])
    assert res.exit_code == 6
    assert "docker pull ghcr.io/" in res.output


def test_pull_images_uses_the_engine_from_the_config_file(monkeypatch, tmp_path):
    # A bot sets its engine in the config file, not the shell: the pull must match a run.
    env = _cfg_env(tmp_path)
    Path(env["FRANKY_CONFIG_FILE"]).write_text('[franky]\nFRANKY_ENGINE = "claude"\n')
    monkeypatch.setattr(cli.os, "environ", env)
    pulled = []
    monkeypatch.setattr(
        cli, "ensure_image_available", lambda image: pulled.append(image) or (True, "")
    )
    res = CliRunner().invoke(cli.main, ["pull-images"])
    assert res.exit_code == 0, res.output
    assert pulled[0] == f"ghcr.io/vietlabs-work/franky:{__version__}-claude"
    assert "FRANKY_ENGINE" not in cli.os.environ  # the copy was loaded, not the process env


def test_pull_images_is_hidden_from_help_but_in_schema():
    assert "pull-images" not in CliRunner().invoke(cli.main, ["--help"]).output
    assert "pull-images" in build_schema(cli.main)["commands"]


# ---------------------------------------------------------------------------
# config subgroup
# ---------------------------------------------------------------------------
# All these tests use tmp_path-rooted paths (via FRANKY_CONFIG_FILE override set
# by the autouse fixture in conftest.py, with per-test overrides where needed).


def _cfg_env(tmp_path) -> dict[str, str]:
    """A fresh env dict with FRANKY_CONFIG_FILE pointing at a non-existent tmp path."""
    return {"FRANKY_CONFIG_FILE": str(tmp_path / "franky-config")}


def test_config_path_prints_path(tmp_path, monkeypatch):
    cfg_path = str(tmp_path / "franky-config")
    monkeypatch.setenv("FRANKY_CONFIG_FILE", cfg_path)
    res = CliRunner().invoke(cli.main, ["config", "path"])
    assert res.exit_code == 0, res.output
    assert cfg_path in res.output


def test_config_list_absent_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(tmp_path / "nonexistent"))
    res = CliRunner().invoke(cli.main, ["config", "list"])
    assert res.exit_code == 0
    assert "not found" in res.output


def test_config_list_shows_masked_secrets(tmp_path, monkeypatch):
    from franky.userconfig import write_config_file

    cfg_path = tmp_path / "franky-config"
    write_config_file(
        cfg_path,
        {
            "GH_TOKEN": "ghp_real",
            "MOONSHOT_API_KEY": "moonshot_real",
            "FRANKY_ENGINE": "pi",
        },
    )
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["config", "list"])
    assert res.exit_code == 0, res.output
    assert "ghp_real" not in res.output
    assert "moonshot_real" not in res.output
    assert "MOONSHOT_API_KEY" in res.output
    assert "***REDACTED***" in res.output
    assert "FRANKY_ENGINE" in res.output
    assert "pi" in res.output


def test_config_list_reveal_shows_plain_values(tmp_path, monkeypatch):
    from franky.userconfig import write_config_file

    cfg_path = tmp_path / "franky-config"
    write_config_file(cfg_path, {"GH_TOKEN": "ghp_real"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["config", "list", "--reveal"])
    assert res.exit_code == 0, res.output
    assert "ghp_real" in res.output


def test_config_set_non_secret_positional(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["config", "set", "FRANKY_ENGINE", "pi"])
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    assert read_config_file(cfg_path)["FRANKY_ENGINE"] == "pi"


def test_config_set_secret_refuses_positional_value(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["config", "set", "GH_TOKEN", "ghp_bad"])
    assert res.exit_code != 0
    assert "shell history" in res.output or "secret" in res.output


def test_config_set_secret_via_hidden_prompt(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["config", "set", "GH_TOKEN"], input="ghp_from_prompt\n")
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    assert read_config_file(cfg_path)["GH_TOKEN"] == "ghp_from_prompt"


def _stdin_set(tmp_path, monkeypatch, args, data, tty=False):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: tty)
    res = CliRunner().invoke(cli.main, ["config", "set", *args], input=data)
    return res, cfg_path


def _stored(cfg_path, key):
    from franky.userconfig import read_config_file

    return read_config_file(cfg_path)[key]


def test_config_set_stdin_success_strips_crlf_keeps_spaces_mode_0600(tmp_path, monkeypatch):
    res, cfg = _stdin_set(tmp_path, monkeypatch, ["GH_TOKEN", "--stdin"], " tok en \r\n\n")
    assert res.exit_code == 0, res.output
    assert _stored(cfg, "GH_TOKEN") == " tok en "
    assert "wrote GH_TOKEN to" in res.output
    assert "tok en" not in res.output
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("args", "data", "tty"),
    [
        (["GH_TOKEN", "--stdin"], "\r\n", False),
        (["GH_TOKEN", "--stdin"], "SECRETa\nSECRETb\n", False),
        (["GH_TOKEN", "--stdin"], "SECRETa\rSECRETb\n", False),
        (["GH_TOKEN", "--stdin"], "SECRET" + "x" * 70000, False),
        (["GH_TOKEN", "--stdin"], b"SECRET\xff\n", False),
        (["GH_TOKEN", "--stdin"], "SECRET x\n", False),
        (["GH_TOKEN", "--stdin"], "SECRET\tx\n", False),
        (["GH_TOKEN", "--stdin"], "SECRET\n", True),
        (["GH_TOKEN", "SECRET", "--stdin"], "SECRET\n", False),
        (["FRANKY_ENGINE", "--stdin"], "SECRET\n", False),
    ],
    ids=[
        "empty",
        "embedded-newline",
        "embedded-cr",
        "oversize",
        "invalid-utf8",
        "non-ascii",
        "control-char",
        "tty",
        "positional",
        "non-secret",
    ],
)
def test_config_set_stdin_refusals_never_echo_value(tmp_path, monkeypatch, args, data, tty):
    res, cfg = _stdin_set(tmp_path, monkeypatch, args, data, tty)
    assert res.exit_code == 2, res.output
    assert "SECRET" not in res.output
    assert "Traceback" not in res.output
    assert not cfg.exists()


def test_config_set_stdin_size_boundary(tmp_path, monkeypatch):
    res, cfg = _stdin_set(
        tmp_path, monkeypatch, ["GH_TOKEN", "--stdin"], "x" * cli._STDIN_SECRET_MAX
    )
    assert res.exit_code == 0, res.output
    assert len(_stored(cfg, "GH_TOKEN")) == cli._STDIN_SECRET_MAX
    cfg.unlink()
    res, cfg = _stdin_set(
        tmp_path, monkeypatch, ["GH_TOKEN", "--stdin"], "x" * (cli._STDIN_SECRET_MAX + 1)
    )
    assert res.exit_code == 2
    assert not cfg.exists()


def test_config_set_moonshot_key_requires_hidden_prompt(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    positional = CliRunner().invoke(cli.main, ["config", "set", "MOONSHOT_API_KEY", "moonshot_bad"])
    assert positional.exit_code != 0
    assert "secret" in positional.output

    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    prompted = CliRunner().invoke(
        cli.main, ["config", "set", "MOONSHOT_API_KEY"], input="moonshot_prompted\n"
    )
    assert prompted.exit_code == 0, prompted.output
    from franky.userconfig import read_config_file

    assert read_config_file(cfg_path)["MOONSHOT_API_KEY"] == "moonshot_prompted"


def test_config_set_unknown_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(tmp_path / "franky-config"))
    res = CliRunner().invoke(cli.main, ["config", "set", "TOTALLY_UNKNOWN_KEY", "val"])
    assert res.exit_code != 0
    assert "unknown config key" in res.output or "TOTALLY_UNKNOWN_KEY" in res.output


def test_config_init_full_wizard(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    runner = CliRunner()
    # Simulate: engine=pi, repos=me/repo, GH_TOKEN=ghp_wiz, provider=OPENROUTER,
    # key=sk-or-wiz, no JIRA
    wizard_input = (
        "pi\n"  # engine
        "me/repo\n"  # FRANKY_ALLOWED_REPOS
        "ghp_wiz\n"  # GH_TOKEN
        "OPENROUTER_API_KEY\n"  # provider var choice
        "sk-or-wiz\n"  # provider value
        "n\n"  # JIRA: no
        "n\n"  # profile: no
    )
    res = runner.invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    assert cfg_path.exists()
    from franky.userconfig import read_config_file

    data = read_config_file(cfg_path)
    assert data["FRANKY_ENGINE"] == "pi"
    assert data["FRANKY_ALLOWED_REPOS"] == "me/repo"
    assert data["GH_TOKEN"] == "ghp_wiz"
    assert data["OPENROUTER_API_KEY"] == "sk-or-wiz"


def test_config_init_claude_wizard(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = (
        "claude\n"  # engine
        "me/repo\n"  # FRANKY_ALLOWED_REPOS
        "ghp_wiz\n"  # GH_TOKEN
        "claude-tok\n"  # CLAUDE_CODE_OAUTH_TOKEN
        "n\n"  # JIRA: no
        "n\n"  # profile: no
    )
    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    data = read_config_file(cfg_path)
    assert data["FRANKY_ENGINE"] == "claude"
    assert data["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-tok"


def test_config_init_codex_wizard_with_codex_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = (
        "codex\n"  # engine
        "me/repo\n"  # FRANKY_ALLOWED_REPOS
        "ghp_wiz\n"  # GH_TOKEN
        "codex-key\n"  # CODEX_API_KEY
        "n\n"  # JIRA: no
        "n\n"  # profile: no
    )
    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    data = read_config_file(cfg_path)
    assert data["CODEX_API_KEY"] == "codex-key"
    assert "OPENAI_API_KEY" not in data


def test_config_init_codex_wizard_does_not_fall_back_to_openai(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = (
        "codex\n"  # engine
        "me/repo\n"  # FRANKY_ALLOWED_REPOS
        "ghp_wiz\n"  # GH_TOKEN
        "\n"  # CODEX_API_KEY empty
        "n\n"  # JIRA: no
        "n\n"  # profile: no
    )
    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    data = read_config_file(cfg_path)
    assert "OPENAI_API_KEY" not in data
    assert "CODEX_API_KEY" not in data
    assert "OPENAI_API_KEY instead" not in res.output


def test_iterate_load_config_file_malformed_gives_clean_error(tmp_path, monkeypatch):
    """A malformed ~/.franky/config must produce a clean ClickException in iterate too."""
    cfg_path = tmp_path / "franky-config"
    cfg_path.write_text("[franky\nbroken", encoding="utf-8")
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["iterate", "https://github.com/me/repo/pull/1"])
    assert res.exit_code != 0
    assert "config file error" in res.output or "malformed" in res.output
    assert "Traceback" not in res.output


def test_main_help_shows_config_group():
    res = CliRunner().invoke(cli.main, ["--help"])
    assert res.exit_code == 0
    assert "config" in res.output


def test_main_help_shows_auth_group():
    res = CliRunner().invoke(cli.main, ["--help"])
    assert res.exit_code == 0
    assert "auth" in res.output


def test_auth_login_codex_persists_marker_after_success(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    monkeypatch.setattr(cli, "codex_auth_login", lambda image, **_kw: True)
    res = CliRunner().invoke(cli.main, ["auth", "login", "codex"])
    assert res.exit_code == 0, res.output
    assert cli.read_config_file(cfg_path)["FRANKY_CODEX_SUBSCRIPTION"] == "1"


def test_codex_auth_uses_codex_image_when_default_engine_is_pi(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", {})
    images = []

    def available(image):
        images.append(image)
        return True, ""

    monkeypatch.setattr(cli, "ensure_image_available", available)
    assert cli._codex_auth_image().endswith(f":{__version__}-codex")
    assert images == [f"ghcr.io/vietlabs-work/franky:{__version__}-codex"]


def test_auth_login_codex_rejects_invalid_auth_volume_override(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FRANKY_CODEX_AUTH_VOLUME", "bad name!")
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    res = CliRunner().invoke(cli.main, ["auth", "login", "codex"])
    assert res.exit_code != 0
    assert "FRANKY_CODEX_AUTH_VOLUME" in res.output
    assert not cfg_path.exists()


def test_auth_login_codex_does_not_persist_marker_on_failure(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    monkeypatch.setattr(cli, "codex_auth_login", lambda image, **_kw: False)
    res = CliRunner().invoke(cli.main, ["auth", "login", "codex"])
    assert res.exit_code != 0
    assert not cfg_path.exists()


def test_auth_login_codex_clears_stale_marker_before_failure(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    monkeypatch.setattr(cli, "codex_auth_login", lambda image, **_kw: False)
    res = CliRunner().invoke(cli.main, ["auth", "login", "codex"])
    assert res.exit_code != 0
    assert "FRANKY_CODEX_SUBSCRIPTION" not in cli.read_config_file(cfg_path)


def test_auth_status_codex_uses_persisted_volume(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    monkeypatch.setattr(cli, "codex_auth_status", lambda image, **_kw: True)
    res = CliRunner().invoke(cli.main, ["auth", "status", "codex"])
    assert res.exit_code == 0
    assert "ready" in res.output


def test_auth_status_codex_rejects_invalid_auth_volume_override(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FRANKY_CODEX_AUTH_VOLUME", "bad name!")
    res = CliRunner().invoke(cli.main, ["auth", "status", "codex"])
    assert res.exit_code != 0
    assert "FRANKY_CODEX_AUTH_VOLUME" in res.output


def test_auth_status_codex_requires_enabled_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: "franky:test")
    monkeypatch.setattr(cli, "codex_auth_status", lambda image, **_kw: True)
    res = CliRunner().invoke(cli.main, ["auth", "status", "codex"])
    assert res.exit_code != 0
    assert "not enabled" in res.output


def test_auth_logout_codex_removes_marker_after_volume(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1", "FRANKY_ENGINE": "codex"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "codex_auth_logout", lambda **_kw: True)
    res = CliRunner().invoke(cli.main, ["auth", "logout", "codex"])
    assert res.exit_code == 0, res.output
    assert cli.read_config_file(cfg_path) == {"FRANKY_ENGINE": "codex"}


def test_auth_logout_codex_rejects_invalid_auth_volume_override(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FRANKY_CODEX_AUTH_VOLUME", "bad name!")
    res = CliRunner().invoke(cli.main, ["auth", "logout", "codex"])
    assert res.exit_code != 0
    assert "FRANKY_CODEX_AUTH_VOLUME" in res.output
    # Resolution fails BEFORE the marker is cleared - nothing should be mutated on refusal.
    assert cli.read_config_file(cfg_path).get("FRANKY_CODEX_SUBSCRIPTION") == "1"


def test_auth_logout_codex_keeps_marker_cleared_when_volume_removal_fails(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cli.write_config_file(cfg_path, {"FRANKY_CODEX_SUBSCRIPTION": "1"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "codex_auth_logout", lambda **_kw: False)
    res = CliRunner().invoke(cli.main, ["auth", "logout", "codex"])
    assert res.exit_code != 0
    assert "FRANKY_CODEX_SUBSCRIPTION" not in cli.read_config_file(cfg_path)


def test_build_load_config_file_malformed_gives_clean_error(tmp_path, monkeypatch):
    """A malformed ~/.franky/config must produce a clean ClickException in build, not a traceback."""
    cfg_path = tmp_path / "franky-config"
    cfg_path.write_text("[franky\nbroken", encoding="utf-8")
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code != 0
    # Should be a ClickException (clean message), not a raw traceback.
    assert "config file error" in res.output or "malformed" in res.output
    assert "Traceback" not in res.output


# ---------------------------------------------------------------------------
# --verbose / FRANKY_VERBOSE + progress callbacks
# ---------------------------------------------------------------------------


def test_build_help_shows_verbose_flag():
    res = CliRunner().invoke(cli.main, ["build", "--help"])
    assert res.exit_code == 0
    assert "--verbose" in res.output or "-v" in res.output


def test_iterate_help_shows_verbose_flag():
    res = CliRunner().invoke(cli.main, ["iterate", "--help"])
    assert res.exit_code == 0
    assert "--verbose" in res.output or "-v" in res.output


def test_make_progress_verbose_echoes_raw_lines():
    """In verbose mode, every line (including non-milestones) reaches the callback."""
    from franky.engine import PiEngine

    cb = cli._make_progress(PiEngine(), verbose=True)
    seen = []
    # Monkeypatch click.echo to capture stderr output.
    original_echo = cli.click.echo
    try:
        cli.click.echo = lambda msg, err=False, nl=True: seen.append(msg) if err else None
        cb("raw line no newline")
        cb('{"type":"done"}')
    finally:
        cli.click.echo = original_echo
    assert len(seen) == 2  # both lines surfaced


def test_make_progress_distilled_suppresses_non_milestone_lines():
    """Without verbose, non-milestone lines (e.g. intermediate token events) are dropped."""
    from franky.engine import PiEngine

    cb = cli._make_progress(PiEngine(), verbose=False)
    seen = []
    original_echo = cli.click.echo
    try:
        cli.click.echo = lambda msg, err=False, nl=True: seen.append(msg) if err else None
        cb('{"type":"usage","tokens":99}')  # not a milestone -> suppressed
        cb("not json")  # not JSON -> suppressed
    finally:
        cli.click.echo = original_echo
    assert seen == []


def test_make_progress_distilled_surfaces_milestones():
    """Without verbose, milestone events are echoed to stderr."""
    import json as _json
    from franky.engine import PiEngine

    cb = cli._make_progress(PiEngine(), verbose=False)
    seen = []
    original_echo = cli.click.echo
    tool_line = _json.dumps({"type": "done"})
    try:
        cli.click.echo = lambda msg, err=False, nl=True: seen.append(msg) if err else None
        cb(tool_line)
    finally:
        cli.click.echo = original_echo
    assert len(seen) == 1
    assert "agent complete" in seen[0]


def test_build_verbose_flag_passes_progress_to_container(monkeypatch):
    """--verbose causes a non-None progress callback to be passed to run_in_container."""
    env = _build_env()
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    received = {}

    def fake_run(cfg, inner_argv, *a, **k):
        received["progress"] = k.get("progress")
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "--verbose", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert received.get("progress") is not None


def test_build_no_verbose_flag_still_passes_progress_to_container(monkeypatch):
    """Without --verbose a distilled progress callback is still passed (Phase 2 is default)."""
    env = _build_env()
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    received = {}

    def fake_run(cfg, inner_argv, *a, **k):
        received["progress"] = k.get("progress")
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert received.get("progress") is not None


def test_build_franky_verbose_env_activates_verbose(monkeypatch):
    """FRANKY_VERBOSE=1 in env is equivalent to --verbose flag."""
    env = {**_build_env(), cli.FRANKY_VERBOSE_VAR: "1"}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    received_cbs = []

    def fake_run(cfg, inner_argv, *a, **k):
        received_cbs.append(k.get("progress"))
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert received_cbs  # progress was passed
    # With verbose active, raw lines must be echoed (nl=False distinguishes verbose from distilled)
    lines_seen = []
    original_echo = cli.click.echo
    try:
        cli.click.echo = lambda msg, err=False, nl=True: (
            lines_seen.append((msg, nl)) if err else None
        )
        received_cbs[0]("a raw line")
    finally:
        cli.click.echo = original_echo
    assert any(nl is False for _, nl in lines_seen), "verbose path should use nl=False"


# ---------------------------------------------------------------------------
# Machine contract: --json results, exit-code taxonomy, --quiet, stdin, never-hang (#50)
# ---------------------------------------------------------------------------


def _mc_env():
    return {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def _mc_setup(monkeypatch, env=None, container=(0, None)):
    """Wire a hermetic build/iterate: env, images present, container mocked. container is
    (code, output); output=None -> a default `opened {PR_URL}` body."""
    env = dict(env) if env is not None else _mc_env()
    env.setdefault("FRANKY_CONFIG_FILE", os.environ["FRANKY_CONFIG_FILE"])
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    code, output = container
    if output is None:
        output = f"opened {PR_URL}"
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (code, output))


def test_build_json_success_stdout_is_one_object(monkeypatch):
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    # stdout must be EXACTLY one JSON object (no prose econ line, no bare URL).
    data = json.loads(res.stdout)
    assert data["status"] == "pr_opened"
    assert data["exit_code"] == 0
    assert data["pr_url"] == PR_URL
    assert data["repo"] == "me/repo"
    assert data["engine"] == "pi"
    assert set(data["economics"]) == {"tokens_in", "tokens_out", "cost_usd", "duration_s"}
    # stdout purity: exactly one line, parseable as one object.
    assert len(res.stdout.strip().splitlines()) == 1
    assert "economics -" not in res.stdout  # no prose econ leaked onto stdout


def test_build_json_includes_job_id(monkeypatch):
    """The run handle (issue #63) rides the --json result so a caller can `franky job ...` it."""
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data["job_id"], str) and data["job_id"]


def test_build_survives_registry_failure(monkeypatch):
    """A registry write blowing up must never fail a build (best-effort contract)."""

    def _raise(*a, **k):
        raise RuntimeError("registry boom")

    _mc_setup(monkeypatch)
    monkeypatch.setattr(cli.jobs, "write_record", _raise)
    monkeypatch.setattr(cli.jobs, "update_record", _raise)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "pr_opened"
    assert data["job_id"]  # still generated + surfaced despite the registry failures


def test_build_json_no_pr_exits_7(monkeypatch):
    _mc_setup(monkeypatch, container=(0, "did stuff, no url"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "no_pr"
    assert data["exit_code"] == 7
    assert data["pr_url"] is None


def test_build_json_agent_error_exits_7(monkeypatch):
    _mc_setup(monkeypatch, container=(1, "boom"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "agent_error"
    assert data["exit_code"] == 7


def test_build_default_no_pr_now_exits_7(monkeypatch):
    # Behavior change: a clean agent exit that produced no PR URL now exits 7 (was 0).
    _mc_setup(monkeypatch, container=(0, "no url here"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 7
    assert "no PR URL" in res.output


def test_build_json_config_error_exits_3(monkeypatch):
    env = {"GH_TOKEN": "ghp_fake", "OPENROUTER_API_KEY": "sk-or-fake"}  # no allowlist
    _mc_setup(monkeypatch, env=env)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 3
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 3
    assert data["error"]["kind"] == "config_error"


def test_build_json_allowlist_rejection_exits_4(monkeypatch):
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "stranger/repo", "--json"])
    assert res.exit_code == 4
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 4
    assert data["error"]["kind"] == "task_rejected"


def test_build_json_missing_creds_exits_5(monkeypatch):
    env = {"FRANKY_ALLOWED_REPOS": "me/repo", "GH_TOKEN": "ghp_fake"}  # no provider key
    env["FRANKY_CONFIG_FILE"] = os.environ["FRANKY_CONFIG_FILE"]
    monkeypatch.setattr(cli.os, "environ", env)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 5
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 5
    assert data["error"]["kind"] == "auth_error"
    assert "ghp_fake" not in res.output  # never leak a secret


def test_build_json_docker_unavailable_exits_6(monkeypatch):
    env = {**_mc_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "ghcr.io/x/franky:0.1.0")
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (False, "no-docker"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 6
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 6
    assert data["error"]["kind"] == "docker_error"


def test_build_json_redacts_secret_inside_object(monkeypatch):
    # The agent output carries the fake secret value AND the PR URL; the JSON must scrub it.
    leaky = f"using sk-or-fake to open {PR_URL}"
    _mc_setup(monkeypatch, container=(0, leaky))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    assert "sk-or-fake" not in res.stdout
    assert "sk-or-fake" not in res.output
    # The PR URL still parses out fine (only the secret was masked).
    data = json.loads(res.stdout)
    assert data["pr_url"] == PR_URL


def test_build_default_stdout_purity_is_bare_pr_url(monkeypatch):
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    # stdout is EXACTLY the PR URL + newline (the econ line goes to stderr).
    assert res.stdout == PR_URL + "\n"


def test_build_quiet_suppresses_update_hint_and_progress(monkeypatch):
    called = {"update": False, "progress_passed": None}
    monkeypatch.setattr(cli, "maybe_auto_update", lambda *a, **k: called.update(update=True))
    env = {**_mc_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    def fake_run(cfg, inner_argv, *a, **k):
        called["progress_passed"] = k.get("progress")
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--quiet"])
    assert res.exit_code == 0, res.output
    assert called["update"] is False  # no update hint under --quiet
    # --quiet prints nothing, but the liveness heartbeat still needs to see the agent's lines.
    called["progress_passed"]("not a tool event\n")
    assert "not a tool event" not in res.output


def test_build_json_implies_quiet_no_update_hint(monkeypatch):
    called = {"update": False}
    monkeypatch.setattr(cli, "maybe_auto_update", lambda *a, **k: called.update(update=True))
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    assert called["update"] is False  # --json implies --quiet


def test_build_yes_approves_plan_first_non_interactive(monkeypatch):
    # --yes auto-approves the gate with no TTY: both passes run, the PR is reported, exit 0.
    env = {**_mc_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    outputs = ["PLAN: do the thing", f"opened {PR_URL}"]

    def fake_run(*a, **k):
        return 0, outputs.pop(0)

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--plan-first", "--yes"]
        )
    assert res.exit_code == 0, res.output
    assert not outputs  # both passes ran
    assert PR_URL in res.output


def test_build_stdin_task_input(monkeypatch):
    # `build - --repo me/repo` reads the prose task from stdin (non-TTY) and reaches the PR.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "-", "--repo", "me/repo"], input="add a flag\n")
    assert res.exit_code == 0, res.output
    assert PR_URL in res.output


def test_build_stdin_dash_interactive_fails_fast(monkeypatch):
    # `build -` with an interactive TTY would block forever; fail fast (exit 2).
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    _mc_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "-", "--repo", "me/repo"])
    assert res.exit_code == 2


def test_config_set_non_secret_non_interactive_no_hang(monkeypatch, tmp_path):
    # `config set KEY` with no value and no TTY must fail fast (exit 2), not block on prompt.
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(tmp_path / "franky-config"))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    res = CliRunner().invoke(cli.main, ["config", "set", "FRANKY_ALLOWED_REPOS"])
    assert res.exit_code == 2


def test_config_init_non_interactive_no_hang(monkeypatch, tmp_path):
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(tmp_path / "franky-config"))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    res = CliRunner().invoke(cli.main, ["config", "init"])
    assert res.exit_code == 2
    assert "interactive" in res.output  # failed for the right reason, not an unrelated error


def test_profile_init_non_interactive_no_hang(monkeypatch, tmp_path):
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(tmp_path / "profile.toml"))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    res = CliRunner().invoke(cli.main, ["profile", "init"])
    assert res.exit_code == 2
    assert "interactive" in res.output


def test_iterate_json_complete(monkeypatch):
    agent_output = json.dumps({"type": "result", "usage": {"input_tokens": 8, "output_tokens": 4}})
    env = {**_iterate_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, agent_output))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "iterate_complete"
    assert data["pr_url"] == PR_URL
    assert data["branch"] is None
    assert len(res.stdout.strip().splitlines()) == 1


def test_iterate_json_agent_error_exits_7(monkeypatch):
    env = {**_iterate_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (1, "boom"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL, "--json"])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "agent_error"
    assert data["exit_code"] == 7


def test_iterate_json_error_object_off_allowlist(monkeypatch):
    # A pre-container failure under --json must emit the {"error": {...}} object (not prose)
    # on stdout and exit with that code. Off-allowlist PR URL -> task rejection, exit 4.
    env = {**_iterate_env(), "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    ran = {"container": False}
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: ran.update(container=True))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", "https://github.com/other/repo/pull/9", "--json"])
    assert res.exit_code == 4
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 4
    assert data["error"]["kind"] == "task_rejected"
    assert len(res.stdout.strip().splitlines()) == 1  # exactly one JSON object on stdout
    assert ran["container"] is False


# ---------------------------------------------------------------------------
# `franky profile` subgroup (issue #49)
# ---------------------------------------------------------------------------

_GHP_FAKE = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab"


def test_profile_path_prints_resolved_override(tmp_path, monkeypatch):
    p = str(tmp_path / "custom.toml")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", p)
    res = CliRunner().invoke(cli.main, ["profile", "path"])
    assert res.exit_code == 0, res.output
    assert p in res.output


def test_profile_show_absent_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(tmp_path / "nope.toml"))
    res = CliRunner().invoke(cli.main, ["profile", "show"])
    assert res.exit_code == 0
    assert "not found" in res.output


def test_profile_show_lists_expanded_files(tmp_path, monkeypatch):
    skill = tmp_path / "skill.md"
    skill.write_text("# my skill\n", encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\nskills = ["{skill}"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "show"])
    assert res.exit_code == 0, res.output
    assert str(skill) in res.output
    assert "expanded files" in res.output
    assert "[skills]" in res.output  # the expanded-files loop ran, not just the TOML echo


def test_profile_show_lists_mcp_names_and_domains_without_values(tmp_path, monkeypatch):
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"token":"${LINEAR_API_KEY}"}\n', encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\nmcp_credentials = ["LINEAR_API_KEY"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.setenv("LINEAR_API_KEY", "mcp-secret-value")

    res = CliRunner().invoke(cli.main, ["profile", "show"])

    assert res.exit_code == 0, res.output
    assert "LINEAR_API_KEY" in res.output
    assert "mcp-secret-value" not in res.output


def test_profile_show_empty_expansion(tmp_path, monkeypatch):
    empty = tmp_path / "skills"
    empty.mkdir()
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\nskills = ["{empty}/*.md"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "show"])
    assert res.exit_code == 0, res.output
    assert "(none)" in res.output


def test_profile_show_bad_toml_is_lenient(tmp_path, monkeypatch):
    prof = tmp_path / "profile.toml"
    prof.write_text("[profile\nskills = oops", encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "show"])
    # show is lenient: it prints the raw text + a warning, but does not hard-fail.
    assert res.exit_code == 0, res.output
    assert "could not expand profile" in res.output


def test_profile_check_clean_exits_zero(tmp_path, monkeypatch):
    skill = tmp_path / "skill.md"
    skill.write_text("# clean\n", encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\nskills = ["{skill}"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "check"])
    assert res.exit_code == 0, res.output
    assert "OK:" in res.output


def test_profile_check_requires_mcp_credential_from_process_environment(tmp_path, monkeypatch):
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"token":"${LINEAR_API_KEY}"}\n', encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\nmcp_credentials = ["LINEAR_API_KEY"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    res = CliRunner().invoke(cli.main, ["profile", "check"])

    assert res.exit_code != 0
    assert "LINEAR_API_KEY" in res.output


def test_load_profile_bundle_applies_named_credential_egress_and_redaction(tmp_path, monkeypatch):
    from franky.config import Config
    from franky.container import build_docker_argv
    from franky.engine import PiEngine

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        '{"url":"https://mcp.linear.app/mcp","token":"${LINEAR_API_KEY}"}\n',
        encoding="utf-8",
    )
    prof = tmp_path / "profile.toml"
    prof.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\n'
        'mcp_credentials = ["LINEAR_API_KEY"]\n'
        'mcp_domains = ["mcp.linear.app"]\n',
        encoding="utf-8",
    )
    cfg = Config(engine=PiEngine(), allowed_repos=["me/repo"])
    secrets = []

    bundle, setup_block = cli._load_profile_bundle(
        str(prof), {"LINEAR_API_KEY": "mcp-secret-value"}, secrets, cfg
    )

    # Unpacked, not `assert bundle` on the tuple: a 2-tuple is truthy even when the bundle is
    # None, which would make this assertion vacuous.
    assert bundle
    assert setup_block == ""  # no [setups] declared
    assert cfg.passthrough_env == {"LINEAR_API_KEY": "mcp-secret-value"}
    assert cfg.extra_allowed_domains == ["mcp.linear.app"]
    assert secrets == ["mcp-secret-value"]
    argv = build_docker_argv("franky", cfg.passthrough_env, ["pi"])
    assert "LINEAR_API_KEY" in argv
    assert "mcp-secret-value" not in argv


def test_load_profile_bundle_warns_when_a_declared_setup_swept_nothing(tmp_path, monkeypatch):
    """A setup that matches no files must SAY so - injecting nothing silently is the one
    failure the operator would not notice (they declared it precisely to have it there)."""
    from franky.config import Config
    from franky.engine import PiEngine

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    empty = tmp_path / ".claude"
    empty.mkdir()
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[setups]\nclaude = "{empty}"\n', encoding="utf-8")
    cfg = Config(engine=PiEngine(), allowed_repos=["me/repo"])

    runner = CliRunner()
    with runner.isolation() as (_out, err, _):
        bundle, block = cli._load_profile_bundle(str(prof), {}, [], cfg)
        warning = err.getvalue().decode()

    assert bundle is None
    assert block == ""
    assert "matched no files" in warning


def test_build_does_not_accept_mcp_credential_loaded_from_franky_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"token":"${LINEAR_API_KEY}"}\n', encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\nmcp_credentials = ["LINEAR_API_KEY"]\n',
        encoding="utf-8",
    )
    user_config = tmp_path / "config"
    user_config.write_text('[franky]\nLINEAR_API_KEY = "file-secret"\n', encoding="utf-8")
    env = {
        **_build_env(),
        "FRANKY_PROFILE_PATH": str(prof),
        "FRANKY_CONFIG_FILE": str(user_config),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])

    assert res.exit_code == 3, res.output
    assert "LINEAR_API_KEY" in res.output
    assert "file-secret" not in res.output


def test_run_pass_keeps_codex_user_config_ignored_and_adds_explicit_mcp_overrides(
    tmp_path, monkeypatch
):
    from franky.config import Config
    from franky.engine import CodexEngine

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".codex" / "franky-mcp.config.toml"
    mcp.parent.mkdir()
    mcp.write_text('[mcp_servers.linear]\ncommand = "npx"\n', encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\n',
        encoding="utf-8",
    )
    cfg = Config(engine=CodexEngine(), allowed_repos=["me/repo"])
    cli._load_profile_bundle(str(prof), {}, [], cfg)
    seen = {}

    def fake_run(_cfg, inner_argv, **_kwargs):
        seen["argv"] = inner_argv
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    cli._run_pass(cfg, "do it", "franky", "franky-proxy")

    assert "--ignore-user-config" in seen["argv"]
    assert "--profile" not in seen["argv"]
    assert seen["argv"][-2:] == ["-c", 'mcp_servers.linear={ command = "npx" }']


def test_run_pass_loads_only_reserved_claude_mcp_config(tmp_path, monkeypatch):
    from franky.config import Config
    from franky.engine import ClaudeEngine

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".claude" / "franky-mcp.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")
    cfg = Config(engine=ClaudeEngine(), allowed_repos=["me/repo"])
    cli._load_profile_bundle(str(prof), {}, [], cfg)
    seen = {}

    def fake_run(_cfg, inner_argv, **_kwargs):
        seen["argv"] = inner_argv
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    cli._run_pass(cfg, "do it", "franky", "franky-proxy")

    assert "--dangerously-skip-permissions" in seen["argv"]
    assert seen["argv"][-3:] == [
        "--mcp-config",
        "/home/franky/.claude/franky-mcp.json",
        "--strict-mcp-config",
    ]


def test_run_pass_passes_configured_effort_to_claude(monkeypatch):
    from franky.config import Config
    from franky.engine import ClaudeEngine

    seen = {}

    def fake_run(_cfg, inner_argv, **_kwargs):
        seen["argv"] = inner_argv
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    cfg = Config(engine=ClaudeEngine(), allowed_repos=["me/repo"], effort="xhigh")
    cli._run_pass(cfg, "do it", "franky", "franky-proxy")
    assert seen["argv"][-2:] == ["--effort", "xhigh"]
    cli._run_pass(Config(engine=ClaudeEngine(), allowed_repos=["me/repo"]), "x", "f", "p")
    assert "--effort" not in seen["argv"]


def test_run_pass_threads_configured_model_to_engine(monkeypatch):
    from franky.config import Config
    from franky.engine import OpenCodeEngine

    cfg = Config(
        engine=OpenCodeEngine(),
        allowed_repos=["me/repo"],
        model="openrouter/anthropic/claude-x",
    )
    seen = {}

    def fake_run(_cfg, inner_argv, **_kwargs):
        seen["argv"] = inner_argv
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    cli._run_pass(cfg, "do it", "franky", "franky-proxy")

    assert seen["argv"][-3:] == ["--model", "openrouter/anthropic/claude-x", "do it"]
    assert seen["argv"] == OpenCodeEngine().inner_argv("do it", cfg.model)


def test_profile_check_secret_hit_nonzero_and_names_file_without_value(tmp_path, monkeypatch):
    leak = tmp_path / "leak.md"
    leak.write_text(f"token {_GHP_FAKE}\n", encoding="utf-8")
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\ninstructions = ["{leak}"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "check"])
    assert res.exit_code != 0
    assert str(leak) in res.output  # offending file named (operator's own file)
    assert _GHP_FAKE not in res.output  # but the secret value is never echoed


def test_profile_check_absent_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(tmp_path / "nope.toml"))
    res = CliRunner().invoke(cli.main, ["profile", "check"])
    assert res.exit_code == 0
    assert "no profile configured" in res.output


def test_profile_check_empty_expansion_shows_declared(tmp_path, monkeypatch):
    empty = tmp_path / "skills"
    empty.mkdir()
    prof = tmp_path / "profile.toml"
    prof.write_text(f'[profile]\nskills = ["{empty}/*.md"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "check"])
    assert res.exit_code == 0, res.output
    assert "no files to inject" in res.output
    assert f"{empty}/*.md" in res.output  # the declared pattern is surfaced


def test_profile_check_bad_toml_nonzero(tmp_path, monkeypatch):
    prof = tmp_path / "profile.toml"
    prof.write_text("[profile\nskills = oops", encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    res = CliRunner().invoke(cli.main, ["profile", "check"])
    assert res.exit_code != 0


def test_profile_init_writes_entered_globs(tmp_path, monkeypatch):
    from franky.profile import read_profile_raw

    prof = tmp_path / "profile.toml"
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    # Hermetic: HOME has no agentic setup dirs, so the setup confirm is never offered and the
    # wizard asks only the three explicit-glob prompts.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wizard_input = "~/.claude/skills/*.md\n~/.claude/CLAUDE.md\n\n"
    res = CliRunner().invoke(cli.main, ["profile", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    raw = read_profile_raw(prof)
    assert raw["skills"] == ["~/.claude/skills/*.md"]
    assert raw["instructions"] == ["~/.claude/CLAUDE.md"]
    assert "knowledge" not in raw


def test_profile_init_offers_detected_setups(tmp_path, monkeypatch):
    """The zero-typing path: a `~/.claude` on this machine becomes a [setups] entry on one y."""
    from franky.profile import read_setups_raw

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    prof = tmp_path / "profile.toml"
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setenv("HOME", str(home))

    res = CliRunner().invoke(cli.main, ["profile", "init"], input="y\n\n\n\n")

    assert res.exit_code == 0, res.output
    assert read_setups_raw(prof) == {"claude": "~/.claude", "codex": "~/.codex"}


def test_profile_init_declining_setups_writes_none(tmp_path, monkeypatch):
    from franky.profile import read_setups_raw

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    prof = tmp_path / "profile.toml"
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setenv("HOME", str(home))

    res = CliRunner().invoke(cli.main, ["profile", "init"], input="n\n~/x.md\n\n\n")

    assert res.exit_code == 0, res.output
    assert read_setups_raw(prof) == {}


def test_profile_init_merges_existing(tmp_path, monkeypatch):
    from franky.profile import read_profile_raw, read_setups_raw

    prof = tmp_path / "profile.toml"
    prof.write_text(
        '[profile]\nskills = ["~/existing.md"]\n'
        'mcp_credentials = ["LINEAR_API_KEY"]\n'
        'mcp_domains = ["mcp.linear.app"]\n'
        '\n[setups]\ncodex = "~/.codex"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wizard_input = "~/new.md\n\n\n"  # add a skill, skip instructions + knowledge
    res = CliRunner().invoke(cli.main, ["profile", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    raw = read_profile_raw(prof)
    assert raw["skills"] == ["~/existing.md", "~/new.md"]
    assert raw["mcp_credentials"] == ["LINEAR_API_KEY"]
    assert raw["mcp_domains"] == ["mcp.linear.app"]
    # An existing setup declaration survives a wizard run that never offered it.
    assert read_setups_raw(prof) == {"codex": "~/.codex"}


def test_config_init_profile_prompt_yes_writes_both(tmp_path, monkeypatch):
    from franky.profile import read_profile_raw
    from franky.userconfig import read_config_file

    cfg_path = tmp_path / "franky-config"
    prof_path = tmp_path / "profile.toml"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof_path))
    # Hermetic HOME: no detected setups, so the profile wizard asks only its glob prompts.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # config init now fails fast in a non-TTY (never-hang, #50); driving the wizard via
    # CliRunner input simulates an interactive session, so mark stdin interactive.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = (
        "pi\n"  # engine
        "me/repo\n"  # repos
        "ghp_wiz\n"  # GH_TOKEN
        "OPENROUTER_API_KEY\n"  # provider choice
        "sk-or-wiz\n"  # provider value
        "n\n"  # JIRA: no
        "y\n"  # profile: yes
        "~/.claude/skills/*.md\n"  # skills
        "~/.claude/CLAUDE.md\n"  # instructions
        "\n"  # knowledge: empty
    )
    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    assert read_config_file(cfg_path)["FRANKY_ENGINE"] == "pi"
    assert read_profile_raw(prof_path)["skills"] == ["~/.claude/skills/*.md"]


def test_config_init_opencode_persists_model_and_openrouter_key(tmp_path, monkeypatch):
    from franky.userconfig import read_config_file

    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = "opencode\nme/repo\nghp_wiz\nopenrouter/anthropic/claude-x\nsk-or-wiz\nn\nn\n"

    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)

    assert res.exit_code == 0, res.output
    config = read_config_file(cfg_path)
    assert config["FRANKY_ENGINE"] == "opencode"
    assert config["FRANKY_MODEL"] == "openrouter/anthropic/claude-x"
    assert config["OPENROUTER_API_KEY"] == "sk-or-wiz"


def test_config_init_opencode_persists_direct_moonshot_key(tmp_path, monkeypatch):
    from franky.userconfig import read_config_file

    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = "opencode\nme/repo\nghp_wiz\nmoonshotai/kimi-k3\nsk-moon-wiz\nn\nn\n"

    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)

    assert res.exit_code == 0, res.output
    config = read_config_file(cfg_path)
    assert config["FRANKY_ENGINE"] == "opencode"
    assert config["FRANKY_MODEL"] == "moonshotai/kimi-k3"
    assert config["MOONSHOT_API_KEY"] == "sk-moon-wiz"
    assert "OPENROUTER_API_KEY" not in config


# ---------------------------------------------------------------------------
# --max-duration timeout (issue #50): a timed-out run maps to status=timeout, exit 9
# ---------------------------------------------------------------------------


def test_build_max_duration_help_shown():
    res = CliRunner().invoke(cli.main, ["build", "--help"])
    assert res.exit_code == 0
    assert "--max-duration" in res.output


def test_build_timeout_maps_to_status_timeout_exit_9(monkeypatch):
    # The container returns the 124 sentinel; build must map it to status=timeout / exit 9,
    # distinct from the generic agent_error (7).
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: (124, "franky: container timed out after 1s")
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--max-duration", "1", "--json"]
        )
    assert res.exit_code == 9, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "timeout"
    assert data["exit_code"] == 9


def test_build_max_duration_threaded_to_container(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["timeout"] = k.get("timeout")
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--max-duration", "42"]
        )
    assert res.exit_code == 0, res.output
    assert seen["timeout"] == 42


def test_build_without_max_duration_uses_container_default(monkeypatch):
    # No --max-duration -> the timeout kwarg is NOT forwarded (run_in_container keeps its own
    # default), so the fake sees no override.
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["has_timeout"] = "timeout" in k
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert seen["has_timeout"] is False


def test_iterate_timeout_maps_to_status_timeout_exit_9(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: (124, "franky: container timed out after 1s")
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL, "--max-duration", "1", "--json"])
    assert res.exit_code == 9, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "timeout"


# ---------------------------------------------------------------------------
# schema command (issue #50)
# ---------------------------------------------------------------------------


def test_schema_command_emits_json():
    res = CliRunner().invoke(cli.main, ["schema"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert "commands" in data
    assert "exit_codes" in data


# ---------------------------------------------------------------------------
# idempotency pre-check (issue #50): already_open short-circuit + --force bypass
# ---------------------------------------------------------------------------


def test_build_already_open_short_circuits(monkeypatch):
    # A Franky PR already open on the predicted branch -> report it, exit 0, NO container run.
    existing = "https://github.com/me/repo/pull/99"
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: existing)

    ran = {"container": False}
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: ran.update(container=True) or (0, "")
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert existing in res.output  # the existing PR URL is echoed on stdout
    assert ran["container"] is False  # no build launched


def test_build_already_open_json_carries_url(monkeypatch):
    existing = "https://github.com/me/repo/pull/99"
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: existing)
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, ""))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["status"] == "already_open"
    assert data["pr_url"] == existing
    assert data["branch"]  # the predicted branch is populated


def test_build_force_bypasses_idempotency_check(monkeypatch):
    # --force must skip find_open_pr entirely and run the build even if a PR is "open".
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    checked = {"n": 0}

    def boom(*a, **k):
        checked["n"] += 1
        return "https://github.com/me/repo/pull/99"

    monkeypatch.setattr(cli, "find_open_pr", boom)
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--force"])
    assert res.exit_code == 0, res.output
    assert PR_URL in res.output  # the build ran normally
    assert checked["n"] == 0  # the idempotency check was never consulted


def test_build_normal_result_populates_predicted_branch(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["branch"] == "franky/do-it"


# ---------------------------------------------------------------------------
# `franky plan` - read-only scope-assessment + decomposition (#52)
# ---------------------------------------------------------------------------

PLAN_NONCE = "feedfacecafe0001"


def _plan_block(payload, nonce=PLAN_NONCE):
    import json as _json

    return f"FRANKY_PLAN_{nonce}_BEGIN{_json.dumps(payload)}FRANKY_PLAN_{nonce}_END"


def _fix_plan_nonce(monkeypatch):
    """Pin the per-run nonce so a fake container can echo a matching sentinel block."""
    monkeypatch.setattr(cli.secrets, "token_hex", lambda *a, **k: PLAN_NONCE)


def test_plan_help_shows_command():
    res = CliRunner().invoke(cli.main, ["plan", "--help"])
    assert res.exit_code == 0
    assert "--engine" in res.output
    assert "read-only" in res.output.lower()


def test_plan_json_emits_one_decomposition_object(monkeypatch):
    _fix_plan_nonce(monkeypatch)
    # Pin the engine so the envelope assertion is deterministic regardless of any host
    # ~/.franky/config (the replaced cli.os.environ does not carry FRANKY_CONFIG_FILE).
    monkeypatch.setattr(cli.os, "environ", {**_build_env(), "FRANKY_ENGINE": "pi"})
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    payload = {
        "fits_one_pr": False,
        "subtasks": [
            {"title": "part one", "summary": "do A", "suggested_repo": "me/repo"},
            {"title": "part two", "summary": "do B", "suggested_repo": "me/repo"},
        ],
        "rationale": "two concerns",
    }
    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        # The prompt is the LAST positional/kw - capture it via the engine argv tail; here we
        # assert the decompose prompt reached the container by checking the inner argv carries
        # the nonce-fenced sentinel instruction.
        seen["argv"] = inner_argv
        return 0, _plan_block(payload)

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "add a big feature", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    # Exactly one JSON object on stdout, nothing else.
    data = json.loads(res.output)
    assert data["fits_one_pr"] is False
    assert [s["title"] for s in data["subtasks"]] == ["part one", "part two"]
    assert data["rationale"] == "two concerns"
    # The envelope fields build_plan_result adds are present and correct.
    assert data["engine"] == "pi"
    assert data["repo"] == "me/repo"
    assert data["exit_code"] == 0
    # The decompose prompt (carrying the nonce sentinel) reached the container, read-only.
    assert any(f"FRANKY_PLAN_{PLAN_NONCE}_BEGIN" in str(a) for a in seen["argv"])
    assert not any("gh pr create" in str(a) for a in seen["argv"])


def test_plan_non_json_prints_prose_summary(monkeypatch):
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    payload = {
        "fits_one_pr": False,
        "subtasks": [{"title": "split this", "summary": "the detail", "suggested_repo": "me/repo"}],
        "rationale": "too big",
    }
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, _plan_block(payload)))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "add a big feature", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert "split this" in res.output
    assert "[me/repo]" in res.output
    assert "too big" in res.output
    # Not JSON on stdout.
    assert "fits_one_pr" not in res.output


def test_plan_json_fits_one_pr_true_happy_path(monkeypatch):
    # The canonical agent-caller path: a task that already fits one PR.
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", {**_build_env(), "FRANKY_ENGINE": "pi"})
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    payload = {
        "fits_one_pr": True,
        "subtasks": [
            {"title": "the whole thing", "summary": "one change", "suggested_repo": "me/repo"}
        ],
        "rationale": "small and focused",
    }
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, _plan_block(payload)))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "tiny fix", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["fits_one_pr"] is True
    assert [s["title"] for s in data["subtasks"]] == ["the whole thing"]
    assert data["exit_code"] == 0


def test_plan_non_json_fits_one_pr_true_prose(monkeypatch):
    # Covers the fits-one-PR verdict line in _format_plan_summary.
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    payload = {"fits_one_pr": True, "subtasks": [], "rationale": "one logical change"}
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, _plan_block(payload)))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "tiny fix", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert "fits ONE focused PR" in res.output
    assert "one logical change" in res.output


def test_plan_no_parseable_plan_exits_7_with_error_object(monkeypatch):
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, "no sentinel here at all"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 7, res.output
    data = json.loads(res.output)
    # The shared error envelope, NOT a partial decomposition.
    assert "error" in data
    assert "fits_one_pr" not in data
    assert data["error"]["kind"] == "no_plan"


def test_plan_agent_error_exits_7(monkeypatch):
    # A nonzero (non-timeout) container exit maps to exit 7 agent_error, NOT no_plan.
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (3, "the agent crashed"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 7, res.output
    data = json.loads(res.output)
    assert "error" in data
    assert data["error"]["kind"] == "agent_error"


def test_plan_timeout_maps_to_exit_9(monkeypatch):
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(
        cli, "run_in_container", lambda *a, **k: (cli.CONTAINER_TIMEOUT_CODE, "timed out")
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["plan", "do it", "--repo", "me/repo", "--max-duration", "1"])
    assert res.exit_code == 9, res.output


def test_plan_off_allowlist_exits_4(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, "x"))

    res = CliRunner().invoke(cli.main, ["plan", "do it", "--repo", "stranger/repo"])
    assert res.exit_code == 4, res.output
    assert "allowlist" in res.output


def test_plan_stdin_reads_task(monkeypatch):
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    payload = {"fits_one_pr": True, "subtasks": [], "rationale": "fits"}
    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["argv"] = inner_argv
        return 0, _plan_block(payload)

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["plan", "-", "--repo", "me/repo", "--json"], input="split this big thing\n"
        )
    assert res.exit_code == 0, res.output
    assert any("split this big thing" in str(a) for a in seen["argv"])


def test_plan_stdin_dash_interactive_fails_fast(monkeypatch):
    # A TTY on `plan -` would block forever - fail fast (exit 2), never hang.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    res = CliRunner().invoke(cli.main, ["plan", "-", "--repo", "me/repo"])
    assert res.exit_code == 2, res.output


def test_build_help_shows_plan_advisory():
    res = CliRunner().invoke(cli.main, ["build", "--help"])
    assert res.exit_code == 0
    assert "franky plan" in res.output


# ---------------------------------------------------------------------------
# build --retry (issue #64 #5) + job diagnose (#4)
# ---------------------------------------------------------------------------


def _retry_env(monkeypatch, container_results, diagnose_result):
    """Wire a build --retry test: creds env, images present, a queue of (code, output) results
    for the build passes, and a stubbed _diagnose (so diagnose never calls run_in_container)."""
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    results = list(container_results)
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return results.pop(0)

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    monkeypatch.setattr(cli, "_diagnose", lambda *a, **k: diagnose_result)
    return calls


def test_build_retry_zero_omits_attempts_field(monkeypatch):
    # --retry 0 (the default) must emit the exact same keys as before - no `attempts`.
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json"])
    assert res.exit_code == 0, res.output
    assert "attempts" not in json.loads(res.stdout)


def test_build_retry_succeeds_on_second_attempt(monkeypatch):
    calls = _retry_env(
        monkeypatch,
        container_results=[(0, "no pr here"), (0, f"opened {PR_URL}")],
        diagnose_result=({"retryable": True, "retry_hint": "run the tests first"}, 0),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "2", "--json"]
        )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "pr_opened" and data["pr_url"] == PR_URL
    assert len(data["attempts"]) == 2
    assert data["attempts"][0]["status"] == "no_pr"
    assert data["attempts"][0]["retry_hint"] == "run the tests first"
    assert data["attempts"][1]["status"] == "pr_opened"
    assert calls["n"] == 2  # two build passes; diagnose is stubbed (no container call)


def test_build_retry_stops_when_diagnosis_not_retryable(monkeypatch):
    calls = _retry_env(
        monkeypatch,
        container_results=[(0, "no pr")],  # only ONE build pass should run
        diagnose_result=({"retryable": False}, 0),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "3", "--json"]
        )
    assert res.exit_code == 7, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "no_pr" and len(data["attempts"]) == 1
    assert calls["n"] == 1  # no retry after a non-retryable diagnosis


def test_build_retry_stops_when_diagnose_fails(monkeypatch):
    # A diagnose pass that itself fails/unparses (returns None) must STOP - never blind-restart.
    calls = _retry_env(
        monkeypatch,
        container_results=[(0, "no pr")],
        diagnose_result=(None, 7),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "3", "--json"]
        )
    assert res.exit_code == 7, res.output
    assert calls["n"] == 1


def test_build_retry_rechecks_idempotency_before_retry(monkeypatch):
    # A "failed" attempt may actually have opened a PR; the pre-retry re-check must catch it and
    # stop (already_open) rather than open a second PR.
    calls = _retry_env(
        monkeypatch,
        container_results=[(0, "no pr")],  # attempt 2 must NOT run
        diagnose_result=({"retryable": True, "retry_hint": "x"}, 0),
    )
    seq = [None, "https://github.com/me/repo/pull/99"]  # pre-loop None, then open at attempt 2
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: seq.pop(0) if seq else None)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "2", "--json"]
        )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "already_open"
    assert data["pr_url"] == "https://github.com/me/repo/pull/99"
    assert calls["n"] == 1  # only the first build pass ran


def test_build_retry_rejects_out_of_range(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "99"])
    assert res.exit_code == 2  # IntRange(max=5) rejects it


def _diag_setup(monkeypatch, tmp_path, container_result, nonce="fixednonce", captured=None):
    env = _build_env()
    env["FRANKY_RUNS_DIR"] = str(tmp_path / "runs")
    env["FRANKY_CONFIG_FILE"] = str(tmp_path / "config")
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))

    def fake_run(*a, **k):
        if captured is not None:
            captured.update(k)
        return container_result

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    monkeypatch.setattr(cli, "_make_nonce", lambda: nonce)
    return env


def _write_failed_run(env, tmp_path, job_id="abcd12"):
    log = tmp_path / f"{job_id}.log"
    log.write_text("the agent failed: tests were red\n", encoding="utf-8")
    rec = jobs.new_record(
        job_id=job_id,
        command="build",
        repo="me/repo",
        engine="pi",
        task="do it",
        container="c",
        network="n",
        proxy="p",
        branch="b",
        started_at="2026-07-05T10:00:00+00:00",
    )
    rec["status"] = "no_pr"
    rec["log_path"] = str(log)
    jobs.write_record(rec, env)
    return job_id


def test_job_diagnose_emits_diagnosis(monkeypatch, tmp_path):
    nonce = "fixednonce"
    diag = (
        '{"root_cause": "tests were red", "category": "test_failure", "retryable": true, '
        '"retry_hint": "make the tests pass first", "confidence": "high"}'
    )
    output = f"analysis...\nFRANKY_DIAG_{nonce}_BEGIN{diag}FRANKY_DIAG_{nonce}_END"
    captured = {}
    env = _diag_setup(monkeypatch, tmp_path, (0, output), nonce, captured)
    job_id = _write_failed_run(env, tmp_path)
    res = CliRunner().invoke(cli.main, ["job", "diagnose", job_id, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["root_cause"] == "tests were red"
    assert data["category"] == "test_failure" and data["retryable"] is True
    assert data["job_id"] == job_id
    assert captured["image"].endswith(f":{__version__}-pi")


def test_job_diagnose_json_quiet_announces_its_job_id(monkeypatch, tmp_path):
    nonce = "fixednonce"
    diag = '{"root_cause": "x", "category": "other", "retryable": false, "confidence": "low"}'
    output = f"FRANKY_DIAG_{nonce}_BEGIN{diag}FRANKY_DIAG_{nonce}_END"
    env = _diag_setup(monkeypatch, tmp_path, (0, output), nonce, {})
    job_id = _write_failed_run(env, tmp_path)
    res = CliRunner().invoke(cli.main, ["job", "diagnose", job_id, "--json", "--quiet"])
    assert res.exit_code == 0, res.output
    event = json.loads(res.stderr.strip().splitlines()[-1])
    assert event["event"] == "started" and event["command"] == "diagnose"
    assert event["job_id"] != job_id and json.loads(res.stdout)["job_id"] == job_id


def test_job_diagnose_not_found_exits_2(monkeypatch, tmp_path):
    env = _build_env()
    env["FRANKY_RUNS_DIR"] = str(tmp_path / "runs")
    monkeypatch.setattr(cli.os, "environ", env)
    res = CliRunner().invoke(cli.main, ["job", "diagnose", "abcdef"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_diagnose_no_transcript_exits_2(monkeypatch, tmp_path):
    env = _build_env()
    env["FRANKY_RUNS_DIR"] = str(tmp_path / "runs")
    monkeypatch.setattr(cli.os, "environ", env)
    rec = jobs.new_record(
        job_id="beef01",
        command="build",
        repo="me/repo",
        engine="pi",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch="b",
        started_at="2026-07-05T10:00:00+00:00",
    )  # status running, log_path "" -> no transcript
    jobs.write_record(rec, env)
    res = CliRunner().invoke(cli.main, ["job", "diagnose", "beef01"])
    assert res.exit_code == 2
    assert "no transcript" in res.stderr


def test_job_diagnose_unparseable_exits_7(monkeypatch, tmp_path):
    env = _diag_setup(monkeypatch, tmp_path, (0, "blah blah no sentinel"))
    job_id = _write_failed_run(env, tmp_path, job_id="c0de01")
    res = CliRunner().invoke(cli.main, ["job", "diagnose", job_id, "--json"])
    assert res.exit_code == 7, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "no_diagnosis"


def test_build_retry_exhausts_budget(monkeypatch):
    # Both attempts fail retryably and the loop ends on the budget guard (not a non-retryable
    # diagnosis): the final failure is reported and no third build/diagnose runs.
    calls = _retry_env(
        monkeypatch,
        container_results=[(0, "no pr"), (0, "still no pr")],
        diagnose_result=({"retryable": True, "retry_hint": "try harder"}, 0),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(
            cli.main, ["build", "do it", "--repo", "me/repo", "--retry", "1", "--json"]
        )
    assert res.exit_code == 7, res.output  # last failure = no_pr = EXIT_AGENT
    data = json.loads(res.stdout)
    assert data["status"] == "no_pr"
    assert len(data["attempts"]) == 2  # both attempts ran; budget exhausted
    assert data["attempts"][0]["retry_hint"] == "try harder"
    assert data["attempts"][1]["retry_hint"] == ""  # last attempt is never diagnosed
    assert calls["n"] == 2  # exactly two build passes, no third


def test_job_diagnose_timeout_exits_9(monkeypatch, tmp_path):
    # A diagnose pass that itself times out -> exit 9 (the documented timeout path).
    env = _diag_setup(monkeypatch, tmp_path, (cli.CONTAINER_TIMEOUT_CODE, ""))
    job_id = _write_failed_run(env, tmp_path, job_id="d00d01")
    res = CliRunner().invoke(cli.main, ["job", "diagnose", job_id, "--json"])
    assert res.exit_code == 9, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "timeout"


# ---------------------------------------------------------------------------
# `franky job replay` (issue #70)
# ---------------------------------------------------------------------------


def _write_replayable_run(
    env,
    tmp_path,
    job_id="feed01",
    command="build",
    source="prose",
    task_full="do it",
    base_sha="abc1234",
    branch="franky/task",
):
    rec = jobs.new_record(
        job_id=job_id,
        command=command,
        repo="me/repo",
        engine="pi",
        task="do it",
        container="c",
        network="n",
        proxy="p",
        branch=branch,
        started_at="2026-07-05T10:00:00+00:00",
        source=source,
        task_full=task_full,
        base_sha=base_sha,
    )
    rec["status"] = "pr_opened"
    jobs.write_record(rec, env)
    return job_id


def _replay_env(monkeypatch, tmp_path, container_result, allowed_repos="me/repo", captured=None):
    env = {
        "FRANKY_ALLOWED_REPOS": allowed_repos,
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_CONFIG_FILE": str(tmp_path / "config"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))

    def fake_run(*a, **k):
        if captured is not None:
            captured.update(k)
        return container_result

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    monkeypatch.setattr(cli.baseref, "commit_exists", lambda *a, **k: True)
    return env


def test_job_replay_reproduce_only_happy_path(monkeypatch, tmp_path):
    captured = {}
    env = _replay_env(monkeypatch, tmp_path, (0, "reproduced the failure"), captured=captured)
    job_id = _write_replayable_run(env, tmp_path)
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "replay_complete"
    assert data["replay_of"] == job_id
    assert data["pr_url"] is None
    new_id = data["job_id"]
    rec = jobs.read_record(new_id, env)
    assert rec["command"] == "replay"
    assert rec["replay_of"] == job_id
    assert rec["base_sha"] == "abc1234"
    assert captured["image"].endswith(f":{__version__}-pi")


def test_job_replay_engine_flag_uses_new_engine_image(monkeypatch, tmp_path):
    captured = {}
    env = _replay_env(monkeypatch, tmp_path, (0, "reproduced"), captured=captured)
    env.pop("OPENROUTER_API_KEY")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-fake"
    job_id = _write_replayable_run(env, tmp_path, job_id="c1055e01")

    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--engine", "claude", "--json"])

    assert res.exit_code == 0, res.output
    assert captured["image"].endswith(f":{__version__}-claude")


def test_job_replay_open_pr_opens_pr(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"))
    job_id = _write_replayable_run(env, tmp_path, job_id="aaaa0001")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--open-pr", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "pr_opened"
    assert data["pr_url"] == PR_URL
    assert data["replay_of"] == job_id


def test_job_replay_missing_base_sha_exits_2(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_replayable_run(env, tmp_path, job_id="bad00001", base_sha=None)
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "base_sha_unavailable"


def test_job_replay_base_commit_gone_exits_2(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "x"))
    monkeypatch.setattr(cli.baseref, "commit_exists", lambda *a, **k: False)
    job_id = _write_replayable_run(env, tmp_path, job_id="deaf0001")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "base_commit_gone"


def test_job_replay_not_replayable_command_exits_2(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_replayable_run(env, tmp_path, job_id="deadbeef", command="diagnose")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "not_replayable"


def test_job_replay_missing_task_full_exits_2(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_replayable_run(env, tmp_path, job_id="face0001", task_full=None, source=None)
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "replay_inputs_missing"


def test_job_replay_off_allowlist_now_exits_4(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "x"), allowed_repos="other/repo")
    job_id = _write_replayable_run(env, tmp_path, job_id="facade01")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 4, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "task_rejected"


def test_job_replay_not_found_exits_2(monkeypatch, tmp_path):
    _replay_env(monkeypatch, tmp_path, (0, "x"))
    res = CliRunner().invoke(cli.main, ["job", "replay", "nosuchjob"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_replay_timeout_exits_9_and_prints_no_complete_line(monkeypatch, tmp_path):
    # A reproduce-only replay that TIMES OUT must exit 9 (status timeout) and, in non-JSON mode,
    # must NOT print a "complete" line (the failure is carried by the exit code + the log).
    env = _replay_env(monkeypatch, tmp_path, (cli.CONTAINER_TIMEOUT_CODE, ""))
    job_id = _write_replayable_run(env, tmp_path, job_id="0ad00001")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id])
    assert res.exit_code == 9, res.output
    assert "complete" not in res.stderr
    rec = jobs.read_record(job_id, env)  # original record is untouched
    assert rec["status"] == "pr_opened"


def test_job_replay_agent_error_exits_7(monkeypatch, tmp_path):
    # A nonzero, non-timeout container exit -> agent_error, exit 7.
    env = _replay_env(monkeypatch, tmp_path, (3, "the agent crashed"))
    job_id = _write_replayable_run(env, tmp_path, job_id="0ad00002")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 7, res.output
    assert json.loads(res.stdout)["status"] == "agent_error"


def test_job_replay_open_pr_no_pr_url_exits_7(monkeypatch, tmp_path):
    # An --open-pr replay whose clean-exit output carries no PR url -> no_pr, exit 7.
    env = _replay_env(monkeypatch, tmp_path, (0, "did work but never opened a PR"))
    job_id = _write_replayable_run(env, tmp_path, job_id="0ad00003")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--open-pr", "--json"])
    assert res.exit_code == 7, res.output
    assert json.loads(res.stdout)["status"] == "no_pr"


def test_job_replay_persists_diagnostics(monkeypatch, tmp_path):
    # Replay is the debugging command; its record MUST carry the captured runtime diagnostics.
    # Fake run_in_container fills the injected sink (the real one does so before teardown, #69).
    env = _replay_env(monkeypatch, tmp_path, (0, "reproduced"))

    def fake_run(*a, **k):
        sink = k.get("diagnostics_sink")
        if sink is not None:
            sink.update({"task_exit_code": 0, "oom_killed": False, "dind_ready": True})
        return (0, "reproduced")

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    job_id = _write_replayable_run(env, tmp_path, job_id="0ad00004")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 0, res.output
    new_id = json.loads(res.stdout)["job_id"]
    rec = jobs.read_record(new_id, env)
    assert rec["diagnostics"] == {
        "task_exit_code": 0,
        "oom_killed": False,
        "dind_ready": True,
    }


def test_job_replay_malformed_base_sha_exits_2(monkeypatch, tmp_path):
    # A truthy-but-junk base_sha (corrupt/hand-edited record) is refused before it reaches the
    # prompt or commit_exists' None-on-uncertain path.
    env = _replay_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_replayable_run(env, tmp_path, job_id="0ad00005", base_sha="not-a-sha!!")
    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "base_sha_unavailable"


# ---------------------------------------------------------------------------
# `franky job resume` (issue #71)
# ---------------------------------------------------------------------------


def _write_resumable_run(
    env,
    tmp_path,
    job_id="fee01",
    command="build",
    source="prose",
    task_full="do it",
    base_sha="abc1234",
    branch="franky/task",
    with_snapshot=True,
    snapshot_bytes=None,
):
    import tarfile as _tarfile

    rec = jobs.new_record(
        job_id=job_id,
        command=command,
        repo="me/repo",
        engine="pi",
        task="do it",
        container="c",
        network="n",
        proxy="p",
        branch=branch,
        started_at="2026-07-05T10:00:00+00:00",
        source=source,
        task_full=task_full,
        base_sha=base_sha,
    )
    rec["status"] = "timeout"
    jobs.write_record(rec, env)
    if with_snapshot:
        snap_path = jobs.runs_dir(env) / f"{job_id}.snapshot.tar.gz"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot_bytes is not None:
            snap_path.write_bytes(snapshot_bytes)
        else:
            # A real (empty) gzip tar so tarfile.open(r:gz) succeeds.
            with _tarfile.open(snap_path, "w:gz"):
                pass
    return job_id


def _resume_env(monkeypatch, tmp_path, container_result, allowed_repos="me/repo", captured=None):
    env = {
        "FRANKY_ALLOWED_REPOS": allowed_repos,
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_CONFIG_FILE": str(tmp_path / "config"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)

    def fake_run(*a, **k):
        if captured is not None:
            captured.update(k)
        return container_result

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    return env


def test_job_resume_no_snapshot_exits_2(monkeypatch, tmp_path):
    env = _resume_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_resumable_run(env, tmp_path, job_id="beef01", with_snapshot=False)
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "no_snapshot"


def test_job_resume_corrupt_snapshot_exits_2(monkeypatch, tmp_path):
    env = _resume_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_resumable_run(env, tmp_path, job_id="beef02", snapshot_bytes=b"not a gzip tar")
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "snapshot_corrupt"


def test_job_resume_happy_path(monkeypatch, tmp_path):
    captured = {}
    env = _resume_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"), captured=captured)
    job_id = _write_resumable_run(env, tmp_path, job_id="beef03")
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "pr_opened"
    assert data["pr_url"] == PR_URL
    assert data["resumed_from"] == job_id
    new_id = data["job_id"]
    rec = jobs.read_record(new_id, env)
    assert rec["command"] == "resume"
    assert rec["resumed_from"] == job_id
    # resume_workspace was threaded through to run_in_container, pointing at the snapshot tar.
    assert captured["resume_workspace"] == str(jobs.runs_dir(env) / f"{job_id}.snapshot.tar.gz")
    assert captured["image"].endswith(f":{__version__}-pi")


def test_job_resume_off_allowlist_now_exits_4(monkeypatch, tmp_path):
    env = _resume_env(monkeypatch, tmp_path, (0, "x"), allowed_repos="other/repo")
    job_id = _write_resumable_run(env, tmp_path, job_id="beef04")
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 4, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "task_rejected"


def test_job_resume_idempotency_short_circuit(monkeypatch, tmp_path):
    env = _resume_env(monkeypatch, tmp_path, (0, "x"))
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: PR_URL)
    job_id = _write_resumable_run(env, tmp_path, job_id="beef05")
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "already_open"
    assert data["pr_url"] == PR_URL
    assert data["resumed_from"] == job_id


def test_job_resume_missing_task_full_exits_2(monkeypatch, tmp_path):
    env = _resume_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_resumable_run(env, tmp_path, job_id="beef06", task_full=None, source=None)
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "replay_inputs_missing"


def test_job_resume_not_found_exits_2(monkeypatch, tmp_path):
    _resume_env(monkeypatch, tmp_path, (0, "x"))
    res = CliRunner().invoke(cli.main, ["job", "resume", "0badcafe"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_resume_not_resumable_command_exits_2(monkeypatch, tmp_path):
    # An iterate/diagnose run has no resumable workspace; the scope gate must win over no_snapshot.
    env = _resume_env(monkeypatch, tmp_path, (0, "x"))
    job_id = _write_resumable_run(
        env, tmp_path, job_id="beef07", command="iterate", with_snapshot=False
    )
    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "not_resumable"


# ---------------------------------------------------------------------------
# `franky job attach` (issue #72) - inject a mid-run correction via the steer-file mailbox
# ---------------------------------------------------------------------------


def _write_attachable_run(env, job_id="a77ac0", engine="pi", status="running", command="build"):
    rec = jobs.new_record(
        job_id=job_id,
        command=command,
        repo="me/repo",
        engine=engine,
        task="do it",
        container=f"franky-run-{job_id}",
        network=f"franky-net-{job_id}",
        proxy=f"franky-proxy-{job_id}",
        branch="franky/task",
        started_at="2026-07-05T10:00:00+00:00",
    )
    rec["status"] = status
    jobs.write_record(rec, env)
    return job_id


def _attach_env(monkeypatch, tmp_path, *, alive=True, delivered=True, captured=None):
    # FRANKY_CONFIG_FILE points at a nonexistent path so the config-file merge job_attach now does
    # is a hermetic no-op (never reads the real ~/.franky/config on the dev machine).
    env = {
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_CONFIG_FILE": str(tmp_path / "no-config"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "container_running", lambda name: alive)

    def fake_deliver(container, message, *a, **k):
        if captured is not None:
            captured["container"] = container
            captured["message"] = message
        return delivered

    monkeypatch.setattr(cli, "deliver_steer", fake_deliver)
    return env


def test_job_attach_happy_path_json(monkeypatch, tmp_path):
    captured = {}
    env = _attach_env(monkeypatch, tmp_path, captured=captured)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(
        cli.main, ["job", "attach", job_id, "-m", "stop refactoring, fix the test", "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data == {"job_id": job_id, "delivered": True, "engine": "pi", "kind": "steered"}
    assert captured["container"] == f"franky-run-{job_id}"
    # The DELIVERED payload is framed (operator-correction banner); the AUDIT note stores the
    # bare (redacted) message, not the framing.
    assert "operator correction" in captured["message"]
    assert "stop refactoring, fix the test" in captured["message"]
    rec = jobs.read_record(job_id, env)
    assert rec["steer_notes"] == [{"message": "stop refactoring, fix the test", "delivered": True}]


def test_job_attach_happy_path_text(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "do X instead"])
    assert res.exit_code == 0, res.output
    assert "correction delivered" in res.output


def test_job_attach_run_not_alive_exits_2(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path, alive=False)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "run_not_alive"


def test_job_attach_unsupported_engine_exits_2(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env, engine="pi")
    # Monkeypatch the PiEngine class' flag off for this one test so we exercise the
    # unsupported-engine branch without inventing a fake ENGINES entry.
    import franky.engine as engine_mod

    monkeypatch.setattr(engine_mod.PiEngine, "supports_steering", False)
    monkeypatch.setattr(cli, "ENGINES", {"pi": engine_mod.PiEngine})
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "steering_unsupported"


def test_job_attach_unknown_engine_exits_2(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env, engine="some-future-engine")
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "unknown_engine"


def test_job_attach_no_message_non_tty_exits_2(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "interactive_input_required"


def test_job_attach_no_message_tty_prompts(monkeypatch, tmp_path):
    captured = {}
    env = _attach_env(monkeypatch, tmp_path, captured=captured)
    job_id = _write_attachable_run(env)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: "typed correction")
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "--json"])
    assert res.exit_code == 0, res.output
    assert "typed correction" in captured["message"]


def test_job_attach_redacts_seeded_secret(monkeypatch, tmp_path):
    captured = {}
    env = {**_attach_env(monkeypatch, tmp_path, captured=captured), "GH_TOKEN": "ghp_supersecret"}
    monkeypatch.setattr(cli.os, "environ", env)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(
        cli.main, ["job", "attach", job_id, "-m", "use ghp_supersecret to auth", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert "ghp_supersecret" not in captured["message"]
    assert "ghp_supersecret" not in res.output


def test_job_attach_delivery_fails_then_run_ended_is_run_not_alive(monkeypatch, tmp_path):
    env = {
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_CONFIG_FILE": str(tmp_path / "no-config"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    # Alive on the FIRST check (so we get past the pre-check), gone by the time we re-check
    # after a failed deliver - the race deliver_steer's caller must classify as run_not_alive.
    calls = {"n": 0}

    def flaky_alive(name):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(cli, "container_running", flaky_alive)
    monkeypatch.setattr(cli, "deliver_steer", lambda *a, **k: False)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "run_not_alive"


def test_job_attach_delivery_failed_still_running_exits_steer_delivery_failed(
    monkeypatch, tmp_path
):
    env = _attach_env(monkeypatch, tmp_path, delivered=False)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "steer_delivery_failed"
    # Best-effort annotation of the failed attempt.
    rec = jobs.read_record(job_id, env)
    assert rec["steer_notes"][-1]["delivered"] is False


def test_job_attach_not_found_exits_2(monkeypatch, tmp_path):
    _attach_env(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli.main, ["job", "attach", "0badcafe", "-m", "hi"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_attach_empty_message_exits_2(monkeypatch, tmp_path):
    # A message WAS supplied, just blank - distinct kind from interactive_input_required.
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "   ", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "empty_message"


def test_job_attach_not_steerable_command_exits_2(monkeypatch, tmp_path):
    # A diagnose run's prompt never carries the steer convention, so attaching to it would be a
    # misleading "delivered" - the steerability gate must reject it up front.
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env, command="diagnose")
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", "hi", "--json"])
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "not_steerable"


def test_job_attach_steer_notes_capped_and_truncated(monkeypatch, tmp_path):
    env = _attach_env(monkeypatch, tmp_path)
    job_id = _write_attachable_run(env)
    # Seed the record with a full window of prior notes so a single fresh attach must evict the
    # oldest (cap = 20, newest kept).
    seed = [{"message": f"note {i}", "delivered": True} for i in range(20)]
    jobs.update_record(job_id, {"steer_notes": seed}, env)
    long_msg = "x" * 600  # > _STEER_NOTE_MSG_MAX (500)
    res = CliRunner().invoke(cli.main, ["job", "attach", job_id, "-m", long_msg, "--json"])
    assert res.exit_code == 0, res.output
    notes = jobs.read_record(job_id, env)["steer_notes"]
    assert len(notes) == 20  # capped
    assert notes[0]["message"] == "note 1"  # oldest ("note 0") evicted
    assert notes[-1]["message"] == "x" * 500  # newest, truncated to 500 chars


def test_job_attach_redacts_config_file_only_secret(monkeypatch, tmp_path):
    # A secret stored ONLY in ~/.franky/config (never exported to env) must still be redacted -
    # job_attach merges the config file into env before building the secret list (fix #1).
    from franky.userconfig import write_config_file

    cfg_path = tmp_path / "franky-config"
    write_config_file(cfg_path, {"GH_TOKEN": "ghp_fileonly_secret"})
    captured = {}
    env = {
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_CONFIG_FILE": str(cfg_path),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "container_running", lambda name: True)

    def fake_deliver(container, message, *a, **k):
        captured["message"] = message
        return True

    monkeypatch.setattr(cli, "deliver_steer", fake_deliver)
    job_id = _write_attachable_run(env)
    res = CliRunner().invoke(
        cli.main, ["job", "attach", job_id, "-m", "auth with ghp_fileonly_secret", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert "ghp_fileonly_secret" not in captured["message"]
    assert "ghp_fileonly_secret" not in res.output
    rec = jobs.read_record(job_id, env)
    assert "ghp_fileonly_secret" not in json.dumps(rec["steer_notes"])


# ---------------------------------------------------------------------------
# `franky review-pr` - independent, read-only PR review (bridge backend)
# ---------------------------------------------------------------------------
# Reuses the module-level PR_URL (https://github.com/me/repo/pull/11) - me/repo is allowlisted.

REVIEW_NONCE = "feedfacecafe0002"
REVIEW_PR_NUMBER = 11
LIVE_SHA = "a" * 40
REVIEW_URL = f"{PR_URL}#pullrequestreview-555"


def _review_env():
    return {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def _fix_review_nonce(monkeypatch):
    """Pin the per-run nonce so a fake container can echo a matching sentinel block."""
    monkeypatch.setattr(cli.secrets, "token_hex", lambda *a, **k: REVIEW_NONCE)


def _review_block(payload, nonce=REVIEW_NONCE):
    return f"FRANKY_REVIEW_{nonce}_BEGIN{json.dumps(payload)}FRANKY_REVIEW_{nonce}_END"


class _GhCalls(list):
    """POST calls (the list itself) plus the files fetches and the stdin bodies."""

    def __init__(self):
        super().__init__()
        self.files = []
        self.inputs = []


def _mc_review_setup(
    monkeypatch,
    *,
    env=None,
    live_sha=LIVE_SHA,
    recheck_sha=None,
    container=(0, None),
    gh=(0, None),
    review_files=None,
    later_sha=None,
):
    """Wire a hermetic review-pr: env, images present, live-head fetch + container + gh api all
    mocked. `recheck_sha` defaults to `live_sha` (head unchanged); pass a different value to
    simulate the PR moving between the pre-run pin and the pre-publish recheck.
    """
    env = dict(env) if env is not None else _review_env()
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    shas = [live_sha, recheck_sha if recheck_sha is not None else live_sha]
    seen_gh_calls = _GhCalls()

    def fake_fetch(repo, number, e, **k):
        if later_sha is not None and seen_gh_calls.files:
            return later_sha
        return shas.pop(0) if shas else (recheck_sha if recheck_sha is not None else live_sha)

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fake_fetch)

    code, output = container
    if output is None:
        output = _review_block({"summary": "looks fine", "findings": [], "checks": []})
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (code, output))

    gcode, gout = gh
    if gout is None:
        gout = json.dumps({"html_url": REVIEW_URL, "id": 555})

    def fake_run_gh(args, e, **k):
        if "/files" in args[1]:
            seen_gh_calls.files.append(list(args))
            return 0, json.dumps([review_files or []]), ""
        seen_gh_calls.append(list(args))
        seen_gh_calls.inputs.append(k.get("input"))
        return gcode, gout, ""

    monkeypatch.setattr(cli, "run_gh", fake_run_gh)
    return seen_gh_calls


def test_review_pr_help_shows_no_publish_flag():
    res = CliRunner().invoke(cli.main, ["review-pr", "--help"])
    assert res.exit_code == 0
    assert "--no-publish" in res.output
    assert "--expected-head-sha" in res.output
    assert "--instructions-file" in res.output


def test_review_pr_reads_private_instructions_from_owner_only_file(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env={**_review_env(), "FRANKY_RUNS_DIR": str(tmp_path / "runs")})
    private_text = "Review with this private acceptance criterion."
    source = tmp_path / "instructions"
    source.write_text(private_text)
    source.chmod(0o600)
    seen = {}
    original = cli.build_review_pr_prompt

    def capture(repo, pr_url, instructions, nonce, **kwargs):
        seen["instructions"] = instructions
        return original(repo, pr_url, instructions, nonce, **kwargs)

    monkeypatch.setattr(cli, "build_review_pr_prompt", capture)

    def fake_run(_cfg, inner_argv, **kwargs):
        seen["argv"] = inner_argv
        seen["private_prompt_tar"] = kwargs.get("private_prompt_tar")
        return 0, _review_block({"summary": "ok", "findings": [], "checks": []})

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", "--instructions-file", str(source), "--no-publish", "--json", "--", PR_URL],
    )
    assert res.exit_code == 0, res.output
    assert seen["instructions"] == private_text
    assert private_text not in res.output
    assert private_text not in " ".join(seen["argv"])
    with tarfile.open(fileobj=io.BytesIO(seen["private_prompt_tar"]), mode="r:gz") as archive:
        prompt = archive.extractfile("franky-private-prompt").read().decode()
    assert private_text in prompt


@pytest.mark.parametrize("kind", ["symlink", "fifo", "group_readable", "oversized", "invalid_utf8"])
def test_review_pr_refuses_unsafe_instructions_file(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(cli.os, "environ", _review_env())
    source = tmp_path / "instructions"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("private")
        target.chmod(0o600)
        source.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(source, 0o600)
    elif kind == "invalid_utf8":
        source.write_bytes(b"\xff")
        source.chmod(0o600)
    else:
        source.write_text("x" * (4001 if kind == "oversized" else 1))
        source.chmod(0o640 if kind == "group_readable" else 0o600)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", "--instructions-file", str(source), "--no-publish", "--json", "--", PR_URL],
    )
    assert res.exit_code == 2
    assert str(source) not in res.output


def test_review_pr_refuses_file_and_inline_instructions_together(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.os, "environ", _review_env())
    source = tmp_path / "instructions"
    source.write_text("private")
    source.chmod(0o600)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", "--instructions-file", str(source), "--json", "--", PR_URL, "inline"],
    )
    assert res.exit_code == 2


@pytest.mark.parametrize("flags", [[], ["--no-publish", "--thread"], ["--no-publish", "--verbose"]])
def test_review_pr_private_file_requires_unpublished_unthreaded_quiet_run(
    monkeypatch, tmp_path, flags
):
    monkeypatch.setattr(cli.os, "environ", _review_env())
    source = tmp_path / "instructions"
    source.write_text("private")
    source.chmod(0o600)
    res = CliRunner().invoke(
        cli.main, ["review-pr", "--instructions-file", str(source), *flags, "--json", "--", PR_URL]
    )
    assert res.exit_code == 2


def test_review_pr_published_echoes_review_url_and_never_approves(monkeypatch):
    """Contract: a clean run publishes exactly one COMMENT/REQUEST_CHANGES review (never
    APPROVE) and reports its URL - both on stdout and in the --json result."""
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL])
    assert res.exit_code == 0, res.output
    assert REVIEW_URL in res.output  # bare review URL on stdout, mirrors a bare PR URL on build
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "api"
    assert args[3] == f"repos/me/repo/pulls/{REVIEW_PR_NUMBER}/reviews"
    assert json.loads(calls.inputs[0])["event"] == "COMMENT"
    assert "APPROVE" not in " ".join(args) + calls.inputs[0]  # flags off: never an APPROVE


def test_review_pr_json_success_reports_reviewed_sha_and_review_url(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(
        monkeypatch,
        container=(
            0,
            _review_block(
                {
                    "summary": "solid change",
                    "findings": [
                        {"title": "nit: naming", "body": "minor", "severity": "nit"},
                    ],
                    "checks": [{"name": "pytest", "outcome": "pass", "detail": "120 passed"}],
                }
            ),
        ),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "review_published"
    assert data["reviewed_sha"] == LIVE_SHA
    assert data["review_url"] == REVIEW_URL
    assert data["review_id"] == 555
    assert "review_body" not in data
    assert data["findings_summary"] == "solid change"
    assert data["checks"] == [{"name": "pytest", "outcome": "pass", "detail": "120 passed"}]
    assert data["repo"] == "me/repo"


def test_review_pr_blocking_finding_requests_changes_never_approve(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(
        monkeypatch,
        container=(
            0,
            _review_block(
                {
                    "summary": "found a real bug",
                    "findings": [
                        {
                            "title": "SQL injection",
                            "body": "unsanitized input reaches the query",
                            "severity": "blocking",
                            "file": "app.py",
                            "line": 42,
                        }
                    ],
                    "checks": [],
                }
            ),
        ),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "review_published"
    assert json.loads(calls.inputs[0])["event"] == "REQUEST_CHANGES"
    assert "APPROVE" not in " ".join(calls[0]) + calls.inputs[0]


def test_review_pr_no_publish_makes_zero_github_writes(monkeypatch):
    """publish=false (--no-publish) must never call the GitHub review API."""
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(
        monkeypatch,
        container=(
            0,
            _review_block(
                {
                    "summary": "private summary",
                    "findings": [
                        {
                            "title": "wrong total",
                            "body": "recalculate tax",
                            "severity": "blocking",
                            "file": "fare.py",
                            "line": 7,
                        }
                    ],
                    "checks": [{"name": "pytest", "outcome": "pass", "detail": "12 passed"}],
                }
            ),
        ),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "review_complete"
    assert "review_url" not in data
    assert "review_id" not in data
    assert data["review_body"] == (
        "private summary\n\n- **Blocking:** wrong total (fare.py:7) - recalculate tax\n\n"
        "<sub>Automated review by Franky</sub>"
    )
    assert calls == []  # zero GitHub writes


def test_review_pr_no_publish_refuses_oversized_review_body(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(
        monkeypatch,
        container=(
            0,
            _review_block(
                {
                    "summary": "s",
                    "findings": [{"title": f"t{i}", "body": "x" * 1500} for i in range(8)],
                    "checks": [],
                }
            ),
        ),
    )
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "agent_error"
    assert "review_body" not in data
    assert calls == []


def test_review_pr_no_publish_refuses_too_many_findings(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(
        monkeypatch,
        container=(
            0,
            _review_block(
                {
                    "summary": "many findings",
                    "findings": [
                        {"title": f"issue {n}", "body": "bad", "severity": "normal"}
                        for n in range(11)
                    ],
                    "checks": [],
                }
            ),
        ),
    )
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    assert res.exit_code == 7
    assert json.loads(res.stdout)["status"] == "agent_error"


def test_review_pr_never_invokes_commit_push_merge(monkeypatch):
    """The prompt handed to the agent explicitly FORBIDS commit/push/merge/approve/resolve (an
    imperative prohibition, not merely a mention), and Franky's own host-side code performs
    exactly one GitHub write - the review POST - never a merge/close/approve/dismiss call."""
    _fix_review_nonce(monkeypatch)
    seen = {}
    gh_calls = []

    def fake_run(cfg, inner_argv, *a, **k):
        seen["argv"] = inner_argv
        return 0, _review_block({"summary": "ok", "findings": [], "checks": []})

    def fake_run_gh(a, e, **k):
        if a[1].endswith("/files"):
            return 0, "[]", ""
        gh_calls.append(list(a))
        return 0, json.dumps({"html_url": REVIEW_URL, "id": 1}), ""

    env = _review_env()
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "fetch_pr_head_sha", lambda *a, **k: LIVE_SHA)
    monkeypatch.setattr(cli, "run_in_container", fake_run)
    monkeypatch.setattr(cli, "run_gh", fake_run_gh)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL])
    assert res.exit_code == 0, res.output

    # The prohibition must be an explicit imperative, not just a mention of the word.
    prompt_text = str(seen["argv"])
    for forbidden in (
        "do NOT `git push`",
        "do NOT create, merge, or close any branch or PR",
        "do NOT run `gh pr review`, `gh pr merge`, `gh pr close`",
    ):
        assert forbidden in prompt_text, f"prompt must explicitly forbid: {forbidden!r}"
    assert "read-only for this entire pass" in prompt_text

    # Franky's own host code performs exactly ONE GitHub write - the review POST - never a
    # merge/close/approve/dismiss call. The only other gh call is the read of the PR files.
    gh_calls = [c for c in gh_calls if "/files" not in c[1]]
    assert len(gh_calls) == 1
    assert gh_calls[0][3] == f"repos/me/repo/pulls/{REVIEW_PR_NUMBER}/reviews"
    joined = " ".join(gh_calls[0]).lower()
    for forbidden in ("merge", "close", "approve", "dismiss"):
        assert forbidden not in joined


def test_review_pr_expected_head_mismatch_refuses_before_any_container_run(monkeypatch):
    """Requirement: reject an expected-head mismatch, and never even start the container."""
    env = _review_env()
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "fetch_pr_head_sha", lambda *a, **k: LIVE_SHA)

    def boom(*a, **k):
        raise AssertionError("the container must never run on a head-SHA mismatch")

    monkeypatch.setattr(cli, "ensure_image_available", boom)
    monkeypatch.setattr(cli, "run_in_container", boom)
    monkeypatch.setattr(cli, "run_gh", boom)

    stale_sha = "b" * 40
    res = CliRunner().invoke(
        cli.main, ["review-pr", "--expected-head-sha", stale_sha, "--json", "--", PR_URL]
    )
    assert res.exit_code == 4, res.output  # EXIT_TASK_REJECTED
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 4
    assert data["error"]["kind"] == "head_changed"
    assert "head" in data["error"]["message"].lower()


def test_review_pr_head_change_blocks_stale_publication(monkeypatch):
    """Requirement: the head moving between the pre-run pin and the pre-publish recheck must
    block publishing - the review still ran, but nothing is written to GitHub."""
    _fix_review_nonce(monkeypatch)
    moved_sha = "c" * 40
    calls = _mc_review_setup(monkeypatch, live_sha=LIVE_SHA, recheck_sha=moved_sha)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 7, res.output  # EXIT_AGENT
    data = json.loads(res.stdout)
    assert data["status"] == "publish_blocked_stale_head"
    assert data["reviewed_sha"] == LIVE_SHA
    assert "review_url" not in data
    assert calls == []  # never posted despite publish defaulting True


_ANCHOR_FILES = [
    {"filename": "app.py", "status": "modified", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c"}
]


def _inline_review(findings=None):
    f = findings or [
        {"title": "Guard null", "body": "NPE", "severity": "blocking", "file": "app.py", "line": 2}
    ]
    return (0, _review_block({"summary": "risky", "findings": f, "checks": []}))


def test_review_pr_posts_inline_comments_on_pinned_commit(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES)
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    assert calls.files == [
        [
            "api",
            f"repos/me/repo/pulls/{REVIEW_PR_NUMBER}/files?per_page=100",
            "--paginate",
            "--slurp",
        ]
    ]
    assert calls[0] == [
        "api",
        "-X",
        "POST",
        f"repos/me/repo/pulls/{REVIEW_PR_NUMBER}/reviews",
        "--input",
        "-",
    ]
    payload = json.loads(calls.inputs[0])
    assert payload["commit_id"] == LIVE_SHA and payload["event"] == "REQUEST_CHANGES"
    assert [(c["path"], c["line"], c["side"]) for c in payload["comments"]] == [
        ("app.py", 2, "RIGHT")
    ]
    assert json.loads(res.stdout)["status"] == "review_published"


def test_review_pr_head_moving_after_file_fetch_blocks_post(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(
        monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES, later_sha="c" * 40
    )
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 7, res.output
    assert json.loads(res.stdout)["status"] == "publish_blocked_stale_head"
    assert len(calls.files) == 1 and calls == []


def test_review_pr_422_retries_once_body_only(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES)
    posts = []

    def fake_run_gh(args, e, **k):
        if "/files" in args[1]:
            return 0, json.dumps([_ANCHOR_FILES]), ""
        posts.append(json.loads(k["input"]))
        if len(posts) == 1:
            return 1, "", "gh: Unprocessable Entity (HTTP 422)"
        return 0, json.dumps({"html_url": REVIEW_URL, "id": 9}), ""

    monkeypatch.setattr(cli, "run_gh", fake_run_gh)
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    assert len(posts) == 2 and posts[0]["comments"] and posts[1]["comments"] == []
    assert "Guard null" in posts[1]["body"]
    data = json.loads(res.stdout)
    assert data["status"] == "review_published"
    assert "inline anchors rejected, posted body-only" in data["reason"]


def test_review_pr_422_retry_rechecks_head_first(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES)
    shas = iter([LIVE_SHA, LIVE_SHA, LIVE_SHA, LIVE_SHA])
    monkeypatch.setattr(cli, "fetch_pr_head_sha", lambda *a, **k: next(shas, "c" * 40))
    posts = []

    def fake_run_gh(args, e, **k):
        if "/files" in args[1]:
            return 0, json.dumps([_ANCHOR_FILES]), ""
        posts.append(args)
        return 1, "", "gh: Unprocessable Entity (HTTP 422)"

    monkeypatch.setattr(cli, "run_gh", fake_run_gh)
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert json.loads(res.stdout)["status"] == "publish_blocked_stale_head", res.output
    assert len(posts) == 1  # the head moved, so no second write


def test_review_pr_unreadable_files_posts_body_only_and_says_so(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES)
    posts = []

    def fake_run_gh(args, e, **k):
        if "/files" in args[1]:
            return 1, "", "HTTP 502"
        posts.append(json.loads(k["input"]))
        return 0, json.dumps({"html_url": REVIEW_URL, "id": 9}), ""

    monkeypatch.setattr(cli, "run_gh", fake_run_gh)
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 0, res.output
    assert posts[0]["comments"] == [] and "Guard null" in posts[0]["body"]
    assert "could not read the PR files" in json.loads(res.stdout)["reason"]


def test_review_pr_refuses_malformed_head_sha(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(monkeypatch, live_sha="not-a-sha")
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code != 0 and calls == []
    assert "not a valid SHA" in res.stdout


def test_review_pr_non_422_failure_does_not_retry(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(monkeypatch, container=_inline_review(), review_files=_ANCHOR_FILES)

    def fake_run_gh(args, e, **k):
        if "/files" in args[1]:
            return 0, "[]", ""
        calls.append(args)
        return 1, "", "HTTP 500 boom"

    monkeypatch.setattr(cli, "run_gh", fake_run_gh)
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 8, res.output
    assert json.loads(res.stdout)["status"] == "publish_failed"
    assert len(calls) == 1


def test_review_pr_off_allowlist_refuses(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _review_env())
    res = CliRunner().invoke(
        cli.main, ["review-pr", "https://github.com/stranger/repo/pull/1", "--json"]
    )
    assert res.exit_code == 4
    data = json.loads(res.stdout)
    assert data["error"]["code"] == 4


def test_review_pr_missing_creds_clean_error_no_secret_leak(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", {})
    res = CliRunner().invoke(cli.main, ["review-pr", PR_URL])
    assert res.exit_code != 0
    assert "ghp_fake" not in res.output


def test_review_pr_invalid_expected_head_sha_exits_usage(monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _review_env())
    res = CliRunner().invoke(
        cli.main, ["review-pr", "--expected-head-sha", "not-a-sha", "--", PR_URL]
    )
    assert res.exit_code == 2  # EXIT_USAGE


def test_review_pr_no_findings_exits_agent_error(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=(0, "agent said nothing structured"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL, "--json"])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "no_findings"


def test_review_pr_timeout_maps_to_exit_9(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=(cli.CONTAINER_TIMEOUT_CODE, "timed out"))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", PR_URL, "--max-duration", "5", "--json"])
    assert res.exit_code == 9
    data = json.loads(res.stdout)
    assert data["status"] == "timeout"


def test_review_pr_matches_bridge_argv_contract(monkeypatch):
    """Contract test: mirrors the bridge's `build_review_pr_argv`, which shapes
    `franky review-pr [--expected-head-sha SHA] [--no-publish] -- <pr_url> [instructions]` and
    reads the result back through `franky_status`. Build the SAME argv the bridge would dispatch
    for a `franky_review_pr(pr_url, instructions, expected_head_sha, publish=True)` MCP call, and
    assert the CLI's --json response carries everything the bridge's read_run needs: a
    submitted-review URL matching `<pr_url>#pullrequestreview-<id>` (what franky_status reports
    as "review finished and published"), plus the grounded reviewed_sha/findings/checks."""
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, live_sha=LIVE_SHA)

    # Mirrors bridge/franky.py::build_review_pr_argv EXACTLY (minus the franky binary itself) -
    # the bridge never adds --json, so the real dispatch is plain-text.
    expected_head_sha = LIVE_SHA
    instructions = "focus on error handling"
    publish = True
    argv = ["review-pr"]
    if expected_head_sha:
        argv += ["--expected-head-sha", expected_head_sha]
    if not publish:
        argv.append("--no-publish")
    argv += ["--", PR_URL]
    if instructions:
        argv.append(instructions)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, argv)
    assert res.exit_code == 0, res.output
    # This is exactly what the bridge's read_run/_parse_review_url regex scans the combined
    # stdout+stderr log for to report "review finished and published" via franky_status.
    assert REVIEW_URL in res.output
    assert REVIEW_URL.startswith(PR_URL + "#pullrequestreview-")

    # Same request, --json, for the structured envelope requirement (9): reviewed SHA, findings
    # summary, check outcomes, and review URL/ID when published.
    runner2 = CliRunner()
    with runner2.isolated_filesystem():
        res2 = runner2.invoke(cli.main, ["review-pr", "--json", *argv[1:]])
    assert res2.exit_code == 0, res2.output
    data = json.loads(res2.stdout)
    assert data["status"] == "review_published"
    assert data["reviewed_sha"] == LIVE_SHA
    assert data["review_url"] == REVIEW_URL
    assert data["review_id"] == 555
    assert data["repo"] == "me/repo"
    assert isinstance(data["job_id"], str) and data["job_id"]


# ---------------------------------------------------------------------------
# `franky review-pr --thread` + `franky threads` (stored per-PR review sessions)
# ---------------------------------------------------------------------------

import franky.threads as threads_mod  # noqa: E402

THREAD_ID = f"me__repo__{REVIEW_PR_NUMBER}__reviewer"
BLOCKING = {
    "summary": "one bug",
    "findings": [{"title": "race", "body": "b", "severity": "blocking", "file": "a.py", "line": 3}],
    "checks": [],
}


def _thread_env(tmp_path, engine_token=True):
    env = _review_env()
    env["FRANKY_THREADS_DIR"] = str(tmp_path / "threads")
    env["FRANKY_CONFIG_FILE"] = str(tmp_path / "no-config")  # never merge a real config file
    if engine_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-fake"
    return env


def _thread_dir(tmp_path):
    return tmp_path / "threads" / THREAD_ID


def _fake_thread_run(monkeypatch, tmp_path, *, code=0, output=None, seen=None, results=None):
    """A run_in_container fake that asserts the record is on disk BEFORE the run, records argv
    and kwargs, and plays the container side of copy-out (a session file per run). `results`
    is an optional list of (code, output) per call, for multi-attempt runs."""
    seen = [] if seen is None else seen
    results = list(results) if results else None

    def fake(cfg, inner_argv, *a, **k):
        record = threads_mod.read_record(_thread_dir(tmp_path))
        call = {"argv": list(inner_argv), "kwargs": k, "record_before": record}
        if k.get("session_tar"):
            call["tar_existed"] = Path(k["session_tar"]).exists()
            with tarfile.open(k["session_tar"]) as archive:
                call["tar_names"] = archive.getnames()
        seen.append(call)
        run_code, run_output = results.pop(0) if results else (code, output)
        sink = k.get("session_sink")
        if sink and run_code == 0:
            path = Path(sink["dest"], sink["paths"][0])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'{{"run": {len(seen)}}}\n')
            sink["status"] = "ok"
        return run_code, run_output if run_output is not None else _review_block(BLOCKING)

    monkeypatch.setattr(cli, "run_in_container", fake)
    return seen


def _review(args):
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(cli.main, ["review-pr", *args, "--json", PR_URL])


def test_review_pr_thread_first_run_pins_session_before_launch(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = _fake_thread_run(monkeypatch, tmp_path)
    res = _review(["--thread", "--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    before = seen[0]["record_before"]
    assert before is not None and before["session_ok"] is False  # written before the run
    sid = before["session_id"]
    assert seen[0]["argv"][seen[0]["argv"].index("--session-id") + 1] == sid
    assert "--resume" not in seen[0]["argv"] and "session_tar" not in seen[0]["kwargs"]
    assert data["thread"] == {
        "id": THREAD_ID,
        "role": "reviewer",
        "engine": "claude",
        "model": None,
        "rubric_version": "",
        "session_id": sid,
        "session": "fresh",
        "session_reason": "new_thread",
        "last_sha_before": None,
    }
    assert data["handoff"]["sha"] == LIVE_SHA
    assert data["handoff"]["findings"][0]["title"] == "race"
    record = threads_mod.read_record(_thread_dir(tmp_path))
    assert record["session_ok"] is True and record["last_sha"] == LIVE_SHA
    assert (_thread_dir(tmp_path) / "session/.claude/projects/-work" / f"{sid}.jsonl").exists()
    job = jobs.read_record(data["job_id"])
    assert job["thread_id"] == THREAD_ID


def test_review_pr_thread_second_run_resumes_the_same_session(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    res = _review(["--thread", "--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    sid = seen[0]["record_before"]["session_id"]
    argv = seen[1]["argv"]
    assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv
    assert seen[1]["tar_existed"] is True
    assert not Path(seen[1]["kwargs"]["session_tar"]).exists()  # cleaned after the run
    assert data["thread"]["session"] == "resumed" and data["thread"]["session_id"] == sid
    assert data["thread"]["last_sha_before"] == LIVE_SHA
    # The prior findings ride into the prompt (fenced) on the second run.
    assert "Prior review context" not in seen[0]["argv"][2]
    assert f"FRANKY_PRIOR_{REVIEW_NONCE}_BEGIN" in argv[2] and "race" in argv[2]


def test_review_pr_thread_engine_change_seeds_and_drops_old_session(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    res = _review(["--thread", "--engine", "pi", "--no-publish"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["thread"]["session"] == "seeded"
    assert data["thread"]["session_reason"] == "engine_changed:claude->pi"
    assert data["thread"]["session_id"] is None
    assert not (_thread_dir(tmp_path) / "session").exists()
    assert "--session-id" not in seen[1]["argv"] and "session_sink" not in seen[1]["kwargs"]
    assert "Prior review context" in seen[1]["argv"][2]


def test_review_pr_thread_busy_exits_4_with_json_error(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    env = _thread_env(tmp_path)
    _mc_review_setup(monkeypatch, env=env)
    seen = _fake_thread_run(monkeypatch, tmp_path)
    held = threads_mod.open_thread("me/repo", REVIEW_PR_NUMBER, "reviewer", env)
    try:
        res = _review(["--thread", "--engine", "claude"])
    finally:
        held.close()
    assert res.exit_code == 4
    assert json.loads(res.stdout)["error"]["kind"] == "thread_busy"
    assert seen == []


RESUME_ERROR = "Error: No conversation found with session ID: x"


def _two_runs(monkeypatch, tmp_path, second_results, args=("--no-publish",)):
    """One successful run to store a session, then a second run with `second_results`."""
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", *args]).exit_code == 0
    first_sid = threads_mod.read_record(_thread_dir(tmp_path))["session_id"]
    seen = _fake_thread_run(monkeypatch, tmp_path, results=second_results)
    res = _review(["--thread", "--engine", "claude", *args])
    return first_sid, seen, res


def test_review_pr_thread_failed_resume_retries_once_seeded(monkeypatch, tmp_path):
    first_sid, seen, res = _two_runs(monkeypatch, tmp_path, [(1, RESUME_ERROR), (0, None)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "review_complete"
    assert data["thread"]["session"] == "seeded"
    assert data["thread"]["session_reason"] == "resume_failed"
    assert len(seen) == 2
    assert seen[0]["argv"][seen[0]["argv"].index("--resume") + 1] == first_sid
    retry = seen[1]["argv"]
    new_sid = retry[retry.index("--session-id") + 1]
    assert new_sid != first_sid and "--resume" not in retry
    assert "session_tar" not in seen[1]["kwargs"] or not seen[1]["kwargs"]["session_tar"]
    assert f"FRANKY_PRIOR_{REVIEW_NONCE}_BEGIN" in retry[2]
    assert seen[1]["record_before"]["session_id"] == new_sid  # pinned before the retry
    record = threads_mod.read_record(_thread_dir(tmp_path))
    assert record["session_id"] == new_sid and record["session_ok"] is True
    # The failed attempt keeps its own redacted log (quiet under --json, so no stderr note).
    assert list((tmp_path / "test-franky-runs").rglob(f"*{data['job_id']}-resume-failed.log"))


def test_review_pr_thread_failed_retry_reports_the_retry_and_never_loops(monkeypatch, tmp_path):
    _sid, seen, res = _two_runs(monkeypatch, tmp_path, [(1, "boom"), (1, "boom again")])
    data = json.loads(res.stdout)
    assert len(seen) == 2 and data["status"] == "agent_error" and res.exit_code == 7
    assert data["thread"]["session"] == "seeded"
    assert data["thread"]["session_reason"] == "resume_failed"
    record = threads_mod.read_record(_thread_dir(tmp_path))
    assert record["session_ok"] is False and record["handoff"] is not None


def test_review_pr_thread_resume_timeout_keeps_session_without_retry(monkeypatch, tmp_path):
    first_sid, seen, res = _two_runs(monkeypatch, tmp_path, [(124, "franky: container timed out")])
    assert len(seen) == 1 and json.loads(res.stdout)["status"] == "timeout"
    record = threads_mod.read_record(_thread_dir(tmp_path))
    assert record["session_id"] == first_sid and record["session_ok"] is True


def test_review_pr_thread_unpackable_session_downgrades_to_seeded(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    monkeypatch.setattr(threads_mod, "session_tar", lambda *a, **k: None)
    seen = _fake_thread_run(monkeypatch, tmp_path)
    data = json.loads(_review(["--thread", "--engine", "claude", "--no-publish"]).stdout)
    assert data["thread"]["session"] == "seeded"
    assert data["thread"]["session_reason"] == "session_pack_failed"
    assert "--session-id" in seen[0]["argv"] and "--resume" not in seen[0]["argv"]


def test_review_pr_thread_record_write_failure_runs_without_session_flags(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = _fake_thread_run(monkeypatch, tmp_path)
    monkeypatch.setattr(threads_mod, "write_record", lambda *a, **k: False)
    res = _review(["--thread", "--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["thread"]["session_reason"] == "record_write_failed"
    assert "--session-id" not in seen[0]["argv"] and "--resume" not in seen[0]["argv"]
    assert "session_sink" not in seen[0]["kwargs"]
    assert "record not updated reason=write_failed" in res.stderr


def test_review_pr_thread_is_case_insensitive_on_the_repo(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    env = _thread_env(tmp_path)
    _mc_review_setup(monkeypatch, env=env)
    _fake_thread_run(monkeypatch, tmp_path)
    runner = CliRunner()
    for url in ("https://github.com/Me/Repo/pull/11", "https://github.com/me/REPO/pull/11"):
        with runner.isolated_filesystem():
            res = runner.invoke(
                cli.main,
                ["review-pr", "--thread", "--engine", "claude", "--no-publish", "--json", url],
            )
        assert res.exit_code == 0, res.output
    assert [p.name for p in (tmp_path / "threads").iterdir()] == [THREAD_ID]
    assert threads_mod.read_record(_thread_dir(tmp_path))["repo"] == "me/repo"
    assert json.loads(res.stdout)["thread"]["session"] == "resumed"


def test_review_pr_thread_handoff_redacts_token_patterns(monkeypatch, tmp_path):
    token = "ghp_" + "B" * 36
    payload = {
        "summary": "s",
        "findings": [{"title": f"leaked {token}", "severity": "blocking", "body": "b"}],
        "checks": [],
    }
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    _fake_thread_run(monkeypatch, tmp_path, output=_review_block(payload))
    res = _review(["--thread", "--engine", "claude", "--no-publish"])
    assert token not in json.dumps(json.loads(res.stdout)["handoff"])
    assert token not in (_thread_dir(tmp_path) / "record.json").read_text()
    seen = _fake_thread_run(monkeypatch, tmp_path)
    _review(["--thread", "--engine", "claude", "--no-publish"])
    assert "leaked [redacted]" in seen[0]["argv"][2] and token not in seen[0]["argv"][2]


def test_review_pr_without_thread_forces_status_new(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    payload = {
        "summary": "s",
        "findings": [{"title": "bug", "severity": "blocking", "status": "resolved", "body": "b"}],
        "checks": [],
    }
    calls = _mc_review_setup(monkeypatch, container=(0, _review_block(payload)))
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["review-pr", "--json", PR_URL])
    assert res.exit_code == 0, res.output
    body = json.loads(calls.inputs[0])
    assert body["event"] == "REQUEST_CHANGES" and "[resolved]" not in body["body"]


def test_review_pr_without_thread_is_unchanged(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = []

    def fake(cfg, inner_argv, *a, **k):
        seen.append((list(inner_argv), set(k)))
        return 0, _review_block(BLOCKING)

    monkeypatch.setattr(cli, "run_in_container", fake)
    res = _review(["--engine", "claude"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert set(data) == {
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
        "reviewed_sha",
        "findings_summary",
        "checks",
        "review_url",
        "review_id",
        "review_event",
        "context_sources",
    }
    argv, kwargs = seen[0]
    assert argv[:7] == [
        "claude",
        "-p",
        argv[2],
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    assert len(argv) == 7 and "Prior review context" not in argv[2]
    assert not {"session_tar", "session_sink"} & kwargs
    assert not (tmp_path / "threads").exists()


def _seed_thread(env, pr=REVIEW_PR_NUMBER, repo="me/repo"):
    thread = threads_mod.open_thread(repo, pr, "reviewer", env)
    record, _written = threads_mod.begin_run(
        thread,
        None,
        repo=repo,
        pr=pr,
        role="reviewer",
        engine="claude",
        native=True,
        model=None,
        rubric="",
        mode="fresh",
        job_id="job1",
        now=threads_mod.datetime(2020, 1, 1, tzinfo=threads_mod.timezone.utc),
    )
    record["handoff"] = {"schema": 1, "sha": "a", "summary": "SECRET-CONTENT", "findings": []}
    threads_mod.write_record(thread, record)
    (thread.session_dir).mkdir()
    (thread.session_dir / "s.jsonl").write_text("SECRET-CONTENT")
    thread.close()
    return thread.path


def test_threads_list_json_includes_repo_and_session_bytes(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    monkeypatch.setattr(cli.os, "environ", env)
    _seed_thread(env)
    res = CliRunner().invoke(cli.main, ["threads", "list", "--json"])
    assert res.exit_code == 0, res.output
    [entry] = json.loads(res.stdout)
    assert entry["thread"] == THREAD_ID and entry["repo"] == "me/repo"
    assert entry["session_bytes"] == len("SECRET-CONTENT")


def test_threads_prune_json_shape_and_stderr_lines(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    monkeypatch.setattr(cli.os, "environ", env)
    path = _seed_thread(env)  # updated 2020 -> idle
    res = CliRunner().invoke(cli.main, ["threads", "prune", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {
        "purged": [{"thread": THREAD_ID, "reason": "idle", "bytes": 14}],
        "kept": 0,
        "bytes": 0,
        "disk_skipped": False,
    }
    assert f"purged {THREAD_ID} reason=idle bytes=14" in res.stderr
    assert "SECRET-CONTENT" not in res.output
    assert not path.exists()


def test_threads_prune_closed_without_token_skips_that_pass(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    del env["GH_TOKEN"]
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(threads_mod, "run_gh", lambda *a, **k: pytest.fail("gh must not run"))
    res = CliRunner().invoke(cli.main, ["threads", "prune", "--closed", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert "skipping the --closed pass" in res.stderr


def test_threads_prune_repo_is_validated(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.os, "environ", _thread_env(tmp_path))
    for args in (["--repo", "../etc"], ["--older-than", "soon"], ["--max-bytes", "2T"]):
        res = CliRunner().invoke(cli.main, ["threads", "prune", "--json", *args])
        assert res.exit_code == 2, args
        assert json.loads(res.stdout)["error"]["kind"] == "usage_error"


def test_threads_prune_repo_reports_disk_skipped(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    monkeypatch.setattr(cli.os, "environ", env)
    _seed_thread(env)
    res = CliRunner().invoke(
        cli.main, ["threads", "prune", "--repo", "me/other", "--max-bytes", "0", "--json"]
    )
    assert json.loads(res.stdout) == {"purged": [], "kept": 0, "bytes": 0, "disk_skipped": True}


def test_threads_purge_json_shape_never_prints_content(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    monkeypatch.setattr(cli.os, "environ", env)
    _seed_thread(env)
    res = CliRunner().invoke(
        cli.main, ["threads", "purge", f"me/repo#{REVIEW_PR_NUMBER}", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {
        "purged": [{"thread": THREAD_ID, "reason": "manual", "bytes": 14}],
        "busy": [],
    }
    assert "SECRET-CONTENT" not in res.output
    assert f"purged {THREAD_ID} bytes=14" in res.stderr


@pytest.mark.parametrize(
    "args", [[], ["me/repo#1", "--all"], ["--all", "--role", "author"], ["not-a-ref"]]
)
def test_threads_purge_usage_errors(monkeypatch, tmp_path, args):
    monkeypatch.setattr(cli.os, "environ", _thread_env(tmp_path))
    res = CliRunner().invoke(cli.main, ["threads", "purge", "--json", *args])
    assert res.exit_code == 2
    assert json.loads(res.stdout)["error"]["kind"] == "usage_error"


@pytest.mark.parametrize("extra", [[], ["--repo", "me/repo"]])
def test_threads_prune_closed_uses_the_process_env_for_gh(monkeypatch, tmp_path, extra):
    from collections.abc import Mapping

    env = _thread_env(tmp_path)
    monkeypatch.setattr(cli.os, "environ", env)
    _seed_thread(env)
    seen = []

    def fake_gh(args, gh_env, **kwargs):
        assert isinstance(gh_env, Mapping) and gh_env.get("GH_TOKEN") == "ghp_fake"
        seen.append(args)
        return 0, json.dumps({"data": {"t0": {"pullRequest": {"state": "MERGED"}}}}), ""

    monkeypatch.setattr(threads_mod, "run_gh", fake_gh)
    res = CliRunner().invoke(
        cli.main, ["threads", "prune", "--closed", "--older-than", "100000d", "--json", *extra]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["purged"] == [
        {"thread": THREAD_ID, "reason": "closed", "bytes": 14}
    ]
    assert len(seen) == 1


def test_review_pr_thread_copies_out_only_the_session_file_and_side_dir(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=_thread_env(tmp_path))
    seen = _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    sid = seen[0]["record_before"]["session_id"]
    assert seen[0]["kwargs"]["session_sink"]["paths"] == [
        f".claude/projects/-work/{sid}.jsonl",
        f".claude/projects/-work/{sid}",
    ]
    seen = _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    assert seen[0]["tar_names"] == [f".claude/projects/-work/{sid}.jsonl"]
    assert not Path(seen[0]["kwargs"]["session_tar"]).exists()


def _reasons(res):
    return [
        (s["ref"], s["status"], s.get("reason")) for s in json.loads(res.stdout)["context_sources"]
    ]


def test_review_pr_public_repo_never_fetches(monkeypatch, tmp_path):
    prompts, fetched = _jira_review_setup(monkeypatch, tmp_path, meta={"private": False})
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert fetched == []
    assert _reasons(res) == [
        ("ABC-123", "unavailable", "public_repo"),
        ("OPS-9", "unavailable", "public_repo"),
    ]
    assert f"FRANKY_TICKET_{REVIEW_NONCE}_BEGIN\n" not in prompts[0]
    assert "no ticket context was provided" in prompts[0]


def test_review_pr_connection_failure_stops_further_fetches(monkeypatch, tmp_path):
    def down(key, e, **k):
        raise _jira_tag(NetworkError("could not reach JIRA"), "network", stop=True)

    _, fetched = _jira_review_setup(
        monkeypatch,
        tmp_path,
        fetcher=down,
        meta={"title": "AA-1 BB-2 CC-3", "body": "", "head_ref": ""},
    )
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert [key for key, _ in fetched] == ["AA-1"]
    assert {r for _, _, r in _reasons(res)} == {"network"}


def test_review_pr_auth_failure_stops_further_fetches(monkeypatch, tmp_path):
    def denied(key, e, **k):
        raise _jira_tag(NetworkError("auth"), "auth", stop=True)

    _, fetched = _jira_review_setup(monkeypatch, tmp_path, fetcher=denied)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert [key for key, _ in fetched] == ["ABC-123"]
    assert [r for _, _, r in _reasons(res)] == ["auth", "auth"]


def test_review_pr_http_base_url_is_unavailable_config_and_review_succeeds(monkeypatch, tmp_path):
    from franky.jira import fetch_jira_issue as real_fetch

    _jira_review_setup(
        monkeypatch,
        tmp_path,
        fetcher=lambda key, e, **k: real_fetch(key, e, **k),
        extra_env={"JIRA_BASE_URL": "http://example.atlassian.net"},
    )
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["status"] == "review_complete"
    assert {(st, r) for _, st, r in _reasons(res)} == {("unavailable", "config")}


def test_review_pr_partial_jira_config_is_unconfigured(monkeypatch, tmp_path):
    _, fetched = _jira_review_setup(
        monkeypatch,
        tmp_path,
        jira=False,
        extra_env={"JIRA_BASE_URL": "https://example.atlassian.net"},
    )
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert fetched == []
    assert {(st, r) for _, st, r in _reasons(res)} == {("unconfigured", "unconfigured")}


def test_review_pr_redacts_jira_token_inside_ticket_text(monkeypatch, tmp_path):
    prompts, _ = _jira_review_setup(
        monkeypatch,
        tmp_path,
        fetcher=lambda key, e, **k: "[ABC-123] note jira-tok-s3cret end",
        meta={"title": "ABC-123", "body": "", "head_ref": ""},
    )
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert "jira-tok-s3cret" not in prompts[0]
    assert "[ABC-123] note ***REDACTED*** end" in prompts[0]


def test_review_pr_passes_jira_secrets_for_stream_redaction_not_env(monkeypatch, tmp_path):
    _jira_review_setup(monkeypatch, tmp_path)
    kwargs = []

    def fake_run(cfg, inner_argv, *a, **k):
        kwargs.append((cfg, k))
        return 0, _review_block({"summary": "ok", "findings": [], "checks": []})

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    cfg, k = kwargs[0]
    assert set(k["extra_secrets"]) == {"jira-tok-s3cret", "dev@example.com"}
    assert "JIRA_API_TOKEN" not in cfg.passthrough_env


def test_review_pr_thread_seeded_retry_keeps_the_ticket_block(monkeypatch, tmp_path):
    _fix_review_nonce(monkeypatch)
    env = _thread_env(tmp_path)
    env.update(_JIRA_ENV)
    _mc_review_setup(monkeypatch, env=env)

    def fake_fetch(repo, number, e, **k):
        if k.get("meta_sink") is not None:
            k["meta_sink"].update(title="ABC-123", body="", head_ref="", private=True)
        return LIVE_SHA

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fake_fetch)
    monkeypatch.setattr(cli, "fetch_jira_issue", lambda key, e, **k: _TICKET_TEXT)
    _fake_thread_run(monkeypatch, tmp_path)
    assert _review(["--thread", "--engine", "claude", "--no-publish"]).exit_code == 0
    seen = _fake_thread_run(monkeypatch, tmp_path, results=[(1, RESUME_ERROR), (0, None)])
    res = _review(["--thread", "--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert len(seen) == 2 and "--resume" in seen[0]["argv"] and "--resume" not in seen[1]["argv"]
    fence = f"FRANKY_TICKET_{REVIEW_NONCE}_BEGIN\n{_TICKET_TEXT}\nFRANKY_TICKET_{REVIEW_NONCE}_END"
    assert fence in seen[0]["argv"][2] and fence in seen[1]["argv"][2]


def test_review_pr_frozen_mode_never_fetches_tickets(monkeypatch):
    _frozen_setup(monkeypatch)
    cli.os.environ.update(_JIRA_ENV)

    def no_fetch(*a, **k):
        raise AssertionError("frozen mode must not fetch tickets")

    monkeypatch.setattr(cli, "fetch_jira_issue", no_fetch)
    res = _run_frozen(FROZEN)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["context_sources"] == []


# --- review-pr --at-sha/--diff-base (frozen eval mode) ---------------------------------------

AT_SHA = "c" * 40
BASE_SHA = "d" * 40
FROZEN = ["--no-publish", "--json", "--at-sha", AT_SHA, "--diff-base", BASE_SHA]


def _frozen_setup(monkeypatch, *, commits=None, commits_rc=0, commits_out=None, container=None):
    """Hermetic frozen run. Returns (gh_calls, container_calls); fails on any live-head fetch."""
    _fix_review_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _review_env())
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    def no_live(*a, **k):
        raise AssertionError("frozen mode must not read the live head")

    monkeypatch.setattr(cli, "fetch_pr_head_sha", no_live)
    gh_calls, container_calls = [], []
    pages = [[{"sha": "e" * 40}], [{"sha": AT_SHA}]] if commits is None else commits
    out = json.dumps(pages) if commits_out is None else commits_out

    def fake_gh(args, e, **k):
        gh_calls.append(list(args))
        return commits_rc, out, ""

    def fake_run(*a, **k):
        container_calls.append(a)
        return container or (
            0,
            _review_block({"summary": "s", "findings": [{"title": "t"}], "checks": []}),
        )

    monkeypatch.setattr(cli, "run_gh", fake_gh)
    monkeypatch.setattr(cli, "run_in_container", fake_run)
    return gh_calls, container_calls


def _run_frozen(args):
    return CliRunner().invoke(cli.main, ["review-pr", *args, "--", PR_URL])


@pytest.mark.parametrize(
    "args",
    [
        ["--no-publish", "--json", "--at-sha", AT_SHA],
        ["--no-publish", "--json", "--diff-base", BASE_SHA],
        ["--no-publish", "--json", "--at-sha", "abc123", "--diff-base", BASE_SHA],
        ["--no-publish", "--json", "--at-sha", AT_SHA.upper(), "--diff-base", BASE_SHA],
        ["--no-publish", "--json", "--at-sha", AT_SHA, "--diff-base", "z" * 40],
        ["--json", "--at-sha", AT_SHA, "--diff-base", BASE_SHA],
        [*FROZEN, "--thread"],
        [*FROZEN, "--expected-head-sha", AT_SHA],
    ],
)
def test_review_pr_frozen_flag_refusals(monkeypatch, args):
    gh_calls, container_calls = _frozen_setup(monkeypatch)
    res = _run_frozen(args)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "usage_error"
    assert gh_calls == [] and container_calls == []


def test_review_pr_frozen_happy_path_pins_at_sha_and_never_posts(monkeypatch):
    gh_calls, container_calls = _frozen_setup(monkeypatch)
    res = _run_frozen(FROZEN)
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["status"] == "review_complete"
    assert data["reviewed_sha"] == AT_SHA
    assert len(container_calls) == 1
    assert gh_calls == [
        [
            "api",
            f"repos/me/repo/pulls/{REVIEW_PR_NUMBER}/commits?per_page=100",
            "--paginate",
            "--slurp",
        ]
    ]
    assert not any("POST" in a or "--method" in a or "-X" in a for c in gh_calls for a in c)


def test_review_pr_frozen_prompt_is_frozen(monkeypatch):
    _frozen_setup(monkeypatch)
    seen = {}
    original = cli.build_review_pr_prompt

    def capture(*a, **k):
        seen.update(k)
        return original(*a, **k)

    monkeypatch.setattr(cli, "build_review_pr_prompt", capture)
    assert _run_frozen(FROZEN).exit_code == 0
    assert seen["frozen"] is True and seen["diff_base"] == BASE_SHA and seen["head_sha"] == AT_SHA


@pytest.mark.parametrize(
    "kw",
    [
        {"commits": [[{"sha": "e" * 40}]]},  # absent
        {"commits_rc": 1},  # gh failure
        {"commits_out": "not json"},  # bad JSON
        {"commits_out": '{"sha": "x"}'},  # wrong shape
    ],
)
def test_review_pr_frozen_refuses_sha_not_in_pr(monkeypatch, kw):
    gh_calls, container_calls = _frozen_setup(monkeypatch, **kw)
    res = _run_frozen(FROZEN)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["error"]["kind"] == "usage_error"
    assert container_calls == []


def test_review_pr_frozen_repo_gate_runs_before_any_api_call(monkeypatch):
    gh_calls, container_calls = _frozen_setup(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", {**_review_env(), "FRANKY_ALLOWED_REPOS": "other/x"})
    res = _run_frozen(FROZEN)
    assert res.exit_code != 0
    assert gh_calls == [] and container_calls == []


def test_review_pr_no_publish_result_has_all_findings(monkeypatch):
    _fix_review_nonce(monkeypatch)
    items = [
        {"title": f"t{i}", "body": "b", "severity": "blocking", "file": "a.py", "line": 3}
        for i in range(10)
    ]
    _mc_review_setup(
        monkeypatch, container=(0, _review_block({"summary": "s", "findings": items, "checks": []}))
    )
    res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["findings_total"] == 10
    assert len(data["findings"]) == 10  # all of them: findings_total must match
    assert set(data["findings"][0]) == {"title", "body", "severity", "file", "line", "start_line"}


@pytest.mark.parametrize("payload", [{"summary": "s", "checks": []}, {"findings": "x"}])
def test_review_pr_missing_findings_list_is_not_a_clean_review(monkeypatch, payload):
    _mc_review_setup(monkeypatch, container=(0, _review_block(payload)))
    _fix_review_nonce(monkeypatch)
    res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    assert res.exit_code == 7
    data = json.loads(res.stdout)
    assert data["status"] == "no_findings"
    assert "findings" not in data and "no `findings` list" in data["reason"]


def test_review_pr_findings_absent_on_non_complete_status(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, container=(1, "boom"))
    res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--json", "--", PR_URL])
    data = json.loads(res.stdout)
    assert data["status"] == "agent_error"
    assert "findings" not in data and "findings_total" not in data


# ---------------------------------------------------------------------------
# `review-pr` JIRA ticket context (host-side fetch, fenced untrusted data)
# ---------------------------------------------------------------------------

from franky.jira import _tag as _jira_tag  # noqa: E402
from franky.result import NetworkError  # noqa: E402

_JIRA_ENV = {
    "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_EMAIL": "dev@example.com",
    "JIRA_API_TOKEN": "jira-tok-s3cret",
}
_TICKET_TEXT = "[ABC-123] Cap the thing\n\nAcceptance: cap at 5."


def _jira_review_setup(
    monkeypatch, tmp_path, *, jira=True, fetcher=None, output=None, meta=None, extra_env=None
):
    """review-pr with PR metadata fed through meta_sink and a fake JIRA fetcher. Returns the
    list of prompts seen by the container and the list of keys fetched."""
    env = _review_env()
    env["FRANKY_CONFIG_FILE"] = str(tmp_path / "no-config")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-fake"
    if jira:
        env.update(_JIRA_ENV)
    env.update(extra_env or {})
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=env)
    meta = {
        "title": "ABC-123 fix",
        "body": "also https://example.atlassian.net/browse/OPS-9",
        "head_ref": "abc-123-fix",
        "private": True,
        **(meta or {}),
    }

    def fake_fetch(repo, number, e, **k):
        if k.get("meta_sink") is not None:
            k["meta_sink"].update(meta)
        return LIVE_SHA

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fake_fetch)
    fetched = []

    def default_fetcher(key, e, **k):
        fetched.append((key, k))
        if key == "OPS-9":
            raise _jira_tag(NetworkError("JIRA issue OPS-9 not found"), "not_found")
        return _TICKET_TEXT

    def fetcher_wrapper(key, e, **k):
        fetched.append((key, k))
        return fetcher(key, e, **k)

    monkeypatch.setattr(cli, "fetch_jira_issue", fetcher_wrapper if fetcher else default_fetcher)
    prompts = []

    def fake_run(cfg, inner_argv, *a, **k):
        prompts.append(" ".join(inner_argv))
        return 0, output or _review_block({"summary": "ok", "findings": [], "checks": []})

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    return prompts, fetched


def test_review_pr_fences_jira_tickets_and_reports_context_sources(monkeypatch, tmp_path):
    prompts, fetched = _jira_review_setup(monkeypatch, tmp_path)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["context_sources"] == [
        {"kind": "jira", "ref": "ABC-123", "status": "included"},
        {"kind": "jira", "ref": "OPS-9", "status": "unavailable", "reason": "not_found"},
    ]
    assert all(k["refuse_restricted"] is True and k["timeout"] == 5.0 for _, k in fetched)
    fence = f"FRANKY_TICKET_{REVIEW_NONCE}_BEGIN\n{_TICKET_TEXT}\nFRANKY_TICKET_{REVIEW_NONCE}_END"
    assert fence in prompts[0]
    assert "Acceptance: cap at 5." not in res.stdout
    assert "jira-tok-s3cret" not in prompts[0]


def test_review_pr_without_jira_env_reports_unconfigured_and_never_fetches(monkeypatch, tmp_path):
    prompts, fetched = _jira_review_setup(monkeypatch, tmp_path, jira=False)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert fetched == []
    sources = json.loads(res.stdout)["context_sources"]
    assert [(s["status"], s["reason"]) for s in sources] == [("unconfigured", "unconfigured")] * 2
    assert f"FRANKY_TICKET_{REVIEW_NONCE}_BEGIN\n" not in prompts[0]
    assert "no ticket context was provided" in prompts[0]


def test_review_pr_plain_stub_without_meta_gives_empty_context_sources(monkeypatch, tmp_path):
    env = _thread_env(tmp_path)
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch, env=env)
    monkeypatch.setattr(cli, "fetch_pr_head_sha", lambda *a, **k: LIVE_SHA)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["context_sources"] == []


def test_review_pr_long_ticket_is_truncated_and_partial(monkeypatch, tmp_path):
    long_text = "[ABC-123] x\n\n" + "y" * 5000
    prompts, _ = _jira_review_setup(
        monkeypatch,
        tmp_path,
        fetcher=lambda key, e, **k: long_text,
        meta={"title": "ABC-123", "body": "", "head_ref": ""},
    )
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["context_sources"] == [
        {"kind": "jira", "ref": "ABC-123", "status": "partial"}
    ]
    assert "[truncated]" in prompts[0] and "y" * 5000 not in prompts[0]


def test_review_pr_redacts_jira_token_from_agent_output_and_json(monkeypatch, tmp_path):
    leak = _review_block({"summary": "token jira-tok-s3cret leaked", "findings": [], "checks": []})
    _jira_review_setup(monkeypatch, tmp_path, output=leak)
    res = _review(["--engine", "claude", "--no-publish"])
    assert res.exit_code == 0, res.output
    assert "jira-tok-s3cret" not in res.stdout
    log_path = json.loads(res.stdout)["log_path"]
    assert "jira-tok-s3cret" not in Path(log_path).read_text()


def test_review_pr_jira_context_with_instructions_file(monkeypatch, tmp_path):
    prompts, _ = _jira_review_setup(monkeypatch, tmp_path)
    source = tmp_path / "instr.txt"
    source.write_text("focus on caps")
    source.chmod(0o600)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", "--instructions-file", str(source), "--no-publish", "--json", "--", PR_URL],
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["context_sources"][0]["status"] == "included"


# ---------------------------------------------------------------------------
# review-pr --allow-approve / --resolve-fixed (host-side event choice, fallback, thread resolve)
# ---------------------------------------------------------------------------

REFUSED = "GitHub wrapper refused: approve is not allowed"
THREADS_PAGE = lambda nodes, nxt=None: json.dumps(  # noqa: E731
    {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": nxt is not None, "endCursor": nxt},
                        "nodes": nodes,
                    }
                }
            }
        }
    }
)
THREAD_NODE = {
    "id": "PRRT_1",
    "isResolved": False,
    "path": "a.py",
    "comments": {
        "nodes": [
            {"author": {"login": "franky-bot", "__typename": "Bot"}, "body": "**Major: race**\n\nb"}
        ]
    },
}
CLEAN = {"summary": "ok", "findings": [], "checks": []}


class _Gh:
    """Scripted `gh` for _publish_review: `posts` and `reviews`/`graphql` replies are queues."""

    def __init__(self, monkeypatch, *, heads=None, posts=(), reviews=None, graphql=()):
        self.calls, self.inputs = [], []
        self.posts, self.graphql = list(posts), list(graphql)
        self.reviews = reviews if reviews is not None else (0, "[]", "")
        self.heads = list(heads) if heads is not None else []
        self.sleeps = []
        monkeypatch.setattr(cli, "_sleep", self.sleeps.append)
        monkeypatch.setattr(cli.os, "environ", _review_env())
        monkeypatch.setattr(cli, "fetch_pr_head_sha", self._head)
        monkeypatch.setattr(cli, "run_gh", self._run)

    def _head(self, *a, **k):
        return self.heads.pop(0) if self.heads else LIVE_SHA

    def _run(self, args, env, **k):
        self.calls.append(list(args))
        self.inputs.append(k.get("input"))
        if args[1].endswith("/files?per_page=100"):
            return 0, "[[]]", ""
        if args[1].endswith("/reviews?per_page=100"):
            return self.reviews() if callable(self.reviews) else self.reviews
        if args[1] == "graphql":
            reply = self.graphql.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        reply = self.posts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post_events(self):
        return [json.loads(i)["event"] for c, i in zip(self.calls, self.inputs) if i]


OK_POST = (
    0,
    json.dumps({"html_url": REVIEW_URL, "id": 555, "user": {"login": "franky-bot[bot]"}}),
    "",
)


def _pub(shaped, **kw):
    return cli._publish_review("me/repo", 11, PR_URL, LIVE_SHA, shaped, [], **kw)


def _clean():
    return build_review_findings(CLEAN)


def test_publish_approves_when_allowed_and_clean(monkeypatch):
    gh = _Gh(monkeypatch, posts=[OK_POST])
    out = _pub(_clean(), allow_approve=True)
    assert out[0] == "review_published" and out[5] == "APPROVE" and out[6] is None
    assert gh.post_events() == ["APPROVE"]
    assert "<!-- franky-review:" in json.loads(gh.inputs[-1])["body"]


def test_publish_default_never_approves(monkeypatch):
    gh = _Gh(monkeypatch, posts=[OK_POST])
    out = _pub(_clean())
    assert out[5] == "COMMENT" and gh.post_events() == ["COMMENT"]
    assert "franky-review:" not in json.loads(gh.inputs[-1])["body"]


@pytest.mark.parametrize(
    "refusal",
    [(1, "", REFUSED), (1, "", "gh: Unprocessable Entity (HTTP 422)")],
)
def test_publish_confirmed_refusal_downgrades_to_comment(monkeypatch, refusal):
    gh = _Gh(monkeypatch, posts=[refusal, OK_POST])
    out = _pub(_clean(), allow_approve=True)
    assert out[0] == "review_published" and out[5] == "COMMENT"
    assert gh.post_events() == ["APPROVE", "COMMENT"]
    assert not any(c[1].endswith("/reviews?per_page=100") for c in gh.calls)  # no reconcile


def test_publish_refusal_then_head_moved_publishes_nothing(monkeypatch):
    gh = _Gh(monkeypatch, heads=[LIVE_SHA, LIVE_SHA, "b" * 40], posts=[(1, "", REFUSED)])
    out = _pub(_clean(), allow_approve=True)
    assert out[0] == "publish_blocked_stale_head" and gh.post_events() == ["APPROVE"]


@pytest.mark.parametrize(
    "uncertain",
    [(1, "", "boom"), (0, "not json", ""), subprocess.TimeoutExpired(["gh"], 5)],
)
def test_publish_uncertain_approve_reconciles_before_any_retry(monkeypatch, uncertain):
    gh = _Gh(monkeypatch, posts=[uncertain])

    def reviews():
        marker = (
            json.loads([i for i in gh.inputs if i][-1])["body"].split("<!-- ")[1].split(" -->")[0]
        )
        found = {"id": 9, "commit_id": LIVE_SHA, "state": "APPROVED", "html_url": "u"}
        return 0, json.dumps([[{**found, "body": f"x <!-- {marker} -->"}]]), ""

    gh.reviews = reviews
    out = _pub(_clean(), allow_approve=True)
    assert out[0] == "review_published" and out[5] == "APPROVE" and out[4] == 9
    assert gh.post_events() == ["APPROVE"]  # no second POST


def test_publish_uncertain_approve_not_found_posts_comment_once(monkeypatch):
    gh = _Gh(monkeypatch, posts=[(1, "", "boom"), OK_POST], reviews=(0, "[[]]", ""))
    out = _pub(_clean(), allow_approve=True)
    assert out[5] == "COMMENT" and gh.post_events() == ["APPROVE", "COMMENT"]
    reads = [c for c in gh.calls if c[1].endswith("/reviews?per_page=100")]
    assert len(reads) == 2 and len(gh.sleeps) == 1  # absent needs two reads


def test_publish_reconcile_ignores_other_commit_and_marker(monkeypatch):
    other = [
        [{"id": 1, "commit_id": "c" * 40, "state": "APPROVED", "body": "<!-- franky-review:zz -->"}]
    ]
    gh = _Gh(monkeypatch, posts=[(1, "", "boom"), OK_POST], reviews=(0, json.dumps(other), ""))
    assert _pub(_clean(), allow_approve=True)[5] == "COMMENT"
    assert gh.post_events() == ["APPROVE", "COMMENT"]


def test_publish_422_after_downgrade_keeps_comment(monkeypatch):
    shaped = build_review_findings(
        {"summary": "s", "findings": [{"title": "n", "severity": "nit", "file": "a.py", "line": 1}]}
    )
    gh = _Gh(
        monkeypatch,
        posts=[(1, "", REFUSED), (1, "", "gh: Unprocessable Entity (HTTP 422)"), OK_POST],
    )
    # an anchored nit makes the COMMENT carry an inline comment, so the 422 retry applies
    monkeypatch.setattr(cli, "commentable_lines", lambda files: {"a.py": [(1, 5)]})
    out = _pub(shaped, allow_approve=True)
    assert out[0] == "review_published" and out[5] == "COMMENT"
    assert gh.post_events() == ["APPROVE", "COMMENT", "COMMENT"]
    assert json.loads(gh.inputs[-1])["comments"] == []


def test_publish_failure_without_approve_is_unchanged(monkeypatch):
    gh = _Gh(monkeypatch, posts=[(1, "", "boom")])
    out = _pub(_clean())
    assert out[0] == "publish_failed" and out[5] is None and gh.post_events() == ["COMMENT"]


RESOLVED_SHAPED = lambda **kw: build_review_findings(  # noqa: E731
    {
        "summary": "s",
        "findings": [
            {
                "title": "race",
                "severity": "normal",
                "file": "a.py",
                "line": 3,
                "status": "resolved",
                **kw,
            }
        ],
    },
    threaded=True,
)
GQL_BASE = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", "owner=me",
            "-F", "name=repo", "-F", "number=11"]  # fmt: skip
RESOLVE_ARGV = ["api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", "id=PRRT_1"]
MUTATION_OK = (
    0,
    json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
    "",
)


def test_resolve_fixed_exact_argv_and_count(monkeypatch):
    gh = _Gh(
        monkeypatch, posts=[OK_POST], graphql=[(0, THREADS_PAGE([THREAD_NODE]), ""), MUTATION_OK]
    )
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "review_published" and out[6] == 1
    graphql = [c for c in gh.calls if c[1] == "graphql"]
    assert graphql == [GQL_BASE, RESOLVE_ARGV]


def test_resolve_fixed_pages_with_cursor(monkeypatch):
    other = {**THREAD_NODE, "id": "PRRT_2", "path": "z.py"}
    gh = _Gh(
        monkeypatch,
        posts=[OK_POST],
        graphql=[
            (0, THREADS_PAGE([other], "CUR1"), ""),
            (0, THREADS_PAGE([THREAD_NODE]), ""),
            MUTATION_OK,
        ],
    )
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[6] == 1
    graphql = [c for c in gh.calls if c[1] == "graphql"]
    assert graphql == [GQL_BASE, [*GQL_BASE, "-F", "after=CUR1"], RESOLVE_ARGV]


def test_resolve_fixed_skipped_when_off_open_or_head_moved(monkeypatch):
    _Gh(monkeypatch, posts=[OK_POST])
    assert _pub(RESOLVED_SHAPED())[6] is None  # flag off
    _Gh(monkeypatch, posts=[OK_POST])
    assert _pub(RESOLVED_SHAPED(status="open"), resolve_fixed=True)[6] is None
    gh = _Gh(monkeypatch, heads=[LIVE_SHA, LIVE_SHA, "b" * 40], posts=[OK_POST])
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "review_published" and out[6] is None
    assert not [c for c in gh.calls if c[1] == "graphql"]


def test_resolve_fixed_failures_never_fail_the_run(monkeypatch):
    _Gh(
        monkeypatch,
        posts=[OK_POST],
        graphql=[(0, THREADS_PAGE([THREAD_NODE]), ""), (1, "", REFUSED)],
    )
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "review_published" and out[6] == 0
    _Gh(monkeypatch, posts=[OK_POST], graphql=[(0, "not json", "")])
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "review_published" and out[6] == 0


def test_resolve_fixed_not_run_when_post_fails(monkeypatch):
    gh = _Gh(monkeypatch, posts=[(1, "", "boom")])
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "publish_failed" and out[6] is None
    assert not [c for c in gh.calls if c[1] == "graphql"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--allow-approve", "--no-publish"],
        ["--resolve-fixed", "--no-publish", "--thread"],
        ["--resolve-fixed"],
    ],
)
def test_review_pr_flag_validation_refusals(monkeypatch, flags):
    _mc_review_setup(monkeypatch)
    res = _review(flags)
    assert res.exit_code == 2, res.output


def test_review_pr_allow_approve_reports_event(monkeypatch):
    _fix_review_nonce(monkeypatch)
    calls = _mc_review_setup(monkeypatch)
    res = _review(["--allow-approve"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["review_event"] == "APPROVE" and "threads_resolved" not in data
    assert json.loads(calls.inputs[0])["event"] == "APPROVE"


def test_review_pr_default_reports_comment_event(monkeypatch):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch)
    data = json.loads(_review([]).stdout)
    assert data["review_event"] == "COMMENT"


def test_review_pr_flags_visible_in_help():
    out = CliRunner().invoke(cli.main, ["review-pr", "--help"]).output
    assert "--allow-approve" in out and "--resolve-fixed" in out


def test_graphql_read_never_passes_dash_x(monkeypatch):
    gh = _Gh(
        monkeypatch, posts=[OK_POST], graphql=[(0, THREADS_PAGE([THREAD_NODE]), ""), MUTATION_OK]
    )
    _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    reads = [c for c in gh.calls if c[1] == "graphql" and f"query={THREADS_QUERY}" in c]
    assert reads and all("-X" not in c for c in reads)


def _comment_shaped():
    return build_review_findings(
        {
            "summary": "s",
            "findings": [{"title": "n", "severity": "nit", "file": "a.py", "line": 1}],
        }
    )


def test_publish_approve_422_retries_body_only_approve_first(monkeypatch):
    monkeypatch.setattr(cli, "commentable_lines", lambda files: {"a.py": [(1, 5)]})
    gh = _Gh(monkeypatch, posts=[(1, "", "gh: Unprocessable Entity (HTTP 422)"), OK_POST])
    out = _pub(_comment_shaped(), allow_approve=True)
    assert out[0] == "review_published" and out[5] == "APPROVE"
    assert gh.post_events() == ["APPROVE", "APPROVE"]
    assert json.loads(gh.inputs[-1])["comments"] == []


def test_publish_body_only_approve_refused_falls_back_to_comment(monkeypatch):
    monkeypatch.setattr(cli, "commentable_lines", lambda files: {"a.py": [(1, 5)]})
    gh = _Gh(
        monkeypatch,
        posts=[(1, "", "gh: Unprocessable Entity (HTTP 422)"), (1, "", REFUSED), OK_POST],
    )
    out = _pub(_comment_shaped(), allow_approve=True)
    assert out[5] == "COMMENT" and gh.post_events() == ["APPROVE", "APPROVE", "COMMENT"]


def test_publish_reconcile_unknown_reposts_nothing(monkeypatch):
    for reviews in ((1, "", "boom"), (0, "not json", ""), (0, "{}", "")):
        gh = _Gh(monkeypatch, posts=[(1, "", "boom")], reviews=reviews)
        out = _pub(_clean(), allow_approve=True)
        assert out[0] == "publish_uncertain" and out[2] != 0 and out[5] is None
        assert gh.post_events() == ["APPROVE"]


def test_publish_reconcile_read_timeout_is_unknown(monkeypatch):
    gh = _Gh(
        monkeypatch,
        posts=[(1, "", "boom")],
        reviews=lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired(["gh"], 5)),
    )
    assert _pub(_clean(), allow_approve=True)[0] == "publish_uncertain"
    assert gh.post_events() == ["APPROVE"]


def test_publish_head_moved_after_uncertain_approve_is_uncertain(monkeypatch):
    gh = _Gh(
        monkeypatch,
        heads=[LIVE_SHA, LIVE_SHA, "b" * 40],
        posts=[(1, "", "boom")],
        reviews=(0, "[[]]", ""),
    )
    out = _pub(_clean(), allow_approve=True)
    assert out[0] == "publish_uncertain" and gh.post_events() == ["APPROVE"]


def test_bare_422_substring_is_not_a_confirmed_refusal():
    assert cli._review_confirmed_unwritten(1, "gh: Unprocessable Entity (HTTP 422)")
    assert cli._review_confirmed_unwritten(1, REFUSED)
    assert not cli._review_confirmed_unwritten(1, "request id 4221 failed")
    assert not cli._review_confirmed_unwritten(1, "timed out")


def test_failed_check_blocks_approve_in_publish(monkeypatch):
    shaped = build_review_findings(
        {"summary": "s", "findings": [], "checks": [{"name": "t", "outcome": "fail"}]}
    )
    gh = _Gh(monkeypatch, posts=[OK_POST])
    assert _pub(shaped, allow_approve=True)[5] == "COMMENT" and gh.post_events() == ["COMMENT"]


def test_resolve_uses_poster_login_from_post_response(monkeypatch):
    no_user = (0, json.dumps({"html_url": REVIEW_URL, "id": 555}), "")
    gh = _Gh(monkeypatch, posts=[no_user])
    assert _pub(RESOLVED_SHAPED(), resolve_fixed=True)[6] == 0
    assert not [c for c in gh.calls if c[1] == "graphql"]  # unknown login: nothing read or resolved
    other = {
        **THREAD_NODE,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "someone-else", "__typename": "Bot"},
                    "body": "**Major: race**\n\nb",
                }
            ]
        },
    }
    _Gh(monkeypatch, posts=[OK_POST], graphql=[(0, THREADS_PAGE([other]), "")])
    assert _pub(RESOLVED_SHAPED(), resolve_fixed=True)[6] == 0


def test_resolve_stops_when_thread_list_exceeds_page_limit(monkeypatch):
    pages = [(0, THREADS_PAGE([THREAD_NODE], f"C{i}"), "") for i in range(5)]
    gh = _Gh(monkeypatch, posts=[OK_POST], graphql=pages)
    out = _pub(RESOLVED_SHAPED(), resolve_fixed=True)
    assert out[0] == "review_published" and out[6] == 0
    graphql = [c for c in gh.calls if c[1] == "graphql"]
    assert len(graphql) == 5 and all(f"query={RESOLVE_MUTATION}" not in c for c in graphql)
