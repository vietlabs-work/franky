"""Tests for franky/profile.py.

All tests are hermetic: no real files outside of tmp_path, no network, no Docker.
"""

import base64
import io
import json
import tarfile
from pathlib import Path

import pytest

from franky.profile import (
    PROFILE_BUNDLE_VAR,
    PROFILE_PATH_VAR,
    ProfileSpec,
    build_bundle,
    codex_mcp_overrides,
    load_profile,
    profile_file_path,
    profile_path,
    read_profile_raw,
    resolve_mcp_credentials,
    scan_for_secrets,
    scan_profile_files,
    validate_mcp_engine,
    write_profile,
)


# ---------------------------------------------------------------------------
# scan_for_secrets
# ---------------------------------------------------------------------------


def test_scan_no_secrets_returns_empty():
    text = "# My Skill\n\nDo the thing with care.\n\n## When to use\n\nAlways.\n"
    assert scan_for_secrets(text) == []


def test_scan_detects_pem_private_key():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_openssh_private_key():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blb...\n-----END OPENSSH PRIVATE KEY-----\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_github_token():
    text = "Use this token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n"
    assert scan_for_secrets(text) != []


@pytest.mark.parametrize(
    "prefix",
    ["gho", "ghu", "ghs", "ghr"],
)
def test_scan_detects_github_token_variants(prefix):
    text = f"token: {prefix}_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_anthropic_api_key():
    text = "key: sk-ant-api03-verylongkeyvaluethatisatleast40charslong1234567890\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_openrouter_key():
    text = "OPENROUTER_API_KEY=sk-or-v1-thisisafakeopenrouterkeyfortest12345\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_openai_style_key():
    text = "key = sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    assert scan_for_secrets(text) != []


def test_scan_detects_secret_env_var_assignment():
    text = "# config\nGH_TOKEN=ghp_myfaketoken\n"
    assert scan_for_secrets(text) != []


@pytest.mark.parametrize(
    "varname",
    [
        "ANTHROPIC_API_KEY",
        "MOONSHOT_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "JIRA_API_TOKEN",
    ],
)
def test_scan_detects_all_known_secret_var_assignments(varname):
    text = f"{varname}=somevalue\n"
    assert scan_for_secrets(text) != []


def test_scan_does_not_flag_commented_secret_var():
    # A commented-out line should not trigger: the pattern requires a non-comment value.
    text = "# GH_TOKEN=example_not_real\n"
    assert scan_for_secrets(text) == []


def test_scan_does_not_flag_empty_assignment():
    text = "GH_TOKEN=\n"
    # The pattern requires at least one non-whitespace, non-# char after the =.
    assert scan_for_secrets(text) == []


def test_scan_does_not_flag_prose_mention_of_var_name():
    text = "Set GH_TOKEN in your shell before running.\n"
    assert scan_for_secrets(text) == []


def test_scan_returns_description_not_value():
    token = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab"
    text = f"token: {token}\n"
    findings = scan_for_secrets(text)
    assert findings
    # The secret VALUE must not appear in the finding description.
    for f in findings:
        assert token not in f


# ---------------------------------------------------------------------------
# profile_path
# ---------------------------------------------------------------------------


def test_profile_path_env_override(tmp_path):
    p = tmp_path / "custom.toml"
    p.touch()
    assert profile_path({PROFILE_PATH_VAR: str(p)}) == p


def test_profile_path_env_override_returned_even_if_absent(tmp_path):
    p = tmp_path / "nonexistent.toml"
    assert profile_path({PROFILE_PATH_VAR: str(p)}) == p


def test_profile_path_default_absent_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert profile_path({}) is None


def test_profile_path_default_exists_returns_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    p = tmp_path / ".franky" / "profile.toml"
    p.parent.mkdir()
    p.touch()
    assert profile_path({}) == p


# ---------------------------------------------------------------------------
# load_profile
# ---------------------------------------------------------------------------


def test_load_profile_empty_profile_section(tmp_path):
    cfg = tmp_path / "profile.toml"
    cfg.write_text("[profile]\n")
    spec = load_profile(cfg)
    assert spec.all_files() == []


