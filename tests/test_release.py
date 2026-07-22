import importlib.util
import subprocess
from pathlib import Path

import pytest


def load_release():
    path = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("release", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release = load_release()

valid_version = release.valid_version
read_pyproject_version = release.read_pyproject_version
read_init_version = release.read_init_version
set_pyproject_version = release.set_pyproject_version
set_init_version = release.set_init_version
set_readme_version = release.set_readme_version
update_changelog = release.update_changelog
extract_notes = release.extract_notes
assert_versions_match = release.assert_versions_match
main = release.main


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "franky").mkdir()
    (tmp_path / "franky" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Some feature\n\n## [0.0.1] - 2026-01-01\n\nOld stuff.\n"
    )
    (tmp_path / "README.md").write_text(
        "# Franky\n\n"
        "```\n"
        "uv tool install franky-agent\n"
        "pipx install franky-agent\n"
        "```\n\n"
        "Egress kills DNS with --dns 127.0.0.1 inside the container.\n\n"
        "## Status\n\nv0.1.0. Under test.\n"
    )
    return tmp_path


def make_fake_run(responses=None):
    """responses: list of (argv_prefix_tuple, returncode, stdout, stderr)
    Returns calls list and runner."""
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if responses:
            for prefix, rc, out, err in responses:
                if list(argv[: len(prefix)]) == list(prefix):
                    return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return runner, calls


# --- valid_version ---


def test_valid_version_accepts_plain():
    assert valid_version("1.2.3") is True


def test_valid_version_rejects_prerelease():
    assert valid_version("1.2.3-rc1") is False


def test_valid_version_rejects_leading_v():
    assert valid_version("v1.2.3") is False


def test_valid_version_rejects_two_part():
    assert valid_version("1.2") is False


def test_valid_version_rejects_four_part():
    assert valid_version("1.2.3.4") is False


# --- version read/write roundtrips ---


def test_set_pyproject_version_roundtrip(repo):
    set_pyproject_version(repo, "2.3.4")
    assert read_pyproject_version(repo) == "2.3.4"


def test_set_init_version_roundtrip(repo):
    set_init_version(repo, "2.3.4")
    assert read_init_version(repo) == "2.3.4"


# --- README version sync ---


def test_set_readme_version_roundtrip(repo):
    set_readme_version(repo, "2.3.4")
    text = (repo / "README.md").read_text()
    assert "## Status\n\nv2.3.4." in text
    assert "v0.1.0" not in text  # the old Status version is gone
    assert "uv tool install franky-agent" in text  # the unversioned install line is untouched
    assert "--dns 127.0.0.1" in text  # the IP is not a version and must be left alone


def test_set_readme_version_no_status_raises(repo):
    # The PyPI install line has no version pin, so the Status line is the only versioned
    # reference. A README without it must fail loudly, not silently ship a stale version.
    (repo / "README.md").write_text("# Franky\n\nNo status line here.\n")
    with pytest.raises(SystemExit):
        set_readme_version(repo, "2.3.4")


# --- changelog ---


def test_update_changelog_transform(repo):
    update_changelog(repo, "1.0.0", "2026-06-22")
    text = (repo / "CHANGELOG.md").read_text()
    assert "## [1.0.0] - 2026-06-22" in text
    unreleased_pos = text.find("## [Unreleased]")
    versioned_pos = text.find("## [1.0.0] - 2026-06-22")
    assert unreleased_pos != -1
    assert versioned_pos != -1
    assert unreleased_pos < versioned_pos


def test_extract_notes_returns_section_body(repo):
    update_changelog(repo, "1.0.0", "2026-06-22")
    notes = extract_notes(repo, "1.0.0")
    assert "Some feature" in notes


def test_extract_notes_missing_section_exits(repo):
    with pytest.raises(SystemExit) as exc_info:
        extract_notes(repo, "9.9.9")
    assert exc_info.value.code != 0


# --- assert_versions_match ---


def test_assert_versions_match_passes_on_match(repo):
    # pyproject and init are both "0.1.0" from the fixture; tag matches
    assert_versions_match(repo, "v0.1.0")  # should not raise


def test_assert_versions_match_pyproject_skew(repo):
    set_pyproject_version(repo, "1.0.0")
    # init is still "0.1.0"
    with pytest.raises(SystemExit):
        assert_versions_match(repo, "v1.0.0")


