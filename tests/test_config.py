import pytest

import franky.config as config_mod
from franky.config import Config, load_config, redact, repo_allowed, validate_allowlist_entry
from franky.engine import (
    CODEX_AUTH_VOLUME,
    CODEX_SUBSCRIPTION_VAR,
    ClaudeEngine,
    CodexEngine,
    OpenCodeEngine,
    PiEngine,
)
from franky.result import ConfigError

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


def test_load_config_engine_resolution_codex():
    # codex selected: only the codex key lands in passthrough; the pi var in _env (OPENROUTER)
    # must NOT, even though it is set - it is not a codex cred.
    env = _env(CODEX_API_KEY="sk-codex-fake")
    cfg = load_config("codex", env)
    assert isinstance(cfg.engine, CodexEngine)
    assert cfg.passthrough_env["CODEX_API_KEY"] == "sk-codex-fake"
    assert "OPENROUTER_API_KEY" not in cfg.passthrough_env


def test_load_config_opencode_propagates_model_and_isolates_credentials():
    env = _env(
        FRANKY_MODEL="openrouter/anthropic/claude-x",
        MOONSHOT_API_KEY="sk-moonshot-fake",
        OPENAI_API_KEY="sk-openai-fake",
        CLAUDE_CODE_OAUTH_TOKEN="oauth-fake",
    )
    cfg = load_config("opencode", env)
    assert isinstance(cfg.engine, OpenCodeEngine)
    assert cfg.model == "openrouter/anthropic/claude-x"
    assert cfg.passthrough_env == {
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
    }


def test_load_config_propagates_model_for_other_engines_without_opencode_validation():
    cfg = load_config(None, _env(FRANKY_MODEL="provider/model with spaces"))
    assert cfg.model == "provider/model with spaces"


@pytest.mark.parametrize(
    "model",
    [
        "",
        "openrouter",
        "openrouter/",
        "openrouter//model",
        "openrouter/model/",
        "openrouter/model id",
        "openrouter/ model",
        "other/model",
    ],
)
def test_load_config_opencode_rejects_missing_or_invalid_model(model):
    with pytest.raises(ConfigError, match="FRANKY_MODEL"):
        load_config("opencode", _env(FRANKY_MODEL=model))


def test_load_config_opencode_accepts_nested_nonempty_model_id():
    cfg = load_config("opencode", _env(FRANKY_MODEL="openrouter/anthropic/claude-x"))
    assert cfg.model == "openrouter/anthropic/claude-x"


def test_load_config_opencode_accepts_direct_moonshot_without_rewriting_model():
    env = _env(FRANKY_MODEL="moonshotai/kimi-k3", MOONSHOT_API_KEY="sk-moonshot-fake")
    cfg = load_config("opencode", env)
    assert cfg.model == "moonshotai/kimi-k3"
    assert cfg.passthrough_env == {
        "GH_TOKEN": "ghp_fake",
        "MOONSHOT_API_KEY": "sk-moonshot-fake",
    }


def test_load_config_opencode_direct_moonshot_rejects_openrouter_key():
    env = _env(FRANKY_MODEL="moonshotai/kimi-k3")
    with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
        load_config("opencode", env)


def test_load_config_opencode_openrouter_rejects_moonshot_key():
    env = _env(FRANKY_MODEL="openrouter/anthropic/claude-x", MOONSHOT_API_KEY="moon")
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY") as exc:
        load_config("opencode", env)
    assert "MOONSHOT_API_KEY" not in str(exc.value)


def test_load_config_codex_subscription_uses_fixed_volume():
    env = _env(**{CODEX_SUBSCRIPTION_VAR: "1"})
    cfg = load_config("codex", env)
    assert cfg.auth_volume == CODEX_AUTH_VOLUME
    assert cfg.passthrough_env == {"GH_TOKEN": "ghp_fake"}


def test_load_config_codex_api_key_takes_precedence_over_subscription():
    env = _env(CODEX_API_KEY="sk-codex-fake", **{CODEX_SUBSCRIPTION_VAR: "1"})
    cfg = load_config("codex", env)
    assert cfg.auth_volume is None
    assert cfg.passthrough_env["CODEX_API_KEY"] == "sk-codex-fake"


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
    with pytest.raises(ValueError, match="creds") as ei:
        load_config(None, env)
    # The refusal hint must be sourced from the pi engine (cred_hint), naming pi's vars -
    # not hardcoded in shared config. A concrete var proves the wiring, not just non-emptiness.
    assert "OPENROUTER_API_KEY" in str(ei.value)
    assert "engine 'pi'" in str(ei.value)


def test_fail_closed_codex_no_provider():
    # OPENAI_API_KEY is valid for pi but must not satisfy codex exec's single-run auth.
    env = _env(OPENAI_API_KEY="sk-openai-fake")
    with pytest.raises(ValueError, match="creds") as ei:
        load_config("codex", env)
    msg = str(ei.value)
    assert "engine 'codex'" in msg
    assert "CODEX_API_KEY" in msg
    assert "OPENROUTER_API_KEY" not in msg  # no pi vars leak into the codex refusal
    assert "auth login codex" in msg


def test_config_no_longer_couples_to_pi_provider_vars():
    # Acceptance (#29): shared config must not import/re-export one engine's provider vars.
    assert not hasattr(config_mod, "PI_PROVIDER_VARS")


def test_fail_closed_claude_token_missing():
    # claude selected but no token -> refuse
    with pytest.raises(ValueError, match="creds|CLAUDE_CODE_OAUTH_TOKEN"):
        load_config("claude", _env())


