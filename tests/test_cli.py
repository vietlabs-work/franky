import json
import subprocess

import franky.cli as cli
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
    # No stdin to answer the gate (CI / piped) must fail closed: the planning pass runs, the
    # gate aborts (no approval), and the build pass never runs.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
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
    assert res.exit_code != 0  # click aborts the confirm cleanly (no traceback)
    assert calls["n"] == 1  # only the planning pass ran; no build
    assert PR_URL not in res.output


def test_build_plan_first_planning_failure_aborts_before_gate(monkeypatch):
    # A planning pass that errors must NOT proceed to a build pass.
    monkeypatch.setattr(cli.os, "environ", _plan_first_env())
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
