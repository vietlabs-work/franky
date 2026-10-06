"""Tests for franky/setups.py - the agentic-setup sweep policy.

Hermetic: every setup tree is built under tmp_path. No network, no Docker, no real ~/.claude.
"""

import tracemalloc
from pathlib import Path

import pytest

from franky import setups


def _claude_setup(root: Path) -> Path:
    """A minimal but realistic ~/.claude: instructions + a skill + a command + junk to exclude."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text("# global instructions\n", encoding="utf-8")
    (root / "skills" / "cook").mkdir(parents=True)
    (root / "skills" / "cook" / "SKILL.md").write_text("# cook\n", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "pr.md").write_text("# PR spec\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# expand_setup: the allowlist
# ---------------------------------------------------------------------------


def test_expand_setup_collects_the_capability_surface(tmp_path):
    scan = setups.expand_setup("claude", _claude_setup(tmp_path / ".claude"))
    names = sorted(p.name for p in scan.files)
    assert names == ["CLAUDE.md", "SKILL.md", "pr.md"]
    assert scan.kind == "claude"
    assert scan.total_bytes > 0


def test_expand_setup_ignores_dirs_outside_the_manifest(tmp_path):
    root = _claude_setup(tmp_path / ".claude")
    (root / "mockups").mkdir()
    (root / "mockups" / "notes.md").write_text("# not a capability\n", encoding="utf-8")

    scan = setups.expand_setup("claude", root)

    assert "notes.md" not in {p.name for p in scan.files}


def test_expand_setup_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown setup kind"):
        setups.expand_setup("cluade", tmp_path)


def test_expand_setup_non_directory_is_refused(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        setups.expand_setup("claude", f)


# ---------------------------------------------------------------------------
# The deny gate - the security-load-bearing half
# ---------------------------------------------------------------------------


def test_expand_setup_prunes_transcript_and_state_dirs(tmp_path):
    """`projects/` and `sessions/` hold conversation transcripts: never injected, never walked."""
    root = _claude_setup(tmp_path / ".claude")
    (root / "skills" / "sessions").mkdir()
    (root / "skills" / "sessions" / "transcript.md").write_text("private\n", encoding="utf-8")
    (root / "skills" / "cache").mkdir()
    (root / "skills" / "cache" / "blob.md").write_text("junk\n", encoding="utf-8")

    scan = setups.expand_setup("claude", root)

    assert not [p for p in scan.files if "sessions" in p.parts or "cache" in p.parts]


def test_expand_setup_denies_credential_basenames_inside_an_allowed_tree(tmp_path):
    root = _claude_setup(tmp_path / ".claude")
    (root / "skills" / "cook" / "auth.json").write_text('{"token": "x"}\n', encoding="utf-8")
    (root / "skills" / "cook" / ".credentials.json").write_text("{}\n", encoding="utf-8")
    (root / "skills" / "cook" / "history.jsonl").write_text("{}\n", encoding="utf-8")

    scan = setups.expand_setup("claude", root)

    assert {p.name for p in scan.files} == {"CLAUDE.md", "SKILL.md", "pr.md"}


def test_expand_setup_denies_a_symlink_pointing_at_a_credential_file(tmp_path):
    """The load-bearing case: deny is checked on the RESOLVED path, not just the basename.

    A symlink named `notes.md` whose target is `auth.json` passes any name-only check, so the
    resolved-name check is the only thing standing between it and the container.
    """
    root = _claude_setup(tmp_path / ".claude")
    secret = tmp_path / "auth.json"
    secret.write_text('{"tokens": {"access": "x"}}\n', encoding="utf-8")
    (root / "skills" / "cook" / "notes.md").symlink_to(secret)

    scan = setups.expand_setup("claude", root)

    assert "notes.md" not in {p.name for p in scan.files}


def test_expand_setup_denies_a_symlink_into_a_denied_directory(tmp_path):
    """A `.md` transcript is not caught by the name deny, so containment has to catch it.

    The target sits under the setup root (`~/.claude/sessions/`), which is where these
    directories actually live - and where the containment check is measured.
    """
    root = _claude_setup(tmp_path / ".claude")
    sessions = root / "sessions"
    sessions.mkdir()
    transcript = sessions / "chat.md"
    transcript.write_text("private conversation\n", encoding="utf-8")
    (root / "skills" / "cook" / "reference.md").symlink_to(transcript)

    scan = setups.expand_setup("claude", root)

    assert "reference.md" not in {p.name for p in scan.files}


def test_expand_setup_ignores_denied_names_in_path_components_above_the_root(tmp_path):
    """REGRESSION: the denied-DIRECTORY check must not look at components ABOVE the root.

    It used to run over the whole absolute resolved path, so a setup living anywhere under a
    directory named `tmp` / `cache` / `log` (all DENY_DIRS entries) swept ZERO files and said
    nothing. Linux CI hit it immediately - pytest's tmp_path is `/tmp/...` - while macOS
    (`/private/var/folders/...`) passed, which is exactly how it reached CI.
    """
    root = _claude_setup(tmp_path / "cache" / "tmp" / "logs" / ".claude")

    scan = setups.expand_setup("claude", root)

    assert {p.name for p in scan.files} == {"CLAUDE.md", "SKILL.md", "pr.md"}


def test_expand_setup_follows_symlinked_skill_dirs(tmp_path):
    """Operators symlink skills in from plugin checkouts; those must still be swept."""
    root = _claude_setup(tmp_path / ".claude")
    external = tmp_path / "plugin-checkout" / "my-skill"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("# external skill\n", encoding="utf-8")
    (root / "skills" / "linked").symlink_to(external)

    scan = setups.expand_setup("claude", root)

    assert any("linked" in p.parts for p in scan.files)


def test_expand_setup_survives_a_symlink_loop(tmp_path):
    root = _claude_setup(tmp_path / ".claude")
    (root / "skills" / "loop").symlink_to(root / "skills")

    scan = setups.expand_setup("claude", root)  # must terminate

    assert "SKILL.md" in {p.name for p in scan.files}


def test_expand_setup_skips_binaries_instead_of_shipping_them_unscanned(tmp_path):
    root = _claude_setup(tmp_path / ".claude")
    blob = root / "skills" / "cook" / "diagram.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")

    scan = setups.expand_setup("claude", root)

    assert blob not in scan.files
    assert blob in scan.skipped_binary


def test_denied_name_is_case_insensitive():
    assert setups.denied_name("AUTH.JSON")
    assert setups.denied_name("Settings.local.json")
    assert not setups.denied_name("SKILL.md")


# ---------------------------------------------------------------------------
# Size guards
# ---------------------------------------------------------------------------


def test_expand_setup_refuses_a_sweep_over_the_file_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    root = _claude_setup(tmp_path / ".claude")
    for i in range(5):
        (root / "commands" / f"c{i}.md").write_text("x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="limit 2"):
        setups.expand_setup("claude", root)


def test_expand_setup_refuses_a_sweep_over_the_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 10)
    root = _claude_setup(tmp_path / ".claude")
    (root / "commands" / "big.md").write_text("x" * 5000, encoding="utf-8")

    with pytest.raises(ValueError, match="limit"):
        setups.expand_setup("claude", root)


def test_bounded_reader_does_not_allocate_the_remaining_budget_for_a_small_file(tmp_path):
    source = tmp_path / "small.md"
    source.write_bytes(b"x")
    budget = setups.ReadBudget(max_files=5000, max_bytes=20 * 1024 * 1024)

    tracemalloc.start()
    try:
        assert setups.read_bounded(source, budget) == b"x"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024


def test_expand_setup_rejects_oversized_manifest_before_open(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_BYTES", 8)
    root = tmp_path / ".claude"
    root.mkdir()
    manifest = root / "CLAUDE.md"
    manifest.write_text("ninebytes", encoding="utf-8")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == manifest:
            raise AssertionError("oversized manifest was opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(ValueError, match="size limit"):
        setups.expand_setup("claude", root)


def test_expand_setup_stops_scandir_before_a_wide_directory_is_materialized(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    root = tmp_path / ".claude"
    commands = root / "commands"
    commands.mkdir(parents=True)
    calls = 0

    class Entry:
        def __init__(self, name):
            self.name = name
            self.path = str(commands / name)

        def is_dir(self, *, follow_symlinks=True):
            return False

        def is_file(self, *, follow_symlinks=True):
            return True

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
            return Entry(f"candidate-{calls}.md")

    monkeypatch.setattr(setups.os, "scandir", lambda _path: Scan())

    with pytest.raises(ValueError, match="enumeration.*limit 2"):
        setups.expand_setup("claude", root)
    assert calls == 3


def test_expand_setup_charges_unreadable_candidates_to_the_file_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 1)
    root = tmp_path / ".claude"
    commands = root / "commands"
    commands.mkdir(parents=True)
    unreadable = root / "CLAUDE.md"
    unreadable.write_text("x", encoding="utf-8")
    (commands / "b.md").write_text("x", encoding="utf-8")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == unreadable:
            raise OSError("unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(ValueError, match="file limit"):
        setups.expand_setup("claude", root)


def test_expand_setup_bounds_visited_empty_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(setups, "MAX_SWEEP_FILES", 2)
    root = tmp_path / ".claude"
    commands = root / "commands"
    (commands / "one" / "two").mkdir(parents=True)

    with pytest.raises(ValueError, match="traversal.*limit 2"):
        setups.expand_setup("claude", root)


# ---------------------------------------------------------------------------
# MCP hints: reported, never injected
# ---------------------------------------------------------------------------


def test_expand_setup_reports_mcp_declarations_without_injecting_the_config(tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (root / "config.toml").write_text(
        '[mcp_servers.slack]\nbearer_token_env_var = "SLACK_MCP_TOKEN"\n', encoding="utf-8"
    )

    scan = setups.expand_setup("codex", root)

    assert scan.mcp_hints == [root / "config.toml"]
    # config.toml is denied outright: it also carries trust/model state, and Codex runs with
    # --ignore-user-config anyway.
    assert root / "config.toml" not in scan.files


def test_expand_setup_no_mcp_hint_when_the_config_declares_none(tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (root / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    assert setups.expand_setup("codex", root).mcp_hints == []


# ---------------------------------------------------------------------------
# find_pr_spec: the convention that replaces a standalone config key
# ---------------------------------------------------------------------------


def test_find_pr_spec_finds_a_command(tmp_path):
    files = [tmp_path / "commands" / "pr.md", tmp_path / "CLAUDE.md"]
    assert setups.find_pr_spec(files) == tmp_path / "commands" / "pr.md"


def test_find_pr_spec_finds_a_skill_dir(tmp_path):
    files = [tmp_path / "skills" / "pr" / "SKILL.md"]
    assert setups.find_pr_spec(files) == tmp_path / "skills" / "pr" / "SKILL.md"


def test_find_pr_spec_prefers_the_shallowest_match(tmp_path):
    deep = tmp_path / "skills" / "pr" / "references" / "pr.md"
    shallow = tmp_path / "commands" / "pr.md"
    assert setups.find_pr_spec([deep, shallow]) == shallow


def test_find_pr_spec_none_when_absent(tmp_path):
    assert setups.find_pr_spec([tmp_path / "CLAUDE.md"]) is None


def test_find_pr_spec_ignores_a_non_markdown_match(tmp_path):
    assert setups.find_pr_spec([tmp_path / "commands" / "pr.yaml"]) is None


def test_every_manifest_kind_has_a_default_root_and_instruction_file():
    # A kind with no instruction file would inject skills with no context; a kind with no
    # default_root cannot be offered by the `profile init` detection.
    for kind, manifest in setups.SETUP_MANIFESTS.items():
        assert manifest.default_root.startswith("~/"), kind
        assert manifest.files, kind
        assert manifest.dirs, kind


def test_expand_setup_denies_the_atlassian_login_by_name_and_through_a_symlink(tmp_path):
    root = _claude_setup(tmp_path / ".claude")
    cook = root / "skills" / "cook"
    login = tmp_path / "atlassian-jira.json"
    login.write_text('{"refresh_token": "rt"}\n', encoding="utf-8")
    (cook / "atlassian-jira.json").write_text("{}\n", encoding="utf-8")
    (cook / "atlassian-jira.lock").write_text("", encoding="utf-8")
    (cook / "notes.md").symlink_to(login)

    scan = setups.expand_setup("claude", root)

    assert {p.name for p in scan.files} == {"CLAUDE.md", "SKILL.md", "pr.md"}
