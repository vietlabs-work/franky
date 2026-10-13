"""Tests for the team setup: the `team` setup kind, its native mapping and its prompt block.

Hermetic: every tree is built under tmp_path, HOME is monkeypatched, nothing touches a network.
The repository-setup mapper has its own file (test_repo_setup.py).
"""

import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from franky import cli, jobs, profile, setups
from franky.config import RepoSetup
from franky.container import REPO_SETUP_BIN
from franky.engine import PiEngine
from franky.prompt import (
    build_iterate_prompt,
    build_plan_prompt,
    build_prompt,
    build_resume_prompt,
    build_setup_block,
)
from franky.task import TaskSpec

from .test_cli import (
    PR_URL,
    _build_env,
    _fix_plan_nonce,
    _fix_review_nonce,
    _iterate_env,
    _mc_review_setup,
    _plan_block,
    _resume_env,
    _review_block,
    _write_resumable_run,
)

BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40
DEFAULT_SHA = "d" * 40
ROOT = Path(__file__).resolve().parent.parent


def _team(root: Path) -> Path:
    """A minimal but realistic team setup dir."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "AGENTS.md": "# Team agents\nAlways write tests.\n",
        "MANIFEST.json": json.dumps(
            {
                "schema": 1,
                "source": "acme/agent-plugins",
                "commit": "0123456789abcdef0123",
                "plugins": ["flow", "review"],
                "skills": [
                    {"name": "cook", "plugin": "flow", "path": "skills/cook"},
                    {"name": "lint", "plugin": "review", "path": "skills/lint"},
                ],
            }
        ),
        "skills/cook/SKILL.md": "---\nname: cook\ndescription: Cook it\n---\n",
        "skills/cook/references/steps.md": "step list\n",
        "skills/lint/SKILL.md": "lint skill\n",
        "rules/b-style.md": "style rule\n",
        "rules/a-safety.md": "safety rule\n",
        "knowledge/domain/payments.md": "payments notes\n",
        "commands/pr.md": "# Team PR spec\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _profile_toml(tmp_path, **setups_dirs) -> Path:
    lines = ["[setups]"] + [f'{kind} = "{path}"' for kind, path in setups_dirs.items()]
    toml = tmp_path / "profile.toml"
    toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml


def _members(bundle: bytes) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        return {m.name: tf.extractfile(m).read().decode() for m in tf.getmembers()}


# ---------------------------------------------------------------------------
# setups.py: the team sweep
# ---------------------------------------------------------------------------


def test_team_is_a_known_setup_kind():
    assert "team" in setups.SETUP_KINDS
    assert setups.SETUP_MANIFESTS["team"].mcp_hints == ()


def test_team_sweep_collects_the_neutral_layout(tmp_path):
    scan = setups.expand_setup("team", _team(tmp_path / "team"))

    rels = sorted(p.relative_to(scan.root).as_posix() for p in scan.files)
    assert rels == [
        "AGENTS.md",
        "MANIFEST.json",
        "commands/pr.md",
        "knowledge/domain/payments.md",
        "rules/a-safety.md",
        "rules/b-style.md",
        "skills/cook/SKILL.md",
        "skills/cook/references/steps.md",
        "skills/lint/SKILL.md",
    ]


def test_team_sweep_ships_no_hooks_mcp_settings_or_executables(tmp_path):
    root = _team(tmp_path / "team")
    (root / "hooks").mkdir()
    (root / "hooks" / "pre.md").write_text("hook\n")
    (root / ".mcp.json").write_text("{}")
    (root / "settings.json").write_text("{}")
    (root / "skills/cook/settings.json").write_text("{}")
    (root / "skills/cook/.env").write_text("A=1\n")
    (root / "skills/cook/run.sh").write_text("#!/bin/sh\n")
    (root / "skills/cook/run.sh").chmod(0o755)
    (root / "skills/cook/binary.md").write_bytes(b"\xff\xfe\x00")
    (root / "skills/cook/helper.py").write_text("print('no exec bit, still a script')\n")
    (root / "skills/cook/references/.mcp.json").write_text('{"mcpServers": {}}')
    (root / "rules/servers.yaml").write_text("mcp: true\n")
    (root / "rules/exec-doc.md").write_text("a doc with an execute bit\n")
    (root / "rules/exec-doc.md").chmod(0o755)

    scan = setups.expand_setup("team", root)

    names = {p.name for p in scan.files}
    refused = {
        "pre.md",
        ".mcp.json",
        "settings.json",
        ".env",
        "run.sh",
        "binary.md",
        "helper.py",
        "servers.yaml",
        "exec-doc.md",
    }
    assert not names & refused
    assert {p.name for p in scan.skipped_policy} == {
        "run.sh",
        "helper.py",
        ".mcp.json",
        "servers.yaml",
        "exec-doc.md",
    }
    assert [p.name for p in scan.skipped_binary] == ["binary.md"]
    assert "MANIFEST.json" in names and "SKILL.md" in names


def test_team_sweep_resolves_symlinks_before_the_deny_checks(tmp_path):
    root = _team(tmp_path / "team")
    secret = tmp_path / "auth.json"
    secret.write_text('{"k": 1}')
    (root / "rules" / "notes.md").symlink_to(secret)

    scan = setups.expand_setup("team", root)

    assert "notes.md" not in {p.name for p in scan.files}


def test_team_sweep_shares_the_profile_budget(tmp_path):
    root = _team(tmp_path / "team")
    budget = setups.ReadBudget(max_files=3, max_bytes=10_000)

    with pytest.raises(ValueError, match="limit"):
        setups.expand_setup("team", root, budget)


def test_read_team_manifest_validates_the_shape(tmp_path):
    root = _team(tmp_path / "team")
    manifest = setups.read_team_manifest(root)
    assert manifest["source"] == "acme/agent-plugins" and len(manifest["skills"]) == 2

    assert setups.read_team_manifest(tmp_path) is None  # no manifest is valid
    (root / "MANIFEST.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        setups.read_team_manifest(root)
    (root / "MANIFEST.json").write_text('{"schema": 2, "source": "x", "commit": "y"}')
    with pytest.raises(ValueError, match="schema 1"):
        setups.read_team_manifest(root)
    (root / "MANIFEST.json").write_text('{"schema": 1, "source": "x", "commit": "y", "skills": 3}')
    with pytest.raises(ValueError, match="skills must be a list"):
        setups.read_team_manifest(root)


# ---------------------------------------------------------------------------
# profile.py: load, native members, merge order
# ---------------------------------------------------------------------------


def _load(tmp_path, monkeypatch, **dirs):
    _home(tmp_path, monkeypatch)
    return profile.load_profile(_profile_toml(tmp_path, **dirs))


def test_load_profile_declares_a_team_setup(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    spec = _load(tmp_path, monkeypatch, team=team)

    assert spec.team_scan is not None and spec.team_scan.root == team
    assert set(spec.setup_scans) == {"team"}


def test_load_profile_refuses_a_broken_team_manifest(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "MANIFEST.json").write_text("{")

    with pytest.raises(ValueError, match="not valid JSON"):
        _load(tmp_path, monkeypatch, team=team)


def test_a_team_dir_outside_home_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = _team(tmp_path / "elsewhere")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    toml = home / "profile.toml"
    toml.write_text(f'[setups]\nteam = "{outside}"\n')

    with pytest.raises(ValueError, match="must be under HOME"):
        profile.load_profile(toml)


NATIVE = {
    "claude": (".claude/skills", ".claude/CLAUDE.md"),
    "codex": (".codex/skills", ".codex/AGENTS.md"),
    "opencode": (".config/opencode/skills", ".config/opencode/AGENTS.md"),
    "pi": (".pi/skills", ".pi/AGENTS.md"),
}


@pytest.mark.parametrize("engine", sorted(NATIVE))
def test_native_arcnames_per_engine(engine, tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    spec = _load(tmp_path, monkeypatch, team=team)
    skills, instruction = NATIVE[engine]

    members = _members(profile.build_bundle(spec, engine=engine))

    assert members[f"{skills}/cook/SKILL.md"].startswith("---\nname: cook")
    assert members[f"{skills}/cook/references/steps.md"] == "step list\n"
    assert members[f"{skills}/lint/SKILL.md"] == "lint skill\n"
    assert instruction in members
    # The team files stay HOME-relative too: the prompt pointer fallback.
    assert members["team/skills/cook/SKILL.md"].startswith("---\nname: cook")
    assert members["team/knowledge/domain/payments.md"] == "payments notes\n"
    # Nothing lands in another engine's native dirs.
    others = [d for e, (d, _) in NATIVE.items() if e != engine]
    assert not any(name.startswith(tuple(others)) for name in members)


def test_instruction_member_merges_operator_text_then_team_rules_in_order(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("# operator text\nmine first\n")
    spec = _load(tmp_path, monkeypatch, claude=claude, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    text = members[".claude/CLAUDE.md"]
    order = [
        text.index(marker)
        for marker in (
            "# operator text",
            "## Team rules (always on)",
            "Always write tests.",
            "### rules/a-safety.md",
            "### rules/b-style.md",
        )
    ]
    assert order == sorted(order)
    assert text.count("# operator text") == 1
    assert "payments notes" not in text  # knowledge is read on demand, never merged


def test_codex_merges_into_its_own_agents_md(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("operator codex text\n")
    spec = _load(tmp_path, monkeypatch, codex=codex, team=team)

    members = _members(profile.build_bundle(spec, engine="codex"))

    assert members[".codex/AGENTS.md"].startswith("operator codex text")
    assert "## Team rules (always on)" in members[".codex/AGENTS.md"]
    assert ".claude/CLAUDE.md" not in members


def test_a_team_skill_overrides_the_operator_skill_of_the_same_name(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    claude = tmp_path / ".claude"
    (claude / "skills" / "cook").mkdir(parents=True)
    (claude / "skills" / "cook" / "SKILL.md").write_text("operator cook\n")
    (claude / "skills" / "mine").mkdir()
    (claude / "skills" / "mine" / "SKILL.md").write_text("operator only\n")
    spec = _load(tmp_path, monkeypatch, claude=claude, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    assert members[".claude/skills/cook/SKILL.md"].startswith("---\nname: cook")
    assert members[".claude/skills/mine/SKILL.md"] == "operator only\n"


def test_a_team_skill_replaces_the_operator_skill_directory_as_a_whole(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    claude = tmp_path / ".claude"
    (claude / "skills" / "cook" / "references").mkdir(parents=True)
    (claude / "skills" / "cook" / "SKILL.md").write_text("operator cook\n")
    (claude / "skills" / "cook" / "references" / "old.md").write_text("operator reference\n")
    (claude / "skills" / "cook" / "extra.md").write_text("operator extra\n")
    spec = _load(tmp_path, monkeypatch, claude=claude, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    cook = sorted(n for n in members if n.startswith(".claude/skills/cook/"))
    assert cook == [".claude/skills/cook/SKILL.md", ".claude/skills/cook/references/steps.md"]


HOOKED_SKILL = (
    "---\nname: cook\ndescription: Cook it\nhooks:\n  PreToolUse:\n    - command: evil\n"
    "mcpServers:\n  x: {command: y}\nallowed-tools: Bash\n---\nbody\n"
)


def test_team_skill_headers_are_rewritten_to_the_allow_list(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "skills/cook/SKILL.md").write_text(HOOKED_SKILL)
    spec = _load(tmp_path, monkeypatch, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    cook = members[".claude/skills/cook/SKILL.md"]
    assert cook == "---\nname: cook\ndescription: Cook it\n---\nbody\n"
    for key in ("hooks", "mcpServers", "allowed-tools", "evil"):
        assert key not in cook


def test_a_team_skill_with_an_unreadable_header_is_not_installed_natively(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "skills/cook/SKILL.md").write_text('---\n"hooks": x\nname: cook\n---\n')
    spec = _load(tmp_path, monkeypatch, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    assert not [n for n in members if n.startswith(".claude/skills/cook/")]
    assert ".claude/skills/lint/SKILL.md" in members


def test_a_rejected_team_skill_leaves_no_raw_copy_at_the_native_path(tmp_path, monkeypatch):
    team = _team(tmp_path / ".claude")  # the team root is the engine's own dir
    (team / "skills/cook/SKILL.md").write_text('---\n"hooks": x\nname: cook\n---\n')
    spec = _load(tmp_path, monkeypatch, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    assert not [n for n in members if n.startswith(".claude/skills/cook/")]
    assert ".claude/skills/lint/SKILL.md" in members


@pytest.mark.parametrize(
    "text,expected",
    [
        ("no header\n", "no header\n"),
        ("---\nname: a\n---\nbody", "---\nname: a\n---\nbody"),
        ("---\nname: a\nhooks:\n  - x\n---\nb", "---\nname: a\n---\nb"),
        ("---\nname: a\n# note\nPermissionMode: bypass\n---\nb", "---\nname: a\n---\nb"),
        ("---\nname: a\n", None),
        ("---\n- orphan\n---\n", None),
        ('---\n"hooks": 1\n---\n', None),
        ("---\nname: a\r\nmodel: m\r\n---\r\nb", "---\nname: a\n---\nb"),
        ("\ufeff---\nname: a\nhooks: x\n---\nb", "---\nname: a\n---\nb"),
        ("\ufeffno header\n", "no header\n"),
        ("--- x\nname: a\n---\nb", None),
        ("----\nhooks: x\n---\nb", None),
        ("---  \nname: a\n---\nb", "---\nname: a\n---\nb"),
    ],
)
def test_sanitize_frontmatter_cases_match_the_image_mapper(text, expected):
    import importlib.util

    spec = importlib.util.spec_from_file_location("mapper", ROOT / "franky-repo-setup.py")
    mapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mapper)
    keys = setups.SKILL_FRONTMATTER_KEYS

    assert setups.sanitize_frontmatter(text, keys) == expected
    assert mapper.sanitize_frontmatter(text, keys) == expected
    assert mapper.SKILL_FRONTMATTER_KEYS == keys
    assert mapper.AGENT_FRONTMATTER_KEYS == setups.AGENT_FRONTMATTER_KEYS


def test_a_profile_without_team_packs_the_same_with_or_without_an_engine(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    (claude / "skills" / "cook").mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("operator\n")
    (claude / "skills" / "cook" / "SKILL.md").write_text("cook\n")
    spec = _load(tmp_path, monkeypatch, claude=claude)

    plain = _members(profile.build_bundle(spec))

    for engine in NATIVE:
        assert _members(profile.build_bundle(spec, engine=engine)) == plain
    assert list(plain) == [".claude/CLAUDE.md", ".claude/skills/cook/SKILL.md"]


def test_a_secret_in_a_team_file_refuses_the_bundle(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "rules" / "leak.md").write_text("token ghp_" + "a1B2c3D4e5" * 4 + "\n")
    spec = _load(tmp_path, monkeypatch, team=team)

    with pytest.raises(ValueError, match="credential"):
        profile.build_bundle(spec, engine="claude")


def test_the_team_pr_spec_wins_over_the_operators(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    claude = tmp_path / ".claude"
    (claude / "commands").mkdir(parents=True)
    (claude / "commands" / "pr.md").write_text("operator pr\n")
    spec = _load(tmp_path, monkeypatch, claude=claude, team=team)

    assert spec.pr_spec() == team / "commands" / "pr.md"
    only_operator = profile.load_profile(_profile_toml(tmp_path, claude=claude))
    assert only_operator.pr_spec() == claude / "commands" / "pr.md"


def test_an_empty_team_dir_still_turns_repo_setup_on(tmp_path, monkeypatch):
    prof, calls = _team_stub(monkeypatch, tmp_path)
    cli.load_profile(None).all_files = lambda: []
    cfg = _cfg()

    bundle, block, _ro = cli._load_profile_bundle(prof, {}, [], cfg, lambda: ("me/repo", BASE_SHA))

    assert bundle is None and calls.bundle == []
    assert cfg.repo_setup == RepoSetup("me/repo", BASE_SHA)
    assert profile.REPO_SETUP_MARKER in block


def test_team_scan_files_are_secret_scanned_by_profile_check(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    spec = _load(tmp_path, monkeypatch, team=team)

    results = profile.scan_profile_files(spec)

    assert results and all(r.error is None and not r.findings for r in results)


# ---------------------------------------------------------------------------
# prompt.py: the team block and the repository-setup marker
# ---------------------------------------------------------------------------


def _spec(tmp_path, monkeypatch, **dirs):
    return _load(tmp_path, monkeypatch, **dirs)


def test_the_team_block_names_the_team_dir_knowledge_and_the_precedence(tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))

    block = build_setup_block(spec, engine="claude", repo_setup=True)

    assert "Team setup (injected)" in block
    assert f"{profile.CONTAINER_HOME}/team" in block and str(tmp_path) not in block
    assert 'under "Team rules (always on)"' in block
    assert "/team/knowledge/" in block
    assert (
        "this prompt, then the repository setup, then the team setup, then the operator setup"
        in (block)
    )
    assert profile.REPO_SETUP_MARKER in block
    assert "Team's PR-description spec".lower() in block.lower()
    assert "/team/commands/pr.md" in block


def test_the_marker_appears_only_with_repo_setup(tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))

    assert profile.REPO_SETUP_MARKER not in build_setup_block(spec, engine="claude")
    assert profile.REPO_SETUP_MARKER in build_setup_block(spec, engine="claude", repo_setup=True)


@pytest.mark.parametrize("engine,pointer", [("claude", False), ("codex", False)])
def test_verified_engines_get_no_skill_pointer(engine, pointer, tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))
    block = build_setup_block(spec, engine=engine)
    assert ("NOT auto-loaded" in block) is pointer
    assert "installed where your engine looks for them" in block


@pytest.mark.parametrize("engine", ["opencode", "pi", None])
def test_unverified_engines_keep_the_prompt_pointer(engine, tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))
    block = build_setup_block(spec, engine=engine)
    assert "NOT auto-loaded" in block and "/team/skills/<name>/SKILL.md" in block


def test_read_only_passes_keep_their_read_only_rules_with_a_team(tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))

    block = build_setup_block(spec, read_only=True, engine="claude", repo_setup=True)

    assert "relaxes this pass's read-only rules" in block
    assert "open the PR" not in block
    assert "PR-description spec" not in block  # no PR is opened in a read-only pass
    assert "None of them relaxes this prompt" in block


def test_team_and_operator_setups_share_one_block(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("x\n")
    spec = _spec(tmp_path, monkeypatch, claude=claude, team=_team(tmp_path / "team"))

    block = build_setup_block(spec, engine="claude")

    assert block.index("Operator setup (injected)") < block.index("Team setup (injected)")
    assert block.count("Three rules govern any conflict") == 1
    assert "(team)" not in block  # the team is not listed as an operator setup


def test_the_repo_index_marker_threads_into_every_session_prompt(tmp_path, monkeypatch):
    spec = _spec(tmp_path, monkeypatch, team=_team(tmp_path / "team"))
    block = build_setup_block(spec, engine="claude", repo_setup=True)
    ro_block = build_setup_block(spec, read_only=True, engine="claude", repo_setup=True)
    task = TaskSpec(repo="me/repo", text="add a flag", source="prose")
    pr = TaskSpec(repo="me/repo", text=PR_URL, source="pr")

    prompts = [
        build_prompt(task, operator_setup=block),
        build_iterate_prompt(pr, operator_setup=block),
        build_resume_prompt(task, branch="franky/t", operator_setup=block),
        build_plan_prompt(task, operator_setup=ro_block),
    ]
    from franky.prompt import build_decompose_prompt, build_replay_prompt

    prompts.append(build_decompose_prompt(task, "nonce", operator_setup=ro_block))
    prompts.append(build_replay_prompt(task, branch="franky/t", base_sha="abc1234", open_pr=True))

    for prompt in prompts[:5]:
        assert profile.REPO_SETUP_MARKER in prompt
    assert profile.REPO_SETUP_MARKER not in prompts[5]  # replay stays bundle-less


def test_the_marker_in_the_image_script_matches_the_profile_constant():
    script = (ROOT / "franky-repo-setup.py").read_text()
    assert f'MARKER = "{profile.REPO_SETUP_MARKER}"' in script


# ---------------------------------------------------------------------------
# cli.py: wiring the repository setup per command
# ---------------------------------------------------------------------------


def _team_stub(monkeypatch, tmp_path):
    """A resolved profile with a team setup; build_bundle / build_setup_block are recorded."""
    team = SimpleNamespace(root=tmp_path / "team", files=[], skipped_policy=[], skipped_binary=[])
    spec = SimpleNamespace(
        setup_scans={"team": team},
        team_scan=team,
        mcp_domains=[],
        all_files=lambda: [1, 2, 3],
        setup_roots=lambda: {"team": team.root},
        pr_spec=lambda: None,
    )
    calls = SimpleNamespace(bundle=[], block=[])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "load_profile", lambda path: spec)
    monkeypatch.setattr(cli, "validate_mcp_engine", lambda *a: None)
    monkeypatch.setattr(cli, "resolve_mcp_credentials", lambda *a: {})
    monkeypatch.setattr(cli, "claude_mcp_config_path", lambda spec: None)

    def bundle(spec, engine=None):
        calls.bundle.append(engine)
        return b"bundle"

    def block(spec, **kw):
        calls.block.append(kw)
        return build_setup_block(spec, **kw)

    monkeypatch.setattr(cli, "build_bundle", bundle)
    monkeypatch.setattr(cli, "build_setup_block", block)
    prof = tmp_path / "p.toml"
    prof.write_text("")
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(prof))
    return str(prof), calls


def _cfg(engine=None):
    from franky.config import Config

    return Config(engine=engine or PiEngine(), allowed_repos=["me/repo"])


def test_load_profile_bundle_sets_the_repo_setup_and_names_the_engine(tmp_path, monkeypatch):
    prof, calls = _team_stub(monkeypatch, tmp_path)
    cfg = _cfg()

    bundle, block, ro = cli._load_profile_bundle(prof, {}, [], cfg, lambda: ("me/repo", BASE_SHA))

    assert bundle == b"bundle" and calls.bundle == ["pi"]
    assert cfg.repo_setup == RepoSetup("me/repo", BASE_SHA)
    assert profile.REPO_SETUP_MARKER in block and profile.REPO_SETUP_MARKER in ro
    assert all(kw["repo_setup"] and kw["engine"] == "pi" for kw in calls.block)


def test_load_profile_bundle_without_a_ref_source_loads_no_repo_setup(tmp_path, monkeypatch):
    prof, _calls = _team_stub(monkeypatch, tmp_path)
    cfg = _cfg()

    _bundle, block, _ro = cli._load_profile_bundle(prof, {}, [], cfg)

    assert cfg.repo_setup is None and profile.REPO_SETUP_MARKER not in block


@pytest.mark.parametrize("bad", [None, "", "main", "abc123"])
def test_an_unresolvable_base_commit_fails_closed(bad, tmp_path, monkeypatch):
    prof, _calls = _team_stub(monkeypatch, tmp_path)
    cfg = _cfg()

    with pytest.raises(cli.NetworkError, match="could not resolve the repository base commit"):
        cli._load_profile_bundle(prof, {}, [], cfg, lambda: ("me/repo", bad))
    assert cfg.repo_setup is None


def _capture_runs(monkeypatch, results):
    seen = []

    def fake_run(cfg, argv, *a, **k):
        seen.append(SimpleNamespace(cfg=cfg, argv=list(argv), kwargs=k))
        return results.pop(0) if len(results) > 1 else results[0]

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    return seen


def _assert_wrapped(run, ref):
    marker = run.cfg.repo_setup.marker
    assert run.argv[:5] == [REPO_SETUP_BIN, "exec", "--marker", marker, "--"]
    assert marker.startswith(profile.REPO_SETUP_MARKER) and len(marker) > 40
    assert run.kwargs["repo_setup"] == {"repo": "me/repo", "ref": ref, "engine": "pi"}
    assert run.cfg.repo_setup == RepoSetup("me/repo", ref)
    assert run.kwargs["profile_bundle"] == b"bundle"
    # The prompt carries the per-run token once, and never the bare literal.
    assert " ".join(run.argv[5:]).count(marker) == 1
    assert " ".join(run.argv[5:]).count(profile.REPO_SETUP_MARKER) == 1


def _common(monkeypatch):
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: DEFAULT_SHA)


def test_build_reads_the_repo_setup_at_the_default_branch_head(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    _common(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    seen = _capture_runs(monkeypatch, [(0, f"opened {PR_URL}")])

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--profile", prof])

    assert res.exit_code == 0, res.output
    _assert_wrapped(seen[0], DEFAULT_SHA)


def test_build_plan_first_pins_one_base_for_the_setup_and_the_run_record(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    prof, _ = _team_stub(monkeypatch, tmp_path)
    tips = iter([DEFAULT_SHA, "e" * 40, "f" * 40])
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: next(tips))
    seen = _capture_runs(monkeypatch, [(0, "PLAN: do it"), (0, f"opened {PR_URL}")])

    res = CliRunner().invoke(
        cli.main,
        ["build", "do it", "--repo", "me/repo", "--plan-first", "--profile", prof],
        input="y\n",
    )

    assert res.exit_code == 0, res.output
    # The planning pass and the build read the same commit, and the run record pins it too.
    assert [r.kwargs["repo_setup"]["ref"] for r in seen] == [DEFAULT_SHA, DEFAULT_SHA]
    records = [jobs.read_record(p.stem) for p in jobs.runs_dir().glob("*.json")]
    builds = [r for r in records if r and r.get("command") == "build"]
    assert builds and {r.get("base_sha") for r in builds} == {DEFAULT_SHA}


def test_plan_reads_the_repo_setup_at_the_default_branch_head(tmp_path, monkeypatch):
    _fix_plan_nonce(monkeypatch)
    monkeypatch.setattr(cli.os, "environ", _build_env())
    _common(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    seen = _capture_runs(
        monkeypatch, [(0, _plan_block({"fits_one_pr": True, "subtasks": [], "rationale": "r"}))]
    )

    res = CliRunner().invoke(cli.main, ["plan", "tiny fix", "--repo", "me/repo", "--profile", prof])

    assert res.exit_code == 0, res.output
    _assert_wrapped(seen[0], DEFAULT_SHA)


def test_iterate_reads_the_repo_setup_at_the_default_branch_head_not_the_pr_base_or_head(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    _common(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)

    def fetch(repo, number, env, **kwargs):
        kwargs["meta_sink"].update(base_sha=BASE_SHA)
        return HEAD_SHA

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fetch)
    monkeypatch.setenv("FRANKY_PROFILE_PATH", prof)
    seen = _capture_runs(monkeypatch, [(0, "pushed")])
    runner = CliRunner()

    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])

    assert res.exit_code == 0, res.output
    # The PR base is author-chosen, so it is not trusted either.
    _assert_wrapped(seen[0], DEFAULT_SHA)
    assert not {HEAD_SHA, BASE_SHA} & set(seen[0].kwargs["repo_setup"].values())


def test_iterate_fails_closed_when_the_default_branch_head_cannot_be_resolved(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.os, "environ", _iterate_env())
    _common(monkeypatch)
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: None)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    seen = _capture_runs(monkeypatch, [(0, "pushed")])
    runner = CliRunner()

    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["iterate", PR_URL])

    assert res.exit_code != 0 and not seen


def test_review_pr_reads_the_repo_setup_at_the_default_branch_head_not_the_pr_base_or_head(
    tmp_path, monkeypatch
):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: DEFAULT_SHA)
    live_fetch = cli.fetch_pr_head_sha

    def fetch(repo, number, env, **kwargs):
        sha = live_fetch(repo, number, env, **kwargs)
        if kwargs.get("meta_sink") is not None:
            kwargs["meta_sink"]["base_sha"] = BASE_SHA
        return sha

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fetch)
    seen = _capture_runs(monkeypatch, [(0, _review_block({"summary": "ok", "findings": []}))])

    res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--", PR_URL])

    assert res.exit_code == 0, res.output
    _assert_wrapped(seen[0], DEFAULT_SHA)
    assert profile.REPO_SETUP_MARKER in " ".join(seen[0].argv)


def test_review_pr_fails_closed_when_the_default_branch_head_cannot_be_resolved(
    tmp_path, monkeypatch
):
    _fix_review_nonce(monkeypatch)
    _mc_review_setup(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: None)
    seen = _capture_runs(monkeypatch, [(0, _review_block({"summary": "ok", "findings": []}))])

    res = CliRunner().invoke(cli.main, ["review-pr", "--no-publish", "--", PR_URL])

    assert res.exit_code != 0 and not seen


def test_resume_loads_the_bundle_and_reads_the_repo_setup_at_the_recorded_base(
    tmp_path, monkeypatch
):
    captured = {}
    env = _resume_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"), captured=captured)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    env["FRANKY_PROFILE_PATH"] = prof
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: DEFAULT_SHA)
    job_id = _write_resumable_run(env, tmp_path, job_id="beef0c")
    jobs.update_record(job_id, {"base_sha": BASE_SHA}, env)

    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])

    assert res.exit_code == 0, res.output
    assert captured["profile_bundle"] == b"bundle"
    assert captured["repo_setup"] == {"repo": "me/repo", "ref": BASE_SHA, "engine": "pi"}


def test_resume_without_a_recorded_base_reads_the_default_branch_head(tmp_path, monkeypatch):
    captured = {}
    env = _resume_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"), captured=captured)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    env["FRANKY_PROFILE_PATH"] = prof
    monkeypatch.setattr(cli.baseref, "resolve_base_sha", lambda repo, env: DEFAULT_SHA)
    job_id = _write_resumable_run(env, tmp_path, job_id="beef0d")

    res = CliRunner().invoke(cli.main, ["job", "resume", job_id, "--json"])

    assert res.exit_code == 0, res.output
    assert captured["repo_setup"]["ref"] == DEFAULT_SHA


def test_replay_stays_bundle_less(tmp_path, monkeypatch):
    captured = {}
    env = _resume_env(monkeypatch, tmp_path, (0, f"opened {PR_URL}"), captured=captured)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    env["FRANKY_PROFILE_PATH"] = prof
    job_id = _write_resumable_run(env, tmp_path, job_id="beef0e", with_snapshot=False)
    jobs.update_record(job_id, {"base_sha": BASE_SHA}, env)
    monkeypatch.setattr(cli.baseref, "commit_exists", lambda *a, **k: True)

    res = CliRunner().invoke(cli.main, ["job", "replay", job_id, "--json"])

    assert res.exit_code == 0, res.output
    assert captured.get("profile_bundle") is None and "repo_setup" not in captured


def test_a_profile_without_team_changes_nothing_in_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, "environ", _build_env())
    _common(monkeypatch)
    prof, _ = _team_stub(monkeypatch, tmp_path)
    spec = cli.load_profile(None)
    spec.team_scan = None
    spec.setup_scans = {}
    spec.setup_roots = lambda: {"claude": tmp_path / ".claude"}
    seen = _capture_runs(monkeypatch, [(0, f"opened {PR_URL}")])

    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--profile", prof])

    assert res.exit_code == 0, res.output
    run = seen[0]
    assert run.argv[0] != REPO_SETUP_BIN and "repo_setup" not in run.kwargs
    assert run.cfg.repo_setup is None
    assert profile.REPO_SETUP_MARKER not in " ".join(run.argv)


def test_profile_check_prints_the_team_line(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    _home(tmp_path, monkeypatch)
    toml = _profile_toml(tmp_path, team=team)
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(toml))

    res = CliRunner().invoke(cli.main, ["profile", "check"])

    assert res.exit_code == 0, res.output
    assert "team: acme/agent-plugins@0123456789ab, 2 plugin(s), 2 skill(s)" in res.output
    assert "pr spec:" in res.output and "commands/pr.md" in res.output


def test_profile_check_notes_a_team_without_a_manifest(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "MANIFEST.json").unlink()
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("FRANKY_PROFILE_PATH", str(_profile_toml(tmp_path, team=team)))

    res = CliRunner().invoke(cli.main, ["profile", "check"])

    assert res.exit_code == 0, res.output
    assert "(no MANIFEST.json)" in res.output


# ---------------------------------------------------------------------------
# The per-run Codex home
# ---------------------------------------------------------------------------


def test_two_runs_share_no_codex_home_state():
    from franky.container import CODEX_RUN_HOME, build_docker_argv

    argvs = [
        build_docker_argv("franky", {}, ["codex"], auth_volume="franky-codex-auth")
        for _ in range(2)
    ]
    for argv in argvs:
        mounts = [argv[i + 1] for i, t in enumerate(argv) if t == "--mount"]
        # HOME is an anonymous (per-run) volume, so ~/.codex lives in it ...
        assert "type=volume,dst=/home/franky" in mounts
        # ... and nothing named is mounted at or inside CODEX_HOME.
        named = [m for m in mounts if "src=" in m]
        assert len(named) == 1 and not named[0].endswith(f"dst={CODEX_RUN_HOME}")
        assert f"CODEX_HOME={CODEX_RUN_HOME}" in argv


def test_a_repo_setup_run_extends_the_entrypoint_marker_wait():
    from franky.container import REPO_SETUP_TIMEOUT, build_docker_argv

    plain = build_docker_argv("franky", {}, ["pi"], profile_wait=True)
    longer = build_docker_argv(
        "franky", {}, ["pi"], profile_wait=True, profile_wait_secs=120 + int(REPO_SETUP_TIMEOUT)
    )

    assert not any(a.startswith("FRANKY_PROFILE_WAIT_SECS") for a in plain)
    assert "FRANKY_PROFILE_WAIT_SECS=220" in longer
    script = (ROOT / "franky-dind-entrypoint.sh").read_text()
    assert "${FRANKY_PROFILE_WAIT_SECS:-120}" in script


def _entrypoint_codex_block() -> str:
    text = (ROOT / "franky-dind-entrypoint.sh").read_text()
    match = re.search(
        r'(if \[ -f "\$\{HOME\}/\.codex-auth/auth\.json" \]; then.*?\nfi\n)', text, re.S
    )
    assert match, "the entrypoint must link auth.json into the per-run CODEX_HOME"
    return match.group(1)


def test_the_entrypoint_links_only_auth_json_and_writes_through(tmp_path):
    home = tmp_path / "home"
    (home / ".codex-auth").mkdir(parents=True)
    (home / ".codex-auth" / "auth.json").write_text('{"v": 1}')
    (home / ".codex-auth" / "stale-skill.md").write_text("old")

    subprocess.run(
        ["bash", "-c", _entrypoint_codex_block()], env={**os.environ, "HOME": str(home)}, check=True
    )

    link = home / ".codex" / "auth.json"
    assert link.is_symlink() and link.read_text() == '{"v": 1}'
    assert sorted(p.name for p in (home / ".codex").iterdir()) == ["auth.json"]
    # A token refresh written through the link (truncate + write) lands in the persistent volume.
    with open(link, "w") as handle:
        handle.write('{"v": 2}')
    assert (home / ".codex-auth" / "auth.json").read_text() == '{"v": 2}'


def test_the_entrypoint_leaves_codex_home_alone_without_a_subscription(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    subprocess.run(
        ["bash", "-c", _entrypoint_codex_block()], env={**os.environ, "HOME": str(home)}, check=True
    )

    assert not (home / ".codex").exists()


def test_a_nested_team_skill_md_is_sanitized_and_a_bad_one_drops_the_skill(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    (team / "skills/cook/sub").mkdir()
    (team / "skills/cook/sub/SKILL.md").write_text("---\nname: n\nhooks:\n  - x\n---\nnested\n")
    (team / "skills/lint/deep/er").mkdir(parents=True)
    (team / "skills/lint/deep/er/skill.md").write_text('---\n"hooks": 1\n---\n')
    spec = _load(tmp_path, monkeypatch, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    assert members[".claude/skills/cook/sub/SKILL.md"] == "---\nname: n\n---\nnested\n"
    assert not [n for n in members if n.startswith(".claude/skills/lint/")]


def test_team_skill_dirs_refuse_structured_config_and_an_agents_dir(tmp_path, monkeypatch):
    team = _team(tmp_path / "team")
    for rel in (
        "skills/cook/a.yaml",
        "skills/cook/b.yml",
        "skills/cook/c.toml",
        "skills/cook/d.json",
        "skills/cook/agents/openai.md",
        "skills/cook/x/Agents/y.md",
    ):
        (team / rel).parent.mkdir(parents=True, exist_ok=True)
        (team / rel).write_text("x\n")
    spec = _load(tmp_path, monkeypatch, team=team)

    members = _members(profile.build_bundle(spec, engine="claude"))

    cook = sorted(n for n in members if n.startswith(".claude/skills/cook/"))
    assert cook == [".claude/skills/cook/SKILL.md", ".claude/skills/cook/references/steps.md"]


def test_each_run_gets_a_fresh_marker_that_extends_the_literal():
    a, b = profile.new_repo_marker(), profile.new_repo_marker()

    assert a != b and a.startswith(profile.REPO_SETUP_MARKER) and len(a) >= 48
