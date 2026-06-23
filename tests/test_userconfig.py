"""Unit tests for franky/userconfig.py.

All tests are hermetic: no real ~/.franky/config is ever read (the autouse fixture
in conftest.py points FRANKY_CONFIG_FILE at a non-existent tmp path), and all writes
use tmp_path fixtures.
"""

import stat
from pathlib import Path

import pytest

from franky.config import GH_TOKEN_VAR, REDACT_TOKEN
from franky.engine import CLAUDE_TOKEN_VAR, CODEX_PROVIDER_VARS, PI_PROVIDER_VARS
from franky.jira import JIRA_API_TOKEN_VAR
from franky.userconfig import (
    SECRET_KEYS,
    SETTABLE_KEYS,
    config_file_path,
    load_config_file,
    mask_value,
    read_config_file,
    set_value,
    write_config_file,
)


# ---------------------------------------------------------------------------
# config_file_path
# ---------------------------------------------------------------------------


def test_config_file_path_override(tmp_path: Path) -> None:
    override = str(tmp_path / "my-config")
    assert config_file_path({"FRANKY_CONFIG_FILE": override}) == Path(override)


def test_config_file_path_default() -> None:
    # Without the override we should get ~/.franky/config.
    path = config_file_path({})
    assert path == Path.home() / ".franky" / "config"


# ---------------------------------------------------------------------------
# read_config_file
# ---------------------------------------------------------------------------


def test_read_absent_file_returns_empty(tmp_path: Path) -> None:
    assert read_config_file(tmp_path / "missing") == {}


def test_read_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"GH_TOKEN": "ghp_test", "FRANKY_ENGINE": "pi"})
    data = read_config_file(p)
    assert data["GH_TOKEN"] == "ghp_test"
    assert data["FRANKY_ENGINE"] == "pi"


def test_read_malformed_toml_raises(tmp_path: Path) -> None:
    p = tmp_path / "config"
    p.write_text("[franky\nbroken = ", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed TOML"):
        read_config_file(p)


def test_read_missing_table_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "config"
    p.write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
    assert read_config_file(p) == {}


def test_read_coerces_to_str(tmp_path: Path) -> None:
    # A hand-edited file might contain an integer value; we coerce defensively.
    p = tmp_path / "config"
    p.write_text('[franky]\nFRANKY_ENGINE = "pi"\n', encoding="utf-8")
    data = read_config_file(p)
    assert isinstance(data["FRANKY_ENGINE"], str)


# ---------------------------------------------------------------------------
# write_config_file - content
# ---------------------------------------------------------------------------


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "dir" / "config"
    write_config_file(p, {"K": "v"})
    assert p.exists()


def test_write_basic_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config"
    original = {"GH_TOKEN": "tok", "FRANKY_ENGINE": "claude"}
    write_config_file(p, original)
    assert read_config_file(p) == original


def test_write_escapes_double_quote(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"K": 'say "hi"'})
    data = read_config_file(p)
    assert data["K"] == 'say "hi"'


def test_write_escapes_backslash(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"K": "C:\\Users\\foo"})
    data = read_config_file(p)
    assert data["K"] == "C:\\Users\\foo"


def test_write_rejects_newline_in_value(tmp_path: Path) -> None:
    p = tmp_path / "config"
    with pytest.raises(ValueError, match="control character"):
        write_config_file(p, {"K": "line1\nline2"})


def test_write_rejects_carriage_return_in_value(tmp_path: Path) -> None:
    p = tmp_path / "config"
    with pytest.raises(ValueError, match="control character"):
        write_config_file(p, {"K": "line1\rline2"})


# ---------------------------------------------------------------------------
# write_config_file - permissions
# ---------------------------------------------------------------------------


def test_write_file_mode_is_0600(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"K": "v"})
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_write_parent_dir_mode_is_0700(tmp_path: Path) -> None:
    franky_dir = tmp_path / ".franky"
    p = franky_dir / "config"
    write_config_file(p, {"K": "v"})
    mode = stat.S_IMODE(franky_dir.stat().st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


# ---------------------------------------------------------------------------
# write_config_file - atomicity (other keys preserved)
# ---------------------------------------------------------------------------


def test_write_atomic_preserves_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"A": "1", "B": "2"})
    # Overwrite with a different dict that changes only A.
    data = read_config_file(p)
    data["A"] = "updated"
    write_config_file(p, data)
    final = read_config_file(p)
    assert final["A"] == "updated"
    assert final["B"] == "2"


# ---------------------------------------------------------------------------
# set_value
# ---------------------------------------------------------------------------


def test_set_value_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "config"
    set_value(p, "GH_TOKEN", "ghp_new")
    assert read_config_file(p)["GH_TOKEN"] == "ghp_new"


def test_set_value_preserves_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"FRANKY_ENGINE": "pi", "GH_TOKEN": "old"})
    set_value(p, "GH_TOKEN", "new")
    data = read_config_file(p)
    assert data["GH_TOKEN"] == "new"
    assert data["FRANKY_ENGINE"] == "pi"


