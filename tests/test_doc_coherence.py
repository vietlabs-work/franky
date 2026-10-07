"""Doc-drift guard (issue #22).

A fast, offline consistency check that the documentation agrees with the code.
`pyproject.toml [project].version` is the single source of truth; README and
CHANGELOG are checked against it. Runs in the existing pytest job (no new CI step),
needs no docker/network, and finishes in well under a second.

The checks are pure functions over file *text* so they can be exercised against
synthetic drift; the final test runs them against the real repo files and asserts
the repo is coherent right now.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# A version reference in the docs is always v-prefixed (e.g. `@v0.1.0`, Status `v0.1.0`).
# Requiring the leading `v` is what keeps bare dotted numbers - notably the IP 127.0.0.1
# in the egress diagram - from being mistaken for a version. The negative lookbehind stops
# a trailing-`v` word (e.g. "dev0.1.0") from matching.
_VERSION_TOKEN = re.compile(r"(?<![A-Za-z0-9])v(\d+\.\d+\.\d+)")
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_CHANGELOG_HEADER = re.compile(r"^## \[([^\]]+)\](.*)$", re.MULTILINE)
_VERSIONED_HEADER = re.compile(r"^\d+\.\d+\.\d+$")
_HEADER_TAIL = re.compile(r"^ - \d{4}-\d{2}-\d{2}$")
_PLACEHOLDERS = ("X.Y.Z", "vX.Y.Z")


def pyproject_version(text: str) -> str:
    m = _PYPROJECT_VERSION.search(text)
    if not m:
        raise AssertionError("no version in pyproject.toml")
    return m.group(1)


def readme_version_tokens(readme: str) -> list[str]:
    """Every v-prefixed semver reference anywhere in the README (fenced blocks included)."""
    return _VERSION_TOKEN.findall(readme)


def placeholder_fences(readme: str) -> list[str]:
    """Fenced code blocks that still carry a literal X.Y.Z / vX.Y.Z install placeholder."""
    return [b for b in _FENCED_BLOCK.findall(readme) if any(p in b for p in _PLACEHOLDERS)]


def changelog_versioned_sections(changelog: str) -> list[str]:
    """Version strings of well-formed `## [X.Y.Z] - DATE` sections, in document order
    (newest first, since the file prepends)."""
    out = []
    for label, _ in _CHANGELOG_HEADER.findall(changelog):
        if _VERSIONED_HEADER.match(label):
            out.append(label)
    return out


def changelog_malformed_headers(changelog: str) -> list[str]:
    """`## [...]` headers that are neither `[Unreleased]` nor a valid `[X.Y.Z] - DATE`."""
    bad = []
    for label, tail in _CHANGELOG_HEADER.findall(changelog):
        if label == "Unreleased":
            continue
        if not (_VERSIONED_HEADER.match(label) and _HEADER_TAIL.match(tail)):
            bad.append(label)
    return bad


def _as_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


# --- check 1: README version refs match pyproject ---


@pytest.mark.parametrize(
    "readme,version,ok",
    [
        ("install ...@v0.1.0\nStatus: v0.1.0.", "0.1.0", True),
        ("install ...@v0.2.0 but Status still v0.1.0", "0.2.0", False),  # drift
        ("a diagram with --dns 127.0.0.1 only", "0.1.0", True),  # IP is not a version
        ("nothing versioned here", "9.9.9", True),  # no refs => vacuously fine
    ],
)
def test_readme_tokens_match_pyproject(readme, version, ok):
    tokens = readme_version_tokens(readme)
    assert all(t == version for t in tokens) is ok


# --- check 2: no literal placeholders in fenced blocks ---


@pytest.mark.parametrize(
    "readme,ok",
    [
        ("```\ninstall ...@v0.1.0\n```", True),
        ("```\ninstall ...@vX.Y.Z\n```", False),  # the #21 bug
        ("```\ninstall ...@X.Y.Z\n```", False),
        ("prose mentioning ghcr.io/franky:X.Y.Z as a pattern", True),  # not fenced => allowed
        ("```\n--dns 127.0.0.1\n```", True),  # IP in a fence is fine
    ],
)
def test_no_placeholder_in_fences(readme, ok):
    assert (len(placeholder_fences(readme)) == 0) is ok


# --- checks 3/4/5: changelog discipline ---


def test_changelog_unreleased_present():
    assert "## [Unreleased]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "changelog,bad",
    [
        ("## [Unreleased]\n\n## [0.1.0] - 2026-06-22\n", []),
        ("## [Unreleased]\n\n## [0.1.0]\n", ["0.1.0"]),  # missing date
        ("## [Unreleased]\n\n## [0.1] - 2026-06-22\n", ["0.1"]),  # not semver
        ("## [Unreleased]\n\n## [0.1.0] - 2026-6-2\n", ["0.1.0"]),  # bad date format
    ],
)
def test_changelog_header_formats(changelog, bad):
    assert changelog_malformed_headers(changelog) == bad


@pytest.mark.parametrize(
    "changelog,version,ok",
    [
        ("## [Unreleased]\n", "0.1.0", True),  # pre-first-release: no versioned section
        ("## [Unreleased]\n\n## [0.1.0] - 2026-06-22\n", "0.1.0", True),  # equal
        (
            "## [Unreleased]\n\n## [0.1.0] - 2026-06-22\n",
            "0.2.0",
            True,
        ),  # changelog behind code: ok
        ("## [Unreleased]\n\n## [0.3.0] - 2026-06-22\n", "0.1.0", False),  # changelog AHEAD: bug
        (
            "## [Unreleased]\n\n## [0.10.0] - 2026-06-22\n",
            "0.9.0",
            False,
        ),  # semver, not string, compare
    ],
)
def test_changelog_newest_not_ahead_of_pyproject(changelog, version, ok):
    sections = changelog_versioned_sections(changelog)
    newest_ok = (not sections) or _as_tuple(sections[0]) <= _as_tuple(version)
    assert newest_ok is ok


# --- the real repo must be coherent right now ---


def test_real_repo_is_coherent():
    version = pyproject_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert all(t == version for t in readme_version_tokens(readme)), (
        f"README version refs disagree with pyproject {version}"
    )
    assert placeholder_fences(readme) == [], (
        "README has a literal X.Y.Z placeholder in a code block"
    )
    assert "## [Unreleased]" in changelog
    assert changelog_malformed_headers(changelog) == []
    sections = changelog_versioned_sections(changelog)
    assert (not sections) or _as_tuple(sections[0]) <= _as_tuple(version), (
        "newest CHANGELOG version is ahead of pyproject"
    )


# --- README stays short and every fact has one home (AGENTS.md "Documentation") ---

README_MAX_LINES = 120
README_SECTIONS = {"Install", "Configure", "Engines", "Commands", "Docs", "Status"}
_DUP_MIN_CHARS = 60
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def doc_files() -> list[Path]:
    return [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]


def prose_lines(text: str) -> list[str]:
    """Lines outside code fences, whitespace-collapsed."""
    return [" ".join(line.split()) for line in _FENCED_BLOCK.sub("", text).splitlines()]


def duplicate_lines(texts: dict[str, str]) -> dict[str, list[str]]:
    """Long prose lines or table rows that appear more than once across the docs."""
    seen: dict[str, list[str]] = {}
    for name, text in texts.items():
        for line in prose_lines(text):
            if len(line) >= _DUP_MIN_CHARS:
                seen.setdefault(line, []).append(name)
    return {line: names for line, names in seen.items() if len(names) > 1}


def heading_anchors(text: str) -> set[str]:
    """GitHub-style anchors of every markdown heading."""
    out = set()
    for line in prose_lines(text):
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            out.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title)))
    return out


def broken_links(path: Path, text: str) -> list[str]:
    """Relative links whose file or heading anchor does not exist."""
    bad = []
    for target in _LINK.findall(_INLINE_CODE.sub("", _FENCED_BLOCK.sub("", text))):
        if re.match(r"[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            bad.append(target)
        elif anchor and dest.suffix == ".md":
            if anchor not in heading_anchors(dest.read_text(encoding="utf-8")):
                bad.append(target)
    return bad


@pytest.mark.parametrize(
    "texts,dups",
    [
        ({"a": "x" * 60, "b": "x" * 60}, 1),  # copied line
        ({"a": "x" * 59, "b": "x" * 59}, 0),  # short lines may repeat
        ({"a": "```\n" + "x" * 60 + "\n```", "b": "x" * 60}, 0),  # code is exempt
        ({"a": "| " + "y" * 60 + " |", "b": "|  " + "y" * 60 + "  |"}, 1),  # spacing ignored
    ],
)
def test_duplicate_lines(texts, dups):
    assert len(duplicate_lines(texts)) == dups


def test_heading_anchors():
    assert heading_anchors("## Review threads\n### `--thread` flag") == {
        "review-threads",
        "--thread-flag",
    }


def test_readme_is_short():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(readme.splitlines()) <= README_MAX_LINES, (
        f"README.md is over {README_MAX_LINES} lines; move detail to docs/ (AGENTS.md Documentation)"
    )


def test_readme_sections_allowed():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sections = set(re.findall(r"^## (.+)$", _FENCED_BLOCK.sub("", readme), re.MULTILINE))
    assert sections <= README_SECTIONS, f"README sections not allowed: {sections - README_SECTIONS}"


def test_every_doc_linked_from_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    unlinked = [p.name for p in (ROOT / "docs").glob("*.md") if f"(docs/{p.name})" not in readme]
    assert unlinked == [], f"docs not in the README Docs table: {unlinked}"


def test_doc_links_resolve():
    bad = {
        p.name: links
        for p in doc_files()
        if (links := broken_links(p, p.read_text(encoding="utf-8")))
    }
    assert bad == {}, f"broken doc links: {bad}"


def test_no_repeated_doc_lines():
    texts = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in doc_files()}
    dups = duplicate_lines(texts)
    assert dups == {}, "lines repeated across docs; keep one home and link to it:\n" + "\n".join(
        f"{names}: {line[:80]}" for line, names in dups.items()
    )
