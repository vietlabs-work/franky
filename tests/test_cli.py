import franky.cli as cli
from click.testing import CliRunner

from franky import __version__

PR_URL = "https://github.com/me/repo/pull/11"


def test_help_exit_zero():
    res = CliRunner().invoke(cli.main, ["--help"])
    assert res.exit_code == 0


def test_version_prints_version():
    res = CliRunner().invoke(cli.main, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.output


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
    monkeypatch.setattr(cli, "ensure_image", lambda *a, **k: True)
    monkeypatch.setattr(cli, "run_in_container", lambda *a, **k: (0, f"opened {PR_URL}"))

    runner = CliRunner()
    with runner.isolated_filesystem():  # keep the tasks/ log out of the repo
        res = runner.invoke(cli.main, ["build", "add a --json flag", "--repo", "me/repo"])
    assert res.exit_code == 0, res.output
    assert PR_URL in res.output


def test_build_engine_flag_selects_engine(monkeypatch):
    # --engine claude must reach config + select ClaudeEngine end-to-end through the CLI.
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image", lambda *a, **k: True)

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
    monkeypatch.setattr(cli, "ensure_image", lambda *a, **k: True)
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


def test_build_off_allowlist_clean_error(monkeypatch):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image", lambda *a, **k: True)
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
