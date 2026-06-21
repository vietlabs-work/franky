import pytest

from franky.config import Config, load_config, redact
from franky.engine import ClaudeEngine, PiEngine

SECRET = "sk-super-secret-value-123"


def test_redact_replaces_secret_value():
    text = f"the key is {SECRET} ok"
    out = redact(text, [SECRET])
    assert SECRET not in out
    assert "***REDACTED***" in out


def test_redact_safe_on_empty_secret():
    text = "nothing secret here"
    assert redact(text, ["", None]) == text


def test_redact_safe_on_empty_text():
    assert redact("", [SECRET]) == ""


def _env(**extra):
    base = {
        "FRANKY_ALLOWED_REPOS": "me/repo, me/other",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }
    base.update(extra)
    return base


def test_load_config_happy_path_pi():
    cfg = load_config(None, _env())
    assert isinstance(cfg.engine, PiEngine)
    assert cfg.allowed_repos == ["me/repo", "me/other"]
    assert cfg.passthrough_env["GH_TOKEN"] == "ghp_fake"
    assert cfg.passthrough_env["OPENROUTER_API_KEY"] == "sk-or-fake"


def test_load_config_engine_resolution_claude():
    env = _env(CLAUDE_CODE_OAUTH_TOKEN="oauth-fake")
    cfg = load_config("claude", env)
    assert isinstance(cfg.engine, ClaudeEngine)
    assert cfg.passthrough_env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-fake"


def test_fail_closed_allowlist_unset():
    env = _env()
    del env["FRANKY_ALLOWED_REPOS"]
    with pytest.raises(ValueError, match="FRANKY_ALLOWED_REPOS"):
        load_config(None, env)


def test_fail_closed_allowlist_blank():
    with pytest.raises(ValueError, match="FRANKY_ALLOWED_REPOS"):
        load_config(None, _env(FRANKY_ALLOWED_REPOS="  ,  "))


def test_fail_closed_gh_token_missing():
    env = _env()
    del env["GH_TOKEN"]
    with pytest.raises(ValueError, match="GH_TOKEN"):
        load_config(None, env)


def test_fail_closed_pi_no_provider():
    env = _env()
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(ValueError, match="creds"):
        load_config(None, env)


def test_fail_closed_claude_token_missing():
    # claude selected but no token -> refuse
    with pytest.raises(ValueError, match="creds|CLAUDE_CODE_OAUTH_TOKEN"):
        load_config("claude", _env())


def test_no_secret_value_in_exception_messages():
    # every fail-closed path must name the VAR, never echo a secret value
    env = _env(GH_TOKEN=SECRET, OPENROUTER_API_KEY=SECRET)
    # claude path: token missing, GH_TOKEN present but should not be echoed
    with pytest.raises(ValueError) as ei:
        load_config("claude", env)
    assert SECRET not in str(ei.value)


def test_secret_values_lists_passthrough_values():
    cfg = load_config(None, _env())
    vals = cfg.secret_values()
    assert "ghp_fake" in vals
    assert "sk-or-fake" in vals


def test_config_is_dataclass_like():
    cfg = Config(engine=PiEngine(), allowed_repos=["a/b"], passthrough_env={"GH_TOKEN": "x"})
    assert cfg.secret_values() == ["x"]
