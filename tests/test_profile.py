"""Tests for franky/profile.py.

All tests are hermetic: no real files outside of tmp_path, no network, no Docker.
"""

import io
import json
import tarfile
from pathlib import Path

import pytest

from franky import setups
from franky.profile import (
    CLAUDE_MCP_CONTAINER_PATH,
    CONTAINER_HOME,
    PROFILE_WAIT_VAR,
    container_path,
    PROFILE_PATH_VAR,
    ProfileSpec,
    build_bundle,
    codex_mcp_overrides,
    load_profile,
    profile_file_path,
    profile_path,
    read_profile_raw,
    read_setups_raw,
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


def test_load_profile_glob_preserves_segment_and_hidden_file_rules(tmp_path):
    skills = tmp_path / "skills"
    nested = skills / "nested"
    nested.mkdir(parents=True)
    (nested / "visible.md").write_text("visible\n", encoding="utf-8")
    (nested / ".hidden.md").write_text("hidden\n", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nskills = ["{skills}/*/*.md", "{skills}/*/.*.md"]\n',
        encoding="utf-8",
    )

    assert load_profile(cfg).skills == [nested / "visible.md", nested / ".hidden.md"]


def test_load_profile_glob_keeps_double_star_nonrecursive(tmp_path):
    skills = tmp_path / "skills"
    one = skills / "one"
    two = one / "two"
    two.mkdir(parents=True)
    (one / "direct.md").write_text("direct\n", encoding="utf-8")
    (two / "nested.md").write_text("nested\n", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{skills}/**/*.md"]\n', encoding="utf-8")

    assert load_profile(cfg).skills == [one / "direct.md"]


def test_load_profile_glob_follows_symlinked_directories(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    target = external / "linked.md"
    target.write_text("linked\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "linked").symlink_to(external)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{skills}/*/*.md"]\n', encoding="utf-8")

    assert load_profile(cfg).skills == [skills / "linked" / "linked.md"]


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


def test_mcp_secret_scan_accepts_crlf_declared_placeholder(tmp_path):
    mcp = tmp_path / "mcp.toml"
    mcp.write_bytes(b'[env]\r\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\r\n')
    spec = ProfileSpec(mcp_configs=[mcp], mcp_credentials=["OPENAI_API_KEY"])

    bundle = build_bundle(spec)

    packed = _decode_bundle(bundle).extractfile("profile/mcp.toml")
    assert packed is not None
    assert packed.read() == b'[env]\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\n'


# ---------------------------------------------------------------------------
# build_bundle
# ---------------------------------------------------------------------------


def _decode_bundle(bundle: bytes) -> tarfile.TarFile:
    """The bundle is raw gzip-tar bytes now - it is streamed over `docker exec` stdin, so
    there is no base64 layer to strip."""
    return tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")


def test_build_bundle_produces_valid_gzip_tar_bytes(tmp_path):
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


def test_explicit_profile_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    source = tmp_path / "instructions.md"
    source.write_text("ninebytes", encoding="utf-8")

    with pytest.raises(ValueError, match="size limit"):
        build_bundle(ProfileSpec(instructions=[source]))


def test_profile_rejects_oversized_file_before_open(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    source = tmp_path / "instructions.md"
    source.write_text("ninebytes", encoding="utf-8")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == source:
            raise AssertionError("oversized file was opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(ValueError, match="size limit"):
        build_bundle(ProfileSpec(instructions=[source]))


def test_profile_rejects_file_that_grows_past_remaining_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    source = tmp_path / "instructions.md"
    source.write_text("x", encoding="utf-8")
    real_open = Path.open

    def growing_open(path, *args, **kwargs):
        if path == source:
            return io.BytesIO(b"ninebytes")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)

    with pytest.raises(ValueError, match="size limit"):
        build_bundle(ProfileSpec(instructions=[source]))


def test_explicit_profile_rejects_invalid_utf8(tmp_path):
    source = tmp_path / "instructions.md"
    source.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="UTF-8"):
        build_bundle(ProfileSpec(instructions=[source]))


def test_combined_profile_budget_includes_explicit_swept_and_mcp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 10)
    explicit = tmp_path / "instructions.md"
    swept = tmp_path / "CLAUDE.md"
    mcp = tmp_path / "mcp.json"
    explicit.write_text("four", encoding="utf-8")
    swept.write_text("four", encoding="utf-8")
    mcp.write_text("{}\n", encoding="utf-8")
    spec = ProfileSpec(
        instructions=[explicit],
        mcp_configs=[mcp],
        setup_scans={"claude": setups.SetupScan("claude", tmp_path, files=[swept])},
    )

    with pytest.raises(ValueError, match="size limit"):
        build_bundle(spec)
    assert any(result.error and "size limit" in result.error for result in scan_profile_files(spec))


def test_combined_profile_file_limit_includes_explicit_swept_and_mcp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    explicit = tmp_path / "instructions.md"
    swept = tmp_path / "CLAUDE.md"
    mcp = tmp_path / "mcp.json"
    explicit.write_text("x", encoding="utf-8")
    swept.write_text("x", encoding="utf-8")
    mcp.write_text("{}", encoding="utf-8")
    spec = ProfileSpec(
        instructions=[explicit],
        mcp_configs=[mcp],
        setup_scans={"claude": setups.SetupScan("claude", tmp_path, files=[swept])},
    )

    with pytest.raises(ValueError, match="file limit"):
        build_bundle(spec)
    assert any(result.error and "file limit" in result.error for result in scan_profile_files(spec))


def test_build_bundle_fail_closed_on_secret(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n")
    spec = ProfileSpec(skills=[f])
    with pytest.raises(ValueError, match="credential"):
        build_bundle(spec)


def test_build_bundle_rejects_known_assignment_after_cr_only_line(tmp_path):
    source = tmp_path / "bad.md"
    source.write_bytes(b"notes\rGH_TOKEN=value\r")

    with pytest.raises(ValueError, match="credential"):
        build_bundle(ProfileSpec(instructions=[source]))


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
# Injection-mode constants
# ---------------------------------------------------------------------------


def test_profile_wait_var_constant():
    # The entrypoint keys its wait loop on this exact name; content never rides the env.
    assert PROFILE_WAIT_VAR == "FRANKY_PROFILE_WAIT"


def test_container_home_matches_the_reserved_mcp_container_path():
    # container_path() derives every in-container path from CONTAINER_HOME, so it must agree
    # with the one path that was hardcoded before it existed.
    assert CLAUDE_MCP_CONTAINER_PATH.startswith(CONTAINER_HOME + "/")


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


def test_load_profile_rejects_oversized_toml_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    cfg = tmp_path / "profile.toml"
    cfg.write_text("[profile]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="size limit"):
        load_profile(cfg)


def test_load_profile_stops_glob_expansion_at_file_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    for index in range(3):
        (tmp_path / f"skill-{index}.md").write_text("x", encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        f'[profile]\nskills = ["{tmp_path}/skill-*.md"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="limit"):
        load_profile(cfg)


def test_load_profile_stops_scandir_before_a_wide_glob_is_materialized(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[profile]\nskills = ["{tmp_path}/*.md"]\n', encoding="utf-8")
    calls = 0

    class Entry:
        def __init__(self, name):
            self.name = name
            self.path = str(tmp_path / name)

        def is_dir(self, *, follow_symlinks=True):
            return False

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal calls
            calls += 1
            if calls > 3:
                raise AssertionError("scandir continued after the enumeration limit")
            return Entry(f"skill-{calls}.md")

    monkeypatch.setattr(setups.os, "scandir", lambda _path: Scan())

    with pytest.raises(ValueError, match="enumeration.*limit 2"):
        load_profile(cfg)
    assert calls == 3


def test_load_profile_combines_explicit_swept_and_mcp_budgets(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 512)
    explicit = tmp_path / "instructions.md"
    explicit.write_text("x" * 200, encoding="utf-8")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"x":"' + "y" * 190 + '"}', encoding="utf-8")
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("z" * 200, encoding="utf-8")
    cfg = tmp_path / "profile.toml"
    cfg.write_text(
        "[profile]\n"
        f'instructions = ["{explicit}"]\n'
        f'mcp_configs = ["{mcp}"]\n'
        "[setups]\n"
        f'claude = "{claude}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="size limit"):
        load_profile(cfg)


def test_mcp_config_read_is_bounded_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"x":"y"}', encoding="utf-8")

    with pytest.raises(ValueError, match="size limit"):
        build_bundle(ProfileSpec(mcp_configs=[mcp]))


# ---------------------------------------------------------------------------
# [setups]: whole-setup declaration by directory
# ---------------------------------------------------------------------------


def _setup_tree(home: Path) -> None:
    claude = home / ".claude"
    (claude / "skills" / "cook").mkdir(parents=True)
    (claude / "commands").mkdir()
    (claude / "CLAUDE.md").write_text("# instructions\n", encoding="utf-8")
    (claude / "skills" / "cook" / "SKILL.md").write_text("# cook\n", encoding="utf-8")
    (claude / "commands" / "pr.md").write_text("# PR spec\n", encoding="utf-8")


def test_load_profile_expands_a_declared_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{tmp_path / ".claude"}"\n', encoding="utf-8")

    spec = load_profile(cfg)

    assert set(spec.setup_scans) == {"claude"}
    assert {p.name for p in spec.setup_files} == {"CLAUDE.md", "SKILL.md", "pr.md"}
    # A directory declaration is a shorthand for a file list: the swept files must flow into
    # all_files(), which is what the fail-closed secret scan and the packer both consume.
    assert set(spec.setup_files) <= set(spec.all_files())


def test_load_profile_setup_files_are_secret_scanned_like_any_other(tmp_path, monkeypatch):
    """The whole point of folding setups into all_files(): no laxer path for a swept file."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    (tmp_path / ".claude" / "skills" / "cook" / "leak.md").write_text(
        "token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab\n", encoding="utf-8"
    )
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{tmp_path / ".claude"}"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="credential"):
        build_bundle(load_profile(cfg))


def test_load_profile_setup_bundle_keeps_home_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{tmp_path / ".claude"}"\n', encoding="utf-8")

    names = sorted(_decode_bundle(build_bundle(load_profile(cfg))).getnames())

    assert names == [
        ".claude/CLAUDE.md",
        ".claude/commands/pr.md",
        ".claude/skills/cook/SKILL.md",
    ]


def test_load_profile_rejects_unknown_setup_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\ncluade = "{tmp_path / ".claude"}"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown setup kind"):
        load_profile(cfg)


def test_load_profile_rejects_missing_setup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{tmp_path / "nope"}"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="not found"):
        load_profile(cfg)


def test_load_profile_rejects_setup_dir_outside_home(tmp_path, monkeypatch):
    """Bundle members are HOME-relative, so a root elsewhere has no in-container location."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    outside = tmp_path / "elsewhere" / ".claude"
    (outside / "skills").mkdir(parents=True)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{outside}"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="under HOME"):
        load_profile(cfg)


def test_load_profile_rejects_non_string_setup_value(tmp_path):
    cfg = tmp_path / "profile.toml"
    cfg.write_text("[setups]\nclaude = 42\n", encoding="utf-8")

    with pytest.raises(ValueError, match="directory path string"):
        load_profile(cfg)


def test_pr_spec_is_discovered_from_the_swept_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    cfg = tmp_path / "profile.toml"
    cfg.write_text(f'[setups]\nclaude = "{tmp_path / ".claude"}"\n', encoding="utf-8")

    assert load_profile(cfg).pr_spec() == tmp_path / ".claude" / "commands" / "pr.md"


def test_pr_spec_is_none_without_a_setup(tmp_path):
    f = tmp_path / "skill.md"
    f.write_text("# skill\n", encoding="utf-8")
    assert ProfileSpec(skills=[f]).pr_spec() is None


def test_all_files_dedupes_a_file_listed_and_swept(tmp_path, monkeypatch):
    """A tar with two members at one path would extract twice; dedup keeps one."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _setup_tree(tmp_path)
    cfg = tmp_path / "profile.toml"
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    cfg.write_text(
        f'[profile]\ninstructions = ["{claude_md}"]\n\n'
        f'[setups]\nclaude = "{tmp_path / ".claude"}"\n',
        encoding="utf-8",
    )

    files = load_profile(cfg).all_files()

    assert files.count(claude_md) == 1


def test_container_path_maps_home_relative_onto_container_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert container_path(tmp_path / ".claude" / "commands" / "pr.md") == (
        f"{CONTAINER_HOME}/.claude/commands/pr.md"
    )


def test_write_profile_round_trips_the_setups_table(tmp_path):
    path = tmp_path / "profile.toml"
    write_profile(path, {"skills": ["~/x.md"]}, {"codex": "~/.codex", "claude": "~/.claude"})

    assert read_setups_raw(path) == {"claude": "~/.claude", "codex": "~/.codex"}
    assert read_profile_raw(path)["skills"] == ["~/x.md"]


def test_read_setups_raw_absent_file_is_empty(tmp_path):
    assert read_setups_raw(tmp_path / "nope.toml") == {}