def test_assert_versions_match_init_skew(repo):
    set_pyproject_version(repo, "1.0.0")
    set_init_version(repo, "2.0.0")
    with pytest.raises(SystemExit):
        assert_versions_match(repo, "v1.0.0")


def test_assert_versions_match_tag_skew(repo):
    set_pyproject_version(repo, "1.0.0")
    set_init_version(repo, "1.0.0")
    with pytest.raises(SystemExit):
        assert_versions_match(repo, "v2.0.0")


def test_assert_versions_match_tag_patch_typo(repo):
    # pyproject == init == 0.1.0 (fixture) but the tag is a common patch typo v0.1.1.
    with pytest.raises(SystemExit):
        assert_versions_match(repo, "v0.1.1")


# --- changelog fail-loud guards ---


def test_update_changelog_missing_unreleased_raises(repo):
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.0.1] - 2026-01-01\n\nOld.\n")
    with pytest.raises(SystemExit):
        update_changelog(repo, "1.0.0", "2026-06-22")


def test_extract_notes_empty_section_raises(repo):
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.0] - 2026-06-22\n\n## [0.0.1]\n\nx\n"
    )
    with pytest.raises(SystemExit):
        extract_notes(repo, "1.0.0")


# --- guard subcommand ---


def test_guard_subcommand_exit_0_on_match(repo):
    set_pyproject_version(repo, "1.0.0")
    set_init_version(repo, "1.0.0")
    fake_run, _ = make_fake_run()
    # Should not raise
    main(["guard", "v1.0.0"], run=fake_run, root=repo)


def test_guard_subcommand_nonzero_on_skew(repo):
    # pyproject/init still "0.1.0", tag is v2.0.0
    fake_run, _ = make_fake_run()
    with pytest.raises(SystemExit) as exc_info:
        main(["guard", "v2.0.0"], run=fake_run, root=repo)
    assert exc_info.value.code != 0


# --- full release happy path ---


def test_release_happy_path_git_commands(repo):
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "HEAD"], 0, "abc123", ""),
        (["git", "rev-parse", "origin/main"], 0, "abc123", ""),
        (["git", "rev-parse", "-q", "--verify"], 1, "", ""),
        (["git", "add"], 0, "", ""),
        (["git", "commit"], 0, "", ""),
        (["git", "tag"], 0, "", ""),
        (["git", "push"], 0, "", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["1.2.3", "--no-watch"], run=fake_run, root=repo)

    git_calls = [c for c in calls if c[0] == "git"]
    subcommands = [c[1] for c in git_calls]
    add_idx = subcommands.index("add")
    commit_idx = subcommands.index("commit")
    tag_idx = subcommands.index("tag")
    push_idx = subcommands.index("push")
    assert add_idx < commit_idx < tag_idx < push_idx

    assert read_pyproject_version(repo) == "1.2.3"
    assert read_init_version(repo) == "1.2.3"
    # README version ref (the Status line) is bumped in lockstep and staged.
    assert "## Status\n\nv1.2.3." in (repo / "README.md").read_text()
    assert "README.md" in git_calls[add_idx]


def test_release_refuses_dirty_tree(repo):
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "M pyproject.toml", ""),
    ]
    fake_run, _ = make_fake_run(responses)
    with pytest.raises(SystemExit):
        main(["1.2.3"], run=fake_run, root=repo)


def test_release_clean_tree_ignores_untracked(repo):
    # Untracked files (agent worktrees, scratch) must NOT block a release - the release commit
    # stages only named files. Flag-aware fake: the --untracked-files=no call (longer prefix,
    # matched first) returns a clean tree, while a bare --porcelain call would surface the
    # untracked `.claude/` worktree. So this only reaches `push` because the code passes the
    # flag; a regression that drops it falls through to the dirty response and aborts.
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "status", "--porcelain"], 0, "?? .claude/", ""),
        (["git", "rev-parse", "HEAD"], 0, "abc123", ""),
        (["git", "rev-parse", "origin/main"], 0, "abc123", ""),
        (["git", "rev-parse", "-q", "--verify"], 1, "", ""),
        (["git", "add"], 0, "", ""),
        (["git", "commit"], 0, "", ""),
        (["git", "tag"], 0, "", ""),
        (["git", "push"], 0, "", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["1.2.3", "--no-watch"], run=fake_run, root=repo)
    subcommands = [c[1] for c in calls if c[0] == "git"]
    assert "push" in subcommands  # reached the end despite untracked .claude/
    status_calls = [c for c in calls if list(c[:2]) == ["git", "status"]]
    assert status_calls and all("--untracked-files=no" in c for c in status_calls)


