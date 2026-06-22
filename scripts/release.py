#!/usr/bin/env python3
"""Release helper for franky.

Subcommands:
  X.Y.Z [--dry-run]  - full release: validate, check state, bump, changelog, commit, tag, push
  tag X.Y.Z [--dry-run]  - recovery: tag + push only (when commit exists but tag/push failed)
  guard <tag>        - assert pyproject==__init__==tag (CI gate, exits nonzero on skew)
  notes X.Y.Z        - print the changelog section body for X.Y.Z (used as release notes)
"""

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


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
    """Bump the version references README carries so the docs track a release in lockstep
    with pyproject/__init__. Targets exactly two anchored spots so unrelated dotted numbers
    (e.g. the `127.0.0.1` in the egress diagram) are never touched:
      - the `...franky@vX.Y.Z` install pins (both uv + pipx lines)
      - the `vX.Y.Z` on the line right under the `## Status` header

    Fails loudly if no install pin is found: a silent no-op would let a release ship with a
    stale documented install command (the exact #21 drift this is meant to prevent)."""
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    text, n_pins = re.subn(r"(franky@)v\d+\.\d+\.\d+", rf"\g<1>v{v}", text)
    if n_pins == 0:
        print(
            "release.py: no `franky@vX.Y.Z` install pin found in README.md to bump",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Status line is optional; bump it only if present (the guard catches any straggler).
    text = re.sub(r"(## Status\s+)v\d+\.\d+\.\d+", rf"\g<1>v{v}", text, count=1)
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
    status = _git(run, ["status", "--porcelain"]).stdout.strip()
    if status:
        print(f"release.py: working tree is dirty:\n{status}", file=sys.stderr)
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


def cmd_release(args, run, root: Path) -> None:
    v = args.version
    if not valid_version(v):
        print(
            f"release.py: invalid version '{v}' - use plain X.Y.Z (no prerelease, no leading v)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if args.dry_run:
        print(f"[dry-run] would bump pyproject.toml and franky/__init__.py to {v}")
        print(f"[dry-run] would update CHANGELOG.md: ## [Unreleased] -> ## [{v}] - {date.today()}")
        print(f"[dry-run] would bump README.md version refs (install pins + Status) to v{v}")
        print("[dry-run] git add pyproject.toml franky/__init__.py CHANGELOG.md README.md")
        print(f"[dry-run] git commit -m 'release: v{v}'")
        print(f"[dry-run] git tag -a v{v} -m v{v}")
        print(f"[dry-run] git push origin main v{v}")
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

    print(f"Released v{v}")


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


def main(argv=None, run=None, root=None) -> None:
    if run is None:
        run = subprocess.run
    if root is None:
        root = ROOT

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
        args = release_parser.parse_args(argv)
        args.subcommand = None
        cmd_release(args, run, root)
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

    args = parser.parse_args(argv)

    if args.subcommand == "tag":
        cmd_tag(args, run, root)
    elif args.subcommand == "guard":
        cmd_guard(args, run, root)
    elif args.subcommand == "notes":
        cmd_notes(args, run, root)
    else:
        parser.print_help()
        raise SystemExit(2)


if __name__ == "__main__":
    main()