# ---------------------------------------------------------------------------
# load_config_file - setdefault semantics
# ---------------------------------------------------------------------------


def test_load_config_file_injects_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "config"
    write_config_file(p, {"FRANKY_ENGINE": "claude", "GH_TOKEN": "ghp_file"})
    env: dict[str, str] = {}
    load_config_file(env, path=p)
    assert env["FRANKY_ENGINE"] == "claude"
    assert env["GH_TOKEN"] == "ghp_file"


def test_load_config_file_process_env_wins(tmp_path: Path) -> None:
    """setdefault: process env value must NOT be overwritten by the file."""
    p = tmp_path / "config"
    write_config_file(p, {"FRANKY_ENGINE": "claude"})
    env: dict[str, str] = {"FRANKY_ENGINE": "pi"}  # already set
    load_config_file(env, path=p)
    assert env["FRANKY_ENGINE"] == "pi"  # original wins


def test_load_config_file_absent_file_noop(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent"
    env: dict[str, str] = {}
    load_config_file(env, path=p)  # should not raise
    assert env == {}


def test_load_config_file_malformed_raises(tmp_path: Path) -> None:
    p = tmp_path / "config"
    p.write_text("[franky\nbroken", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed TOML"):
        load_config_file({}, path=p)


# ---------------------------------------------------------------------------
# SECRET_KEYS - guard against drift from imported constants
# ---------------------------------------------------------------------------


def test_secret_keys_contains_gh_token() -> None:
    assert GH_TOKEN_VAR in SECRET_KEYS


def test_secret_keys_contains_all_pi_provider_vars_except_ollama() -> None:
    # OLLAMA_HOST is a host URL, not a credential, so it is deliberately excluded.
    for var in PI_PROVIDER_VARS:
        if var == "OLLAMA_HOST":
            continue
        assert var in SECRET_KEYS, f"{var} missing from SECRET_KEYS"


def test_ollama_host_not_secret_but_settable() -> None:
    from franky.userconfig import SETTABLE_KEYS

    assert "OLLAMA_HOST" not in SECRET_KEYS  # a URL, shown unmasked / allowed on argv
    assert "OLLAMA_HOST" in SETTABLE_KEYS


def test_secret_keys_contains_all_codex_provider_vars() -> None:
    for var in CODEX_PROVIDER_VARS:
        assert var in SECRET_KEYS, f"{var} missing from SECRET_KEYS"


def test_secret_keys_contains_claude_token() -> None:
    assert CLAUDE_TOKEN_VAR in SECRET_KEYS


def test_secret_keys_contains_jira_api_token() -> None:
    assert JIRA_API_TOKEN_VAR in SECRET_KEYS


def test_secret_keys_is_exact_union() -> None:
    """SECRET_KEYS must equal the union of the imported constants - no extra, no missing."""
    expected = (
        frozenset({GH_TOKEN_VAR})
        | frozenset(PI_PROVIDER_VARS)
        | frozenset(CODEX_PROVIDER_VARS)
        | frozenset({CLAUDE_TOKEN_VAR})
        | frozenset({JIRA_API_TOKEN_VAR})
    ) - frozenset({"OLLAMA_HOST"})  # the one URL excluded from the credential set
    assert SECRET_KEYS == expected


def test_jira_email_not_in_secret_keys() -> None:
    # JIRA_EMAIL is shown in `list` (not masked) and allowed as a positional argv.
    assert "JIRA_EMAIL" not in SECRET_KEYS


# ---------------------------------------------------------------------------
# mask_value
# ---------------------------------------------------------------------------


def test_mask_value_masks_secret() -> None:
    result = mask_value("GH_TOKEN", "ghp_real_token")
    assert result == REDACT_TOKEN


def test_mask_value_does_not_mask_non_secret() -> None:
    result = mask_value("FRANKY_ENGINE", "pi")
    assert result == "pi"


def test_mask_value_empty_secret_not_masked() -> None:
    # An empty string should not become REDACT_TOKEN (nothing to hide).
    result = mask_value("GH_TOKEN", "")
    assert result == ""


def test_mask_value_all_pi_provider_vars_masked() -> None:
    for var in PI_PROVIDER_VARS:
        if var == "OLLAMA_HOST":
            # A host URL, not a credential - deliberately shown unmasked.
            assert mask_value(var, "http://box:11434") == "http://box:11434"
            continue
        assert mask_value(var, "sk-real") == REDACT_TOKEN


def test_mask_value_jira_email_not_masked() -> None:
    result = mask_value("JIRA_EMAIL", "user@example.com")
    assert result == "user@example.com"


# ---------------------------------------------------------------------------
# SETTABLE_KEYS - sanity
# ---------------------------------------------------------------------------


def test_settable_keys_includes_secret_keys() -> None:
    assert SECRET_KEYS <= SETTABLE_KEYS


def test_settable_keys_includes_non_secret_keys() -> None:
    for key in ("FRANKY_ENGINE", "FRANKY_ALLOWED_REPOS", "JIRA_EMAIL"):
        assert key in SETTABLE_KEYS