def test_load_profile_literal_paths(tmp_path):
    skill = tmp_path / "skill.md"
    skill.write_text("# Skill\n")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{skill}"]\n')
    spec = load_profile(cfg)
    assert spec.skills == [skill]
    assert spec.instructions == []
    assert spec.knowledge == []


def test_load_profile_all_categories(tmp_path):
    skill = tmp_path / "skill.md"
    instr = tmp_path / "CLAUDE.md"
    know = tmp_path / "arch.md"
    for f in (skill, instr, know):
        f.write_text("content\n")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nskills = ["{skill}"]\ninstructions = ["{instr}"]\nknowledge = ["{know}"]\n'
    )
    spec = load_profile(cfg)
    assert spec.skills == [skill]
    assert spec.instructions == [instr]
    assert spec.knowledge == [know]


def test_load_profile_glob_expands(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    (d / "a.md").write_text("A\n")
    (d / "b.md").write_text("B\n")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{d}/*.md"]\n')
    spec = load_profile(cfg)
    assert sorted(spec.skills) == sorted([d / "a.md", d / "b.md"])


def test_load_profile_glob_no_matches_is_ok(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{empty}/*.md"]\n')
    spec = load_profile(cfg)
    assert spec.skills == []


def test_load_profile_literal_missing_file_raises(tmp_path):
    cfg = tmp_path / "profile.toml"
    absent = tmp_path / "no-such-file.md"
    cfg.write_text(f'[profile]\nskills = ["{absent}"]\n')
    with pytest.raises(ValueError, match="not found"):
        load_profile(cfg)


def test_load_profile_malformed_toml_raises(tmp_path):
    cfg = tmp_path / "profile.toml"
    cfg.write_text("[profile\nskills = broken\n")
    with pytest.raises(ValueError, match="malformed TOML"):
        load_profile(cfg)


def test_load_profile_missing_file_raises(tmp_path):
    absent = tmp_path / "no-profile.toml"
    with pytest.raises(ValueError, match="could not read"):
        load_profile(absent)


def test_load_profile_non_list_category_raises(tmp_path):
    cfg = tmp_path / "profile.toml"
    cfg.write_text('[profile]\nskills = "not-a-list"\n')
    with pytest.raises(ValueError, match="list"):
        load_profile(cfg)


def test_load_profile_non_string_entry_raises(tmp_path):
    cfg = tmp_path / "profile.toml"
    cfg.write_text("[profile]\nskills = [42]\n")
    with pytest.raises(ValueError, match="strings"):
        load_profile(cfg)


def test_load_profile_mcp_fields_and_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".codex" / "franky-mcp.config.toml"
    mcp.parent.mkdir()
    mcp.write_text(
        '[mcp_servers.linear]\nurl = "https://mcp.linear.app/mcp"\n'
        'bearer_token_env_var = "LINEAR_API_KEY"\n',
        encoding="utf-8",
    )
    cfg = tmp_path / "profile.toml"
    table = {
        "mcp_configs": [str(mcp)],
        "mcp_credentials": ["LINEAR_API_KEY"],
        "mcp_domains": ["mcp.linear.app"],
    }
    write_profile(cfg, table)

    spec = load_profile(cfg)

    assert spec.mcp_configs == [mcp]
    assert spec.mcp_credentials == ["LINEAR_API_KEY"]
    assert spec.mcp_domains == ["mcp.linear.app"]
    assert read_profile_raw(cfg) == table


def test_load_profile_accepts_nested_json_mcp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".claude" / "mcp.json"
    mcp.parent.mkdir()
    mcp.write_text(
        '{"mcpServers":{"linear":{"url":"https://mcp.linear.app/mcp",'
        '"env":{"LINEAR_API_KEY":"${LINEAR_API_KEY}"}}}}\n',
        encoding="utf-8",
    )
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\n'
        'mcp_credentials = ["LINEAR_API_KEY"]\n'
        'mcp_domains = ["mcp.linear.app"]\n',
        encoding="utf-8",
    )

    assert load_profile(cfg).mcp_configs == [mcp]


def test_load_profile_requires_every_declared_credential_to_be_referenced(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"command":"npx"}\n', encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\nmcp_credentials = ["LINEAR_API_KEY"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not referenced"):
        load_profile(cfg)


def test_codex_mcp_overrides_require_isolated_top_level_and_serialize_values(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".codex" / "franky-mcp.config.toml"
    mcp.parent.mkdir()
    mcp.write_text(
        '[mcp_servers."linear.remote"]\nurl = "https://mcp.linear.app/mcp"\n'
        'args = ["serve", "--stdio"]\nenabled = true\ntimeout_sec = 10\n'
        "startup_timeout_sec = 1.5\n"
        'bearer_token_env_var = "LINEAR_API_KEY"\n',
        encoding="utf-8",
    )
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\n'
        'mcp_credentials = ["LINEAR_API_KEY"]\n'
        'mcp_domains = ["mcp.linear.app"]\n',
        encoding="utf-8",
    )

    overrides = codex_mcp_overrides(load_profile(cfg))

    assert len(overrides) == 1
    assert overrides[0].startswith('mcp_servers."linear.remote"=')
    assert 'args = ["serve", "--stdio"]' in overrides[0]
    assert "startup_timeout_sec = 1.5" in overrides[0]
    assert 'bearer_token_env_var = "LINEAR_API_KEY"' in overrides[0]

    mcp.write_text('model = "forbidden"\n[mcp_servers.ok]\ncommand = "npx"\n')
    with pytest.raises(ValueError, match="only the top-level mcp_servers"):
        load_profile(cfg)


def test_claude_mcp_config_requires_isolated_top_level(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / ".claude" / "franky-mcp.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers":{},"permissions":{}}\n', encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="only the top-level mcpServers"):
        load_profile(cfg)

    mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    assert load_profile(cfg).mcp_configs == [mcp]


@pytest.mark.parametrize("name", ["lowercase", "1TOKEN", "TOKEN-NAME", "TOKEN NAME"])
def test_load_profile_rejects_invalid_mcp_credential_name(tmp_path, name):
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_credentials = ["{name}"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="credential name"):
        load_profile(cfg)


@pytest.mark.parametrize(
    "name",
    ["FRANKY_PROFILE_BUNDLE", "HOME", "HTTP_PROXY", "NO_PROXY", "DOCKER_HOST"],
)
def test_load_profile_rejects_reserved_runtime_credential_name(tmp_path, name):
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_credentials = ["{name}"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="reserved runtime variable"):
        load_profile(cfg)


@pytest.mark.parametrize(
    "domain", ["https://mcp.example.com", "mcp.example.com:443", "*.example.com"]
)
def test_load_profile_rejects_invalid_mcp_domain(tmp_path, domain):
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_domains = ["{domain}"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="hostname"):
        load_profile(cfg)


def test_load_profile_rejects_mcp_config_outside_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}\n", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="under HOME"):
        load_profile(cfg)


def test_load_profile_rejects_unsupported_mcp_config_format(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.yaml"
    mcp.write_text("servers: {}\n", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON or TOML"):
        load_profile(cfg)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"token":"${UNDECLARED}"}\n', "undeclared credential"),
        ('{"env":{"LINEAR_API_KEY":"literal-value"}}\n', "literal value"),
        ('{"url":"https://evil.example/mcp"}\n', "not declared"),
    ],
)
def test_load_profile_rejects_unsafe_mcp_content(tmp_path, monkeypatch, content, message):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.json"
    mcp.write_text(content, encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\n'
        'mcp_credentials = ["LINEAR_API_KEY"]\n'
        'mcp_domains = ["mcp.linear.app"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        load_profile(cfg)


@pytest.mark.parametrize("key", ["MCP_TOKEN", "bearer_token_env_var"])
def test_load_profile_rejects_undeclared_credential_like_literal(tmp_path, monkeypatch, key):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({key: "short-secret"}) + "\n", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match=f"credential-like field {key}"):
        load_profile(cfg)


def test_validate_mcp_engine_rejects_config_that_selected_engine_cannot_load(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    generic = tmp_path / "mcp.json"
    generic.write_text("{}\n", encoding="utf-8")
    spec = ProfileSpec(mcp_configs=[generic])

    with pytest.raises(ValueError, match="Codex MCP config must use"):
        validate_mcp_engine(spec, "codex")
    with pytest.raises(ValueError, match="Claude MCP config must use"):
        validate_mcp_engine(spec, "claude")
    validate_mcp_engine(spec, "pi")


def test_resolve_mcp_credentials_requires_nonempty_process_env():
    spec = ProfileSpec(mcp_credentials=["LINEAR_API_KEY"])
    with pytest.raises(ValueError, match="LINEAR_API_KEY"):
        resolve_mcp_credentials(spec, {})
    assert resolve_mcp_credentials(spec, {"LINEAR_API_KEY": "secret"}) == {
        "LINEAR_API_KEY": "secret"
    }


def test_resolve_mcp_credentials_rejects_actual_value_anywhere_in_config(tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        '{"env_vars":["LINEAR_API_KEY"],"other":"short-secret"}\n',
        encoding="utf-8",
    )
    spec = ProfileSpec(mcp_configs=[mcp], mcp_credentials=["LINEAR_API_KEY"])
    with pytest.raises(ValueError, match="literal value of credential LINEAR_API_KEY"):
        resolve_mcp_credentials(spec, {"LINEAR_API_KEY": "short-secret"})


def test_mcp_secret_scan_allows_declared_placeholder_but_not_other_literal_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mcp = tmp_path / "mcp.toml"
    mcp.write_text(
        '[env]\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\n',
        encoding="utf-8",
    )
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nmcp_configs = ["{mcp}"]\nmcp_credentials = ["OPENAI_API_KEY"]\n',
        encoding="utf-8",
    )
    spec = load_profile(cfg)
    build_bundle(spec)

    mcp.write_text(
        '[env]\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\n'
        'other = "ghp_aaaaaaaaaaOPENAIAPIKEYaaaaaaaaaaaaaaaaaaaa"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="credential"):
        build_bundle(load_profile(cfg))


# ---------------------------------------------------------------------------
# build_bundle
# ---------------------------------------------------------------------------


def _decode_bundle(bundle: str) -> tarfile.TarFile:
    raw = base64.b64decode(bundle)
    return tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")


def test_build_bundle_produces_valid_base64_gzip_tar(tmp_path):
    f = tmp_path / "skill.md"
    f.write_text("# Skill\n\nDo the thing.\n")
    spec = ProfileSpec(skills=[f])
    bundle = build_bundle(spec)
    tf = _decode_bundle(bundle)
    assert len(tf.getmembers()) == 1


def test_build_bundle_file_content_preserved(tmp_path):
    content = "# My Skill\n\nAlways test before push.\n"
    f = tmp_path / "skill.md"
    f.write_text(content)
    spec = ProfileSpec(skills=[f])
    bundle = build_bundle(spec)
    tf = _decode_bundle(bundle)
    member = tf.getmembers()[0]
    extracted = tf.extractfile(member).read().decode("utf-8")
    assert extracted == content


def test_build_bundle_multiple_files(tmp_path):
    files = []
    for i in range(3):
        f = tmp_path / f"file{i}.md"
        f.write_text(f"content {i}\n")
        files.append(f)
    spec = ProfileSpec(skills=files)
    bundle = build_bundle(spec)
    tf = _decode_bundle(bundle)
    assert len(tf.getmembers()) == 3


def test_build_bundle_fail_closed_on_secret(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n")
    spec = ProfileSpec(skills=[f])
    with pytest.raises(ValueError, match="credential"):
        build_bundle(spec)


def test_build_bundle_fail_closed_on_pem_key(tmp_path):
    f = tmp_path / "key.md"
    f.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    spec = ProfileSpec(knowledge=[f])
    with pytest.raises(ValueError, match="credential"):
        build_bundle(spec)


def test_build_bundle_refuses_empty_spec(tmp_path):
    spec = ProfileSpec()
    with pytest.raises(ValueError, match="empty"):
        build_bundle(spec)


def test_build_bundle_arcname_relative_to_home(tmp_path, monkeypatch):
    """Files under HOME get archive names relative to HOME, not absolute paths."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    skill_dir = tmp_path / ".claude" / "skills"
    skill_dir.mkdir(parents=True)
    f = skill_dir / "my-skill.md"
    f.write_text("# Skill\n")
    spec = ProfileSpec(skills=[f])
    bundle = build_bundle(spec)
    tf = _decode_bundle(bundle)
    names = tf.getnames()
    assert len(names) == 1
    assert names[0] == ".claude/skills/my-skill.md"


def test_build_bundle_preserves_reserved_mcp_path_for_home_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    source = tmp_path / "configs" / "claude.json"
    source.parent.mkdir()
    source.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    mcp = tmp_path / ".claude" / "franky-mcp.json"
    mcp.parent.mkdir()
    mcp.symlink_to(source)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nmcp_configs = ["{mcp}"]\n', encoding="utf-8")

    bundle = build_bundle(load_profile(cfg))

    assert _decode_bundle(bundle).getnames() == [".claude/franky-mcp.json"]


def test_build_bundle_arcname_fallback_for_outside_home(tmp_path, monkeypatch):
    """Files outside HOME get a flat fallback name under profile/."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    f = tmp_path / "external.md"
    f.write_text("# External\n")
    spec = ProfileSpec(knowledge=[f])
    bundle = build_bundle(spec)
    tf = _decode_bundle(bundle)
    names = tf.getnames()
    assert names[0] == "profile/external.md"


@pytest.mark.skipif(
    __import__("os").getuid() == 0,
    reason="root can read any file regardless of permissions",
)
def test_build_bundle_unreadable_file_raises(tmp_path):
    f = tmp_path / "skill.md"
    f.write_text("content\n")
    f.chmod(0o000)
    spec = ProfileSpec(skills=[f])
    try:
        with pytest.raises(ValueError, match="could not read"):
            build_bundle(spec)
    finally:
        f.chmod(0o644)  # restore so tmp_path cleanup works


# ---------------------------------------------------------------------------
# PROFILE_BUNDLE_VAR constant
# ---------------------------------------------------------------------------


def test_profile_bundle_var_constant():
    assert PROFILE_BUNDLE_VAR == "FRANKY_PROFILE_BUNDLE"


# ---------------------------------------------------------------------------
# profile_file_path (always returns a path, unlike profile_path)
# ---------------------------------------------------------------------------


def test_profile_file_path_default_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv(PROFILE_PATH_VAR, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # The default path is returned even though it does not exist (unlike profile_path()).
    assert profile_file_path({}) == tmp_path / ".franky" / "profile.toml"


def test_profile_file_path_honors_override():
    p = profile_file_path({PROFILE_PATH_VAR: "/somewhere/custom.toml"})
    assert p == Path("/somewhere/custom.toml")


# ---------------------------------------------------------------------------
# read_profile_raw (raw declared lists, no glob expansion / existence check)
# ---------------------------------------------------------------------------


def test_read_profile_raw_absent_file_returns_empty(tmp_path):
    assert read_profile_raw(tmp_path / "nope.toml") == {}


def test_read_profile_raw_returns_unexpanded_strings(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text(
        '[profile]\nskills = ["~/.claude/skills/*.md"]\ninstructions = ["~/.claude/CLAUDE.md"]\n',
        encoding="utf-8",
    )
    raw = read_profile_raw(p)
    # The glob is returned verbatim - not expanded and not existence-checked.
    assert raw == {
        "skills": ["~/.claude/skills/*.md"],
        "instructions": ["~/.claude/CLAUDE.md"],
    }


def test_read_profile_raw_omits_empty_categories(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text("[profile]\nskills = []\n", encoding="utf-8")
    assert read_profile_raw(p) == {}


def test_read_profile_raw_malformed_toml_raises(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text("[profile\nskills = oops", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed TOML"):
        read_profile_raw(p)


def test_read_profile_raw_non_table_raises(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text('profile = "not a table"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must be a TOML table"):
        read_profile_raw(p)


def test_read_profile_raw_non_string_entry_raises(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text("[profile]\nskills = [1, 2]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be strings"):
        read_profile_raw(p)


# ---------------------------------------------------------------------------
# write_profile (deterministic [profile] array writer, round-trips with read_profile_raw)
# ---------------------------------------------------------------------------


def test_write_profile_round_trips(tmp_path):
    p = tmp_path / "profile.toml"
    table = {
        "skills": ["~/.claude/skills/*.md", "~/extra.md"],
        "instructions": ["~/.claude/CLAUDE.md"],
    }
    write_profile(p, table)
    assert read_profile_raw(p) == table


def test_write_profile_canonical_order_and_omits_empty(tmp_path):
    p = tmp_path / "profile.toml"
    # Insertion order deliberately reversed; output must be canonical (skills first).
    write_profile(p, {"knowledge": ["~/k.md"], "skills": ["~/s.md"], "instructions": []})
    text = p.read_text(encoding="utf-8")
    assert text.index("skills") < text.index("knowledge")
    assert "instructions" not in text  # empty category omitted


def test_write_profile_mode_0644(tmp_path):
    import stat

    p = tmp_path / "profile.toml"
    write_profile(p, {"skills": ["~/s.md"]})
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_write_profile_rejects_control_char(tmp_path):
    p = tmp_path / "profile.toml"
    with pytest.raises(ValueError, match="control character"):
        write_profile(p, {"skills": ["bad\nentry"]})


def test_write_profile_escapes_quotes_and_backslashes(tmp_path):
    p = tmp_path / "profile.toml"
    write_profile(p, {"skills": ['~/a"b\\c.md']})
    assert read_profile_raw(p) == {"skills": ['~/a"b\\c.md']}


# ---------------------------------------------------------------------------
# scan_profile_files (dry-run twin of build_bundle's per-file loop)
# ---------------------------------------------------------------------------


def test_scan_profile_files_clean(tmp_path):
    a = tmp_path / "a.md"
    a.write_text("# clean skill\n", encoding="utf-8")
    spec = ProfileSpec(skills=[a])
    results = scan_profile_files(spec)
    assert len(results) == 1
    assert results[0].path == a
    assert results[0].findings == []
    assert results[0].error is None
    assert results[0].size == len("# clean skill\n".encode("utf-8"))


def test_scan_profile_files_flags_secret_and_names_file(tmp_path):
    bad = tmp_path / "leak.md"
    bad.write_text("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n", encoding="utf-8")
    spec = ProfileSpec(instructions=[bad])
    results = scan_profile_files(spec)
    assert results[0].path == bad
    assert results[0].findings != []


def test_scan_profile_files_captures_read_error(tmp_path):
    bad = tmp_path / "noperm.md"
    bad.write_text("x\n", encoding="utf-8")
    bad.chmod(0o000)
    spec = ProfileSpec(skills=[bad])
    try:
        results = scan_profile_files(spec)
        assert results[0].error is not None
        assert results[0].findings == []
    finally:
        bad.chmod(0o644)


def test_scan_profile_files_and_build_bundle_agree(tmp_path):
    """No-drift guard: a file scan_profile_files flags also makes build_bundle raise,
    and a clean set passes both."""
    bad = tmp_path / "leak.md"
    bad.write_text("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n", encoding="utf-8")
    bad_spec = ProfileSpec(skills=[bad])
    assert any(r.findings for r in scan_profile_files(bad_spec))
    with pytest.raises(ValueError):
        build_bundle(bad_spec)

    good = tmp_path / "ok.md"
    good.write_text("# fine\n", encoding="utf-8")
    good_spec = ProfileSpec(skills=[good])
    assert not any(r.findings for r in scan_profile_files(good_spec))
    build_bundle(good_spec)  # must not raise
