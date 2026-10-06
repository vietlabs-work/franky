#!/usr/bin/env python3
"""Release helper for franky.

Subcommands:
  X.Y.Z [--dry-run]  - full release: validate, check state, bump, changelog, commit, tag, push
  tag X.Y.Z [--dry-run]  - recovery: tag + push only (when commit exists but tag/push failed)
  guard <tag>        - assert pyproject==__init__==tag (CI gate, exits nonzero on skew)
  notes X.Y.Z        - print the changelog section body for X.Y.Z (used as release notes)
  changelog-check --base SHA - PR gate: shipped code changed => a new ## [Unreleased] bullet
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

# Cap on how long `make release` will tail the triggered workflow before it stops watching
# (the release itself is already pushed and keeps going regardless). The multi-arch image
# builds dominate and run several minutes; 20 min is comfortable headroom.
_WATCH_TIMEOUT = 1200


def _find_root(start: Path) -> Path:
    """Walk up from start until we find a directory containing pyproject.toml."""
    current = start.resolve()
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise SystemExit("release.py: could not locate pyproject.toml from " + str(start))


ROOT = _find_root(Path(__file__).parent)


def valid_version(s: str) -> bool:
    """True iff s is a plain X.Y.Z version string (no prerelease, no leading v)."""
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", s))


def read_pyproject_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("release.py: could not find version in pyproject.toml")
    return m.group(1)


def read_init_version(root: Path) -> str:
    text = (root / "franky" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("release.py: could not find __version__ in franky/__init__.py")
    return m.group(1)


def set_pyproject_version(root: Path, v: str) -> None:
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    new_text = re.sub(r'^(version\s*=\s*)"[^"]+"', rf'\g<1>"{v}"', text, flags=re.MULTILINE)
    path.write_text(new_text, encoding="utf-8")


def set_init_version(root: Path, v: str) -> None:
    path = root / "franky" / "__init__.py"
    text = path.read_text(encoding="utf-8")
    new_text = re.sub(r'^(__version__\s*=\s*)"[^"]+"', rf'\g<1>"{v}"', text, flags=re.MULTILINE)
    path.write_text(new_text, encoding="utf-8")


def set_readme_version(root: Path, v: str) -> None:
    """Bump the `vX.Y.Z` on the line right under the `## Status` header so the docs track a
    release in lockstep with pyproject/__init__. Anchored to that one spot so unrelated dotted
    numbers (e.g. the `127.0.0.1` in the egress diagram) are never touched.

    The PyPI install command carries no version pin (`uv tool install franky-agent` always
    fetches the latest), so there is no install pin to bump - the Status line is the only
    versioned README reference. Fails loudly if it is missing: a silent no-op would let a
    release ship with a stale documented version (the #21 drift this guards against)."""
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    text, n = re.subn(r"(## Status\s+)v\d+\.\d+\.\d+", rf"\g<1>v{v}", text, count=1)
    if n == 0:
        print(
            "release.py: no `## Status` version line found in README.md to bump",
            file=sys.stderr,
        )
        raise SystemExit(1)
    path.write_text(text, encoding="utf-8")


def update_changelog(root: Path, v: str, date_str: str) -> None:
    """Replace first ## [Unreleased] with ## [X.Y.Z] - DATE and insert a fresh ## [Unreleased] above.

    Fails loudly if there is no ## [Unreleased] header: a silent no-op here would let a release
    commit + immutable tag get pushed (and the GHCR images published) with a stale changelog,
    after which the `notes` step strands the publish with no GitHub Release."""
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    if "## [Unreleased]" not in text:
        print(
            "release.py: CHANGELOG.md has no '## [Unreleased]' section to release", file=sys.stderr
        )
        raise SystemExit(1)
    versioned = f"## [{v}] - {date_str}"
    # Two-step: first retitle [Unreleased] -> the versioned header (so its body becomes the
    # release notes), then re-insert a fresh empty [Unreleased] above that versioned header.
    new_text = text.replace("## [Unreleased]", versioned, 1)
    new_text = new_text.replace(versioned, f"## [Unreleased]\n\n{versioned}", 1)
    path.write_text(new_text, encoding="utf-8")


def extract_notes(root: Path, v: str) -> str:
    """Return the changelog section body for version v (everything until the next ## header)."""
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    header = f"## [{v}]"
    idx = text.find(header)
    if idx == -1:
        print(f"release.py: no changelog section for {v}", file=sys.stderr)
        raise SystemExit(1)
    after_header = text[idx:]
    # Skip past the header line itself
    newline_pos = after_header.find("\n")
    if newline_pos == -1:
        return ""
    body = after_header[newline_pos + 1 :]
    # Find the next ## header
    next_section = re.search(r"^## ", body, re.MULTILINE)
    if next_section:
        body = body[: next_section.start()]
    body = body.strip()
    if not body:
        print(f"release.py: changelog section for {v} is empty", file=sys.stderr)
        raise SystemExit(1)
    return body


# Paths that ship to users (the package, the images, the install metadata). A PR touching any
# of them must add a ## [Unreleased] bullet, or `make release` later finds an empty section.
_SHIPPED = (
    "franky/",
    "proxy/",
    "Dockerfile",
    "franky-dind-entrypoint.sh",
    "install-codex-native-launcher.sh",
    "pyproject.toml",
)


# Any unordered-list item (`-`, `*`, `+`, indented or not); captures its text for comparison.
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(\S.*)$", re.MULTILINE)


def _unreleased_bullets(text: str) -> set[str]:
    """The bullet texts of the ## [Unreleased] section (empty set if there is none)."""
    idx = text.find("## [Unreleased]")
    if idx == -1:
        return set()
    body = text[idx:].split("\n", 1)[-1]
    nxt = re.search(r"^## ", body, re.MULTILINE)
    body = body[: nxt.start()] if nxt else body
    return {m.group(1).strip() for m in _BULLET_RE.finditer(body)}


def _released_bullets(text: str) -> Counter:
    """Bullet counts of every versioned section (the text with ## [Unreleased] cut out).

    A Counter, so a duplicate of a bullet already in an old release still counts as added.
    """
    idx = text.find("## [Unreleased]")
    if idx != -1:
        nxt = re.search(r"^## ", text[idx + 1 :], re.MULTILINE)
        text = text[:idx] + (text[idx + 1 + nxt.start() :] if nxt else "")
    return Counter(m.group(1).strip() for m in _BULLET_RE.finditer(text))


def cmd_changelog_check(args, run, root: Path) -> None:
    files = _git(run, ["diff", "--name-only", f"{args.base}...HEAD"]).stdout.split()
    shipped = [f for f in files if f.startswith(_SHIPPED)]
    # A missing base CHANGELOG.md (first commit) counts as no prior bullets.
    base = _git(run, ["show", f"{args.base}:CHANGELOG.md"], check=False)
    base_text = base.stdout if base.returncode == 0 else ""
    head_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    # A release cut after the branch forked leaves a stale bullet inside the released section
    # (git merges it there silently), and `make release` then finds an empty Unreleased.
    added = list((_released_bullets(head_text) - _released_bullets(base_text)).elements())
    if added:
        print(
            "release.py: CHANGELOG.md gains a bullet inside a released version section "
            f"({added[0][:60]!r}). Released sections are frozen: rebase on the base "
            "branch and move the bullet under ## [Unreleased].",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not shipped:
        print("release.py: no shipped files changed; no changelog entry needed")
        return
    before = _unreleased_bullets(base_text)
    after = _unreleased_bullets(head_text)
    if after - before:
        print("release.py: changelog entry found under ## [Unreleased]")
        return
    print(
        "release.py: this PR changes shipped files but adds no bullet under ## [Unreleased] "
        f"in CHANGELOG.md ({', '.join(shipped[:5])}). Add one under Added / Changed / Fixed, "
        "or label the PR `no-changelog` if users see no change.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def assert_versions_match(root: Path, tag: str) -> None:
    """Assert pyproject, __init__, and tag all carry the same version. Exits nonzero on any mismatch."""
    tag_ver = tag.lstrip("v")
    pyproject_ver = read_pyproject_version(root)
    init_ver = read_init_version(root)
    if pyproject_ver == init_ver == tag_ver:
        return
    print(
        f"release.py: version skew detected!\n"
        f"  pyproject.toml : {pyproject_ver}\n"
        f"  franky/__init__.py: {init_ver}\n"
        f"  tag            : {tag_ver}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _git(run, argv, check=True):
    """Run a git command, returning the CompletedProcess. Exits on nonzero if check=True."""
    proc = run(["git"] + argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print(f"release.py: git {' '.join(argv)} failed:\n{proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return proc


def _assert_clean_tree(run) -> None:
    # Only uncommitted edits to TRACKED files matter: both callers (cmd_release, cmd_tag) stage
    # by name (git add pyproject.toml ...) or don't stage at all - never `git add .` - so a stray
    # tracked edit would be swept into the release commit, but untracked files (agent worktrees,
    # editor scratch) can never enter the release. --untracked-files=no lets a release run in a
    # working dir that has unrelated untracked clutter.
    status = _git(run, ["status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    if status:
        print(f"release.py: tracked files have uncommitted changes:\n{status}", file=sys.stderr)
        raise SystemExit(1)


def _assert_tag_absent(run, v: str) -> None:
    tag_check = run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/v{v}"], capture_output=True, text=True
    )
    if tag_check.returncode == 0:
        print(f"release.py: tag v{v} already exists", file=sys.stderr)
        raise SystemExit(1)


def _assert_release_preconditions(root: Path, v: str, run) -> None:
    """Check branch, tree cleanliness, sync with origin, and tag absence."""
    _git(run, ["fetch", "origin"])

    branch = _git(run, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != "main":
        print(f"release.py: must be on main branch (currently on '{branch}')", file=sys.stderr)
        raise SystemExit(1)

    _assert_clean_tree(run)

    local_head = _git(run, ["rev-parse", "HEAD"]).stdout.strip()
    origin_head = _git(run, ["rev-parse", "origin/main"]).stdout.strip()
    if local_head != origin_head:
        print(
            f"release.py: local main ({local_head[:8]}) differs from origin/main ({origin_head[:8]})",
            file=sys.stderr,
        )
        raise SystemExit(1)

    _assert_tag_absent(run, v)


def _assert_release_inputs(root: Path, v: str) -> None:
    current = read_pyproject_version(root)
    if tuple(map(int, v.split("."))) <= tuple(map(int, current.split("."))):
        print(
            f"release.py: version {v} must be newer than current version {current}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    extract_notes(root, "Unreleased")


def _origin_web_url(run):
    """Best-effort https://github.com/owner/repo from the origin remote, or None if it can't be
    derived. Handles ssh (git@github.com:o/r.git), ssh-url, and https forms."""
    try:
        proc = run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", proc.stdout.strip())
    return f"https://github.com/{m.group(1)}" if m else None


def _find_release_run_id(run, v: str, sleep, attempts: int = 6, delay: int = 5):
    """Poll for the tag-triggered Release run and return its id as a str, else None.

    A tag-triggered run carries the tag name in headBranch, so `--branch vX.Y.Z` finds it
    (`--limit 1` = the newest, which is what a fresh push or a manual re-run produces). The
    default budget (~25s: attempts at t=0,5,10,15,20,25) covers GitHub's usual push->run
    registration latency; on a slow-dispatch day it returns None and the caller degrades to
    links. Returns None on gh-not-installed (OSError) or a persistent gh error."""
    tag = f"v{v}"
    for i in range(attempts):
        try:
            proc = run(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    "release.yml",
                    "--branch",
                    tag,
                    "--limit",
                    "1",
                    "--json",
                    "databaseId",
                ],
                capture_output=True,
                text=True,
            )
        except OSError:
            return None  # gh not installed
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                runs = json.loads(proc.stdout)
            except (json.JSONDecodeError, TypeError):
                runs = []
            if runs:
                return str(runs[0]["databaseId"])
        if i < attempts - 1:
            sleep(delay)
    return None


def _watch_release(run, v: str, sleep) -> None:
    """After the tag push, tail the triggered Release workflow to completion and report the
    outcome. Best-effort: degrades to printable links if gh is missing or the run can't be
    found, and never raises (the release is already pushed)."""
    web = _origin_web_url(run)
    actions_url = f"{web}/actions" if web else "the repo's Actions tab"
    release_url = f"{web}/releases/tag/v{v}" if web else f"the v{v} release page"

    print(
        f"\nPushed release commit and tag v{v}. The Release workflow now builds the wheel,\n"
        "publishes it to PyPI, pushes the GHCR images, and publishes the GitHub Release -\n"
        "this takes several minutes.",
    )

    run_id = _find_release_run_id(run, v, sleep)
    if run_id is None:
        # The release IS pushed and the workflow runs regardless - we just couldn't tail it.
        print("\nCouldn't attach to the workflow run. If gh is installed, check `gh auth status`;")
        print("otherwise the run may simply not have registered yet. The release is unaffected:")
        print(f"  Progress: {actions_url}")
        print(f"  Release:  {release_url}  (appears when the workflow finishes)")
        return

    print(f"\nWatching run {run_id} (Ctrl-C stops watching; the release keeps running):\n")
    try:
        # start_new_session so the timeout kill reaps gh and any child (e.g. a pager), not just
        # the parent. No capture_output: gh run watch streams its live progress to the terminal.
        proc = run(
            ["gh", "run", "watch", run_id, "--exit-status"],
            timeout=_WATCH_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        print(f"\nStopped watching after {_WATCH_TIMEOUT // 60} min; the workflow may still run.")
        print(f"  Progress: {actions_url}")
        return
    except OSError:
        print(f"  Progress: {actions_url}")
        return

    if proc.returncode == 0:
        print(f"\nRelease published: {release_url}")
    else:
        print("\nThe Release workflow did NOT succeed. Commit + tag are pushed; inspect:")
        print(f"  Progress: {actions_url}")


def _print_release_links(run, v: str) -> None:
    """Honest one-shot status used with --no-watch: the push triggered the workflow, here's where
    to track it."""
    web = _origin_web_url(run)
    print(f"\nPushed release commit and tag v{v}. The Release workflow is now building +")
    print("publishing (several minutes; not done yet). Track it:")
    print(f"  Progress: {web + '/actions' if web else 'the repo Actions tab'}")
    print(f"  Release:  {web + f'/releases/tag/v{v}' if web else f'the v{v} release page'}")


def cmd_release(args, run, root: Path, sleep=time.sleep) -> None:
    v = args.version
    if not valid_version(v):
        print(
            f"release.py: invalid version '{v}' - use plain X.Y.Z (no prerelease, no leading v)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    _assert_release_inputs(root, v)

    if args.dry_run:
        print(f"[dry-run] would bump pyproject.toml and franky/__init__.py to {v}")
        print(f"[dry-run] would update CHANGELOG.md: ## [Unreleased] -> ## [{v}] - {date.today()}")
        print(f"[dry-run] would bump README.md version refs (install pins + Status) to v{v}")
        print("[dry-run] git add pyproject.toml franky/__init__.py CHANGELOG.md README.md")
        print(f"[dry-run] git commit -m 'release: v{v}'")
        print(f"[dry-run] git tag -a v{v} -m v{v}")
        print(f"[dry-run] git push origin main v{v}")
        print("[dry-run] the tag push triggers the Release workflow (wheel -> PyPI, GHCR")
        print("[dry-run]   images, GitHub Release); would then watch it unless --no-watch")
        return

    _assert_release_preconditions(root, v, run)

    today = str(date.today())
    set_pyproject_version(root, v)
    set_init_version(root, v)
    update_changelog(root, v, today)
    set_readme_version(root, v)

    _git(run, ["add", "pyproject.toml", "franky/__init__.py", "CHANGELOG.md", "README.md"])
    _git(run, ["commit", "-m", f"release: v{v}"])
    _git(run, ["tag", "-a", f"v{v}", "-m", f"v{v}"])

    push_proc = run(["git", "push", "origin", "main", f"v{v}"], capture_output=True, text=True)
    if push_proc.returncode != 0:
        print(
            f"release.py: push failed (commit and tag created locally):\n{push_proc.stderr.strip()}",
            file=sys.stderr,
        )
        print(
            f"\nRecovery: once the push issue is resolved, run:\n"
            f"  python3 scripts/release.py tag {v}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # The push only TRIGGERS the release; the GitHub Release + GHCR images are built async by
    # the tag workflow. Don't claim "Released" - report the real state and (by default) watch.
    if args.no_watch:
        _print_release_links(run, v)
    else:
        _watch_release(run, v, sleep)


def cmd_tag(args, run, root: Path) -> None:
    v = args.version
    if not valid_version(v):
        print(f"release.py: invalid version '{v}' - use plain X.Y.Z", file=sys.stderr)
        raise SystemExit(1)

    assert_versions_match(root, f"v{v}")
    _assert_clean_tree(run)
    _assert_tag_absent(run, v)

    if args.dry_run:
        print(f"[dry-run] git tag -a v{v} -m v{v}")
        print(f"[dry-run] git push origin v{v}")
        return

    _git(run, ["tag", "-a", f"v{v}", "-m", f"v{v}"])
    _git(run, ["push", "origin", f"v{v}"])
    print(f"Tagged and pushed v{v}")


def cmd_guard(args, run, root: Path) -> None:
    assert_versions_match(root, args.tag)
    print(f"OK: all version sources match {args.tag.lstrip('v')}")


def cmd_notes(args, run, root: Path) -> None:
    v = args.version
    notes = extract_notes(root, v)
    print(notes)


def main(argv=None, run=None, root=None, sleep=None) -> None:
    if run is None:
        run = subprocess.run
    if root is None:
        root = ROOT
    if sleep is None:
        sleep = time.sleep

    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    # If the first argument looks like a plain version (X.Y.Z), treat it as the release
    # subcommand. This avoids ambiguity when argparse tries to resolve it as a subcommand
    # choice, which happens on Python 3.14+ with the combined subparsers+positional layout.
    if argv and re.fullmatch(r"\d+\.\d+\.\d+", argv[0]):
        release_parser = argparse.ArgumentParser(prog="release.py X.Y.Z")
        release_parser.add_argument("version", metavar="X.Y.Z")
        release_parser.add_argument("--dry-run", action="store_true")
        release_parser.add_argument(
            "--no-watch",
            action="store_true",
            help="Don't tail the triggered Release workflow; just print where to track it.",
        )
        args = release_parser.parse_args(argv)
        args.subcommand = None
        cmd_release(args, run, root, sleep)
        return

    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Release helper for franky.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    # tag subcommand
    tag_parser = subparsers.add_parser("tag", help="Recovery: tag + push only.")
    tag_parser.add_argument("version", metavar="X.Y.Z")
    tag_parser.add_argument("--dry-run", action="store_true")

    # guard subcommand
    guard_parser = subparsers.add_parser("guard", help="Assert all version sources match the tag.")
    guard_parser.add_argument(
        "tag", metavar="TAG", help="The tag to check against (vX.Y.Z or X.Y.Z)."
    )

    # notes subcommand
    notes_parser = subparsers.add_parser("notes", help="Print changelog section for a version.")
    notes_parser.add_argument("version", metavar="X.Y.Z")

    # changelog-check subcommand
    check_parser = subparsers.add_parser(
        "changelog-check", help="PR gate: shipped changes need a new ## [Unreleased] bullet."
    )
    check_parser.add_argument("--base", required=True, metavar="SHA")

    args = parser.parse_args(argv)

    if args.subcommand == "tag":
        cmd_tag(args, run, root)
    elif args.subcommand == "guard":
        cmd_guard(args, run, root)
    elif args.subcommand == "notes":
        cmd_notes(args, run, root)
    elif args.subcommand == "changelog-check":
        cmd_changelog_check(args, run, root)
    else:
        parser.print_help()
        raise SystemExit(2)


if __name__ == "__main__":
    main()