def _happy_release_responses():
    """Git responses for a clean, synced, tag-absent release that reaches push."""
    return [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "HEAD"], 0, "abc123", ""),
        (["git", "rev-parse", "origin/main"], 0, "abc123", ""),
        (["git", "rev-parse", "-q", "--verify"], 1, "", ""),
        (["git", "add"], 0, "", ""),
        (["git", "commit"], 0, "", ""),
        (["git", "tag"], 0, "", ""),
        (["git", "push"], 0, "", ""),
    ]


def test_release_no_watch_prints_links(repo, capsys):
    responses = _happy_release_responses() + [
        (["git", "remote", "get-url"], 0, "https://github.com/franky-agent/franky.git", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["1.2.3", "--no-watch"], run=fake_run, root=repo, sleep=lambda *_: None)
    # --no-watch never tails the workflow.
    assert not any(c[:3] == ["gh", "run", "watch"] for c in calls)
    out = capsys.readouterr().out
    assert "https://github.com/franky-agent/franky/actions" in out
    assert "releases/tag/v1.2.3" in out


def test_release_watch_happy_path(repo, capsys):
    responses = _happy_release_responses() + [
        (["git", "remote", "get-url"], 0, "git@github.com:franky-agent/franky.git", ""),
        (["gh", "run", "list"], 0, '[{"databaseId": 28002437360}]', ""),
        (["gh", "run", "watch"], 0, "", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["1.2.3"], run=fake_run, root=repo, sleep=lambda *_: None)
    watch_calls = [c for c in calls if c[:3] == ["gh", "run", "watch"]]
    assert watch_calls == [["gh", "run", "watch", "28002437360", "--exit-status"]]
    out = capsys.readouterr().out
    assert "Release published" in out
    assert "releases/tag/v1.2.3" in out


def test_release_watch_reports_workflow_failure(repo, capsys):
    responses = _happy_release_responses() + [
        (["git", "remote", "get-url"], 0, "https://github.com/franky-agent/franky.git", ""),
        (["gh", "run", "list"], 0, '[{"databaseId": 999}]', ""),
        (["gh", "run", "watch"], 1, "", ""),  # --exit-status -> nonzero on a failed run
    ]
    fake_run, _ = make_fake_run(responses)
    main(["1.2.3"], run=fake_run, root=repo, sleep=lambda *_: None)
    out = capsys.readouterr().out
    assert "did NOT succeed" in out


def test_release_watch_degrades_when_gh_missing(repo, capsys):
    base = dict(
        (tuple(prefix), (rc, out, err))
        for prefix, rc, out, err in _happy_release_responses()
        + [(["git", "remote", "get-url"], 0, "https://github.com/franky-agent/franky.git", "")]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv and argv[0] == "gh":
            raise FileNotFoundError("gh")  # gh not installed
        for prefix, (rc, out, err) in base.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    # Must not raise: the release is already pushed, watch is best-effort.
    main(["1.2.3"], run=runner, root=repo, sleep=lambda *_: None)
    out = capsys.readouterr().out
    assert "Couldn't attach" in out
    assert "https://github.com/franky-agent/franky/actions" in out
    assert not any(c[:3] == ["gh", "run", "watch"] for c in calls)


def test_origin_web_url_normalizes_remote_forms(repo):
    for remote in (
        "git@github.com:franky-agent/franky.git",
        "https://github.com/franky-agent/franky.git",
        "ssh://git@github.com/franky-agent/franky",
    ):
        fake_run, _ = make_fake_run([(["git", "remote", "get-url"], 0, remote, "")])
        assert release._origin_web_url(fake_run) == "https://github.com/franky-agent/franky"
    # Non-GitHub remote -> None (best-effort, no crash).
    fake_run, _ = make_fake_run([(["git", "remote", "get-url"], 0, "https://gitlab.com/x/y", "")])
    assert release._origin_web_url(fake_run) is None


def test_release_refuses_wrong_branch(repo):
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "feature-x", ""),
    ]
    fake_run, _ = make_fake_run(responses)
    with pytest.raises(SystemExit):
        main(["1.2.3"], run=fake_run, root=repo)


def test_release_refuses_unsynced_main(repo):
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "HEAD"], 0, "abc123", ""),
        (["git", "rev-parse", "origin/main"], 0, "def456", ""),
    ]
    fake_run, _ = make_fake_run(responses)
    with pytest.raises(SystemExit):
        main(["1.2.3"], run=fake_run, root=repo)


