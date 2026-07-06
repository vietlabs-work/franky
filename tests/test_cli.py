import json
import os
import subprocess
from pathlib import Path

import franky.cli as cli
import franky.jobs as jobs
from click.testing import CliRunner

from franky import __version__
from franky._install import Install

PR_URL = "https://github.com/me/repo/pull/11"


def test_help_exit_zero():
    res = CliRunner().invoke(cli.main, ["--help"])
    assert res.exit_code == 0


def test_version_prints_version():
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.output


# ---------------------------------------------------------------------------
# Enriched version command
# ---------------------------------------------------------------------------


def _make_env(extra=None):
    """Minimal env that resolves engine cleanly (defaults to pi, no creds needed for version)."""
    env = {}
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
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["engine"] = cfg.engine.name
        seen["argv0"] = inner_argv[0]
        return 0, f"opened {PR_URL}"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--engine", "claude"])
    assert res.exit_code == 0, res.output
    assert seen["engine"] == "claude"
    assert seen["argv0"] == "claude"


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
        # The log file must also contain the economics text.
        from pathlib import Path

        logs = list(Path("tasks").glob("*.log"))
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
        from pathlib import Path

        logs = list(Path("tasks").glob("*.log"))
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
        logs = list(Path("tasks").glob("*.log"))
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


def test_iterate_engine_flag_selects_engine(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")

    seen = {}

    def fake_run(cfg, inner_argv, *a, **k):
        seen["engine"] = cfg.engine.name
        seen["argv0"] = inner_argv[0]
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)

    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL, "--engine", "claude"])
    assert res.exit_code == 0, res.output
    assert seen["engine"] == "claude"
    assert seen["argv0"] == "claude"


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
        logs = list(Path("tasks").glob("*.log"))
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
    write_config_file(cfg_path, {"GH_TOKEN": "ghp_real", "FRANKY_ENGINE": "pi"})
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    res = CliRunner().invoke(cli.main, ["config", "list"])
    assert res.exit_code == 0, res.output
    assert "ghp_real" not in res.output
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


def test_config_init_codex_wizard_falls_back_to_openai(tmp_path, monkeypatch):
    cfg_path = tmp_path / "franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    wizard_input = (
        "codex\n"  # engine
        "me/repo\n"  # FRANKY_ALLOWED_REPOS
        "ghp_wiz\n"  # GH_TOKEN
        "\n"  # CODEX_API_KEY empty -> fall through to OPENAI_API_KEY
        "sk-openai\n"  # OPENAI_API_KEY
        "n\n"  # JIRA: no
        "n\n"  # profile: no
    )
    res = CliRunner().invoke(cli.main, ["config", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    from franky.userconfig import read_config_file

    data = read_config_file(cfg_path)
    assert data["OPENAI_API_KEY"] == "sk-openai"
    assert "CODEX_API_KEY" not in data


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
    assert called["progress_passed"] is None  # no progress callback under --quiet


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
    # skills, instructions, knowledge prompts
    wizard_input = "~/.claude/skills/*.md\n~/.claude/CLAUDE.md\n\n"
    res = CliRunner().invoke(cli.main, ["profile", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    raw = read_profile_raw(prof)
    assert raw["skills"] == ["~/.claude/skills/*.md"]
    assert raw["instructions"] == ["~/.claude/CLAUDE.md"]
    assert "knowledge" not in raw


def test_profile_init_merges_existing(tmp_path, monkeypatch):
    from franky.profile import read_profile_raw

    prof = tmp_path / "profile.toml"
    prof.write_text('[profile]\nskills = ["~/existing.md"]\n', encoding="utf-8")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    wizard_input = "~/new.md\n\n\n"  # add a skill, skip instructions + knowledge
    res = CliRunner().invoke(cli.main, ["profile", "init"], input=wizard_input)
    assert res.exit_code == 0, res.output
    assert read_profile_raw(prof)["skills"] == ["~/existing.md", "~/new.md"]


def test_config_init_profile_prompt_yes_writes_both(tmp_path, monkeypatch):
    from franky.profile import read_profile_raw
    from franky.userconfig import read_config_file

    cfg_path = tmp_path / "franky-config"
    prof_path = tmp_path / "profile.toml"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof_path))
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
    data = json.loads(res.output)
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
    data = json.loads(res.output)
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
    data = json.loads(res.output)
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


def _diag_setup(monkeypatch, tmp_path, container_result, nonce="fixednonce"):
    env = _build_env()
    env["FRANKY_RUNS_DIR"] = str(tmp_path / "runs")
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: container_result)
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
    env = _diag_setup(monkeypatch, tmp_path, (0, output), nonce)
    job_id = _write_failed_run(env, tmp_path)
    res = CliRunner().invoke(cli.main, ["job", "diagnose", job_id, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["root_cause"] == "tests were red"
    assert data["category"] == "test_failure" and data["retryable"] is True
    assert data["job_id"] == job_id


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


def _replay_env(monkeypatch, tmp_path, container_result, allowed_repos="me/repo"):
    env = {
        "FRANKY_ALLOWED_REPOS": allowed_repos,
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: container_result)
    monkeypatch.setattr(cli.baseref, "commit_exists", lambda *a, **k: True)
    return env


def test_job_replay_reproduce_only_happy_path(monkeypatch, tmp_path):
    env = _replay_env(monkeypatch, tmp_path, (0, "reproduced the failure"))
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
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
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