@pytest.mark.parametrize(
    "flag, env",
    [
        # claude path: token missing, GH_TOKEN present (must not be echoed)
        ("claude", {"FRANKY_ALLOWED_REPOS": "me/repo", "GH_TOKEN": SECRET}),
        # GH_TOKEN missing path: a provider key present (must not be echoed)
        (None, {"FRANKY_ALLOWED_REPOS": "me/repo", "OPENROUTER_API_KEY": SECRET}),
        # allowlist-missing path: secrets present (must not be echoed)
        (None, {"GH_TOKEN": SECRET, "OPENROUTER_API_KEY": SECRET}),
    ],
)
def test_no_secret_value_in_exception_messages(flag, env):
    # every fail-closed path must name the VAR, never echo a secret value
    with pytest.raises(ValueError) as ei:
        load_config(flag, env)
    assert SECRET not in str(ei.value)


def test_secret_values_lists_passthrough_values():
    cfg = load_config(None, _env())
    vals = cfg.secret_values()
    assert "ghp_fake" in vals
    assert "sk-or-fake" in vals


def test_config_is_dataclass_like():
    cfg = Config(engine=PiEngine(), allowed_repos=["a/b"], passthrough_env={"GH_TOKEN": "x"})
    assert cfg.secret_values() == ["x"]


# ---------------------------------------------------------------------------
# repo_allowed - glob / segment-wise matching
# ---------------------------------------------------------------------------


def test_repo_allowed_exact_match():
    assert repo_allowed("my-org/my-repo", ["my-org/my-repo"])


def test_repo_allowed_exact_no_match():
    assert not repo_allowed("my-org/other", ["my-org/my-repo"])


def test_repo_allowed_wildcard_name():
    assert repo_allowed("my-org/anything", ["my-org/*"])


def test_repo_allowed_wildcard_name_does_not_cross_owner():
    # "my-org/*" must NOT match "my-org-evil/x" - the "*" is in the name segment only.
    assert not repo_allowed("my-org-evil/x", ["my-org/*"])


def test_repo_allowed_wildcard_name_does_not_match_other_owner():
    assert not repo_allowed("other/anything", ["my-org/*"])


def test_repo_allowed_prefix_wildcard():
    assert repo_allowed("my-org/team-alpha", ["my-org/team-*"])
    assert repo_allowed("my-org/team-beta", ["my-org/team-*"])
    assert not repo_allowed("my-org/prod-repo", ["my-org/team-*"])


def test_repo_allowed_global_wildcard_matches_everything():
    assert repo_allowed("any-org/any-repo", ["*"])
    assert repo_allowed("owner/repo", ["*"])


def test_repo_allowed_case_insensitive():
    assert repo_allowed("Owner/Repo", ["owner/repo"])
    assert repo_allowed("owner/repo", ["Owner/Repo"])
    assert repo_allowed("MY-ORG/MY-REPO", ["my-org/*"])


def test_repo_allowed_multiple_patterns_first_match_wins():
    assert repo_allowed("a/b", ["x/y", "a/b", "a/*"])
    assert repo_allowed("a/z", ["x/y", "a/b", "a/*"])


def test_repo_allowed_no_match_returns_false():
    assert not repo_allowed("stranger/repo", ["my-org/*", "other/x"])


def test_repo_allowed_malformed_repo_returns_false():
    # A bare owner with no slash should never match.
    assert not repo_allowed("badowner", ["badowner/*"])


def test_repo_allowed_multi_slash_repo_does_not_satisfy_owner_glob():
    # A multi-slash repo must NOT sneak past "owner/*" by matching the trailing
    # "sub/path" against the name glob - repo must be exactly owner/name.
    assert not repo_allowed("owner/sub/path", ["owner/*"])
    assert not repo_allowed("owner/sub/path", ["*"])
    assert not repo_allowed("owner/", ["owner/*"])
    assert not repo_allowed("/repo", ["*"])


# ---------------------------------------------------------------------------
# validate_allowlist_entry
# ---------------------------------------------------------------------------


def test_validate_allowlist_entry_exact():
    validate_allowlist_entry("owner/repo")  # should not raise


def test_validate_allowlist_entry_wildcard_name():
    validate_allowlist_entry("owner/*")  # should not raise


def test_validate_allowlist_entry_global_wildcard():
    validate_allowlist_entry("*")  # should not raise


def test_validate_allowlist_entry_prefix_glob():
    validate_allowlist_entry("my-org/team-*")  # should not raise


@pytest.mark.parametrize(
    "bad_entry",
    [
        "foo",  # no slash
        "a/b/c",  # two slashes
        "a/",  # empty name segment
        "/b",  # empty owner segment
        "owner /repo",  # whitespace
        "",  # empty
        "a/ b",  # whitespace in name
        "*/repo",  # owner glob - only the bare "*" may match across owners
        "*/*",  # owner glob
        "my-*/repo",  # partial owner glob
    ],
)
def test_validate_allowlist_entry_rejects_bad(bad_entry: str) -> None:
    with pytest.raises(ValueError):
        validate_allowlist_entry(bad_entry)


def test_load_config_rejects_malformed_allowlist_entry(monkeypatch):
    """A malformed entry in FRANKY_ALLOWED_REPOS must raise at load_config time."""

    def _env(**extra):
        base = {
            "FRANKY_ALLOWED_REPOS": "me/repo",
            "GH_TOKEN": "ghp_fake",
            "OPENROUTER_API_KEY": "sk-or-fake",
        }
        base.update(extra)
        return base

    with pytest.raises(ValueError, match="invalid allowlist entry"):
        load_config(None, _env(FRANKY_ALLOWED_REPOS="just-owner"))