def test_release_refuses_existing_tag(repo):
    responses = [
        (["git", "fetch"], 0, "", ""),
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, "main", ""),
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "HEAD"], 0, "abc123", ""),
        (["git", "rev-parse", "origin/main"], 0, "abc123", ""),
        (["git", "rev-parse", "-q", "--verify"], 0, "abc123", ""),
    ]
    fake_run, _ = make_fake_run(responses)
    with pytest.raises(SystemExit):
        main(["1.2.3"], run=fake_run, root=repo)


def test_release_dry_run_no_file_writes(repo):
    # Dry-run returns before any git call, so the fake run is never consulted. Assert NO file
    # was mutated (all three version sources untouched) and NO git mutation ran.
    fake_run, calls = make_fake_run()
    main(["1.2.3", "--dry-run"], run=fake_run, root=repo)
    assert read_pyproject_version(repo) == "0.1.0"
    assert read_init_version(repo) == "0.1.0"
    assert "## [Unreleased]" in (repo / "CHANGELOG.md").read_text()
    assert "## Status\n\nv0.1.0." in (repo / "README.md").read_text()  # README untouched
    git_calls = [c for c in calls if c[0] == "git"]
    mutating = [c for c in git_calls if c[1] in ("add", "commit", "tag", "push")]
    assert mutating == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_release_refuses_empty_unreleased_before_side_effects(repo, dry_run):
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\nOld stuff.\n"
    )
    before = {path: path.read_text() for path in repo.rglob("*") if path.is_file()}
    fake_run, calls = make_fake_run()
    args = ["1.2.3"] + (["--dry-run"] if dry_run else [])

    with pytest.raises(SystemExit):
        main(args, run=fake_run, root=repo)

    assert calls == []
    assert {path: path.read_text() for path in before} == before


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("version", ["0.0.9", "0.1.0"])
def test_release_refuses_non_increasing_version_before_side_effects(repo, dry_run, version):
    before = {path: path.read_text() for path in repo.rglob("*") if path.is_file()}
    fake_run, calls = make_fake_run()
    args = [version] + (["--dry-run"] if dry_run else [])

    with pytest.raises(SystemExit):
        main(args, run=fake_run, root=repo)

    assert calls == []
    assert {path: path.read_text() for path in before} == before


# --- tag recovery ---


def test_tag_recovery_happy_path(repo):
    set_pyproject_version(repo, "1.2.3")
    set_init_version(repo, "1.2.3")
    responses = [
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "-q", "--verify"], 1, "", ""),
        (["git", "tag"], 0, "", ""),
        (["git", "push"], 0, "", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["tag", "1.2.3"], run=fake_run, root=repo)
    git_calls = [c for c in calls if c[0] == "git"]
    subcommands = [c[1] for c in git_calls]
    assert "tag" in subcommands
    assert "push" in subcommands
    assert "add" not in subcommands
    assert "commit" not in subcommands


def test_tag_recovery_refuses_dirty_tree(repo):
    set_pyproject_version(repo, "1.2.3")
    set_init_version(repo, "1.2.3")
    responses = [
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "M somefile.py", ""),
    ]
    fake_run, _ = make_fake_run(responses)
    with pytest.raises(SystemExit):
        main(["tag", "1.2.3"], run=fake_run, root=repo)


def test_tag_recovery_refuses_version_mismatch(repo):
    # pyproject/init are "0.1.0" from fixture; trying to tag "1.2.3"
    fake_run, _ = make_fake_run()
    with pytest.raises(SystemExit):
        main(["tag", "1.2.3"], run=fake_run, root=repo)


def test_tag_recovery_dry_run_no_mutations(repo):
    set_pyproject_version(repo, "1.2.3")
    set_init_version(repo, "1.2.3")
    responses = [
        (["git", "status", "--porcelain", "--untracked-files=no"], 0, "", ""),
        (["git", "rev-parse", "-q", "--verify"], 1, "", ""),
    ]
    fake_run, calls = make_fake_run(responses)
    main(["tag", "1.2.3", "--dry-run"], run=fake_run, root=repo)
    git_calls = [c for c in calls if c[0] == "git"]
    mutating = [c for c in git_calls if c[1] in ("tag", "push")]
    assert mutating == []
