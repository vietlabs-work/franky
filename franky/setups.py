"""Agentic-setup sweep POLICY: which parts of a `~/.claude`-style config dir may be injected.

The operator declares a whole setup by directory (`claude = "~/.claude"`) instead of listing
files one by one, and this module decides what that expands to. It is pure policy in the same
sense as `egress.py`: no docker, no bundling, no packing - only walking the tree, applying the
allow/deny rules, and classifying what it found. `profile.py` consumes the result.

WHY a directory cannot simply be tarred: these dirs are enormous and hold exactly the things
that must never enter a container carrying live credentials. Measured on a real setup,
`~/.claude` was 1.1 GB and `~/.codex` 787 MB, of which ~950 KB was the useful instruction /
skill / command surface - the rest being conversation transcripts (`projects/`, `sessions/`),
plugin trees, caches, and `auth.json`. So the sweep is an ALLOWLIST of the capability-bearing
subtrees, with a deny gate layered on top for anything credential- or state-shaped that could
appear INSIDE one of those subtrees.

Three properties are load-bearing:

- **Deny is checked against the RESOLVED realpath too.** A symlink named `notes.md` pointing at
  `~/.codex/auth.json` must not smuggle a credential past a basename check.
- **Only UTF-8-decodable files ship.** Binary assets are skipped, never injected blind: the
  fail-closed secret scan in `profile.py` can only inspect text, so shipping bytes it cannot
  read would be an unscanned hole (and a PNG helps no agent anyway).
- **Denied directories are PRUNED, not filtered.** The bounded scanner never descends into
  `projects/` or `sessions/`, so a sweep stays fast instead of stat-ing a million files.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# One combined profile ceiling. A standalone setup sweep receives the same full budget.
# The bundle uses stdin, so these values guard host memory and configuration mistakes.
MAX_SWEEP_BYTES = 20 * 1024 * 1024
MAX_SWEEP_FILES = 5000


@dataclass
class ReadBudget:
    """Shared file-count, byte, and enumeration budget for one profile operation."""

    max_files: int
    max_bytes: int
    files: int = 0
    bytes: int = 0
    entries: int = 0

    @property
    def remaining_bytes(self) -> int:
        return self.max_bytes - self.bytes

    def add(self, size: int) -> None:
        if self.files >= self.max_files:
            raise ValueError(f"profile exceeds file limit {self.max_files}")
        if size > self.remaining_bytes:
            raise ValueError(f"profile exceeds size limit {self.max_bytes} bytes")
        self.files += 1
        self.bytes += size

    def visit_entry(self) -> None:
        if self.entries >= self.max_files:
            raise ValueError(f"profile enumeration exceeds entry limit {self.max_files}")
        self.entries += 1


def profile_budget() -> ReadBudget:
    """Return a budget that uses the current profile limits."""
    return ReadBudget(MAX_SWEEP_FILES, MAX_SWEEP_BYTES)


def claim_file(path: Path, budget: ReadBudget) -> None:
    """Charge a file from metadata, before a caller accumulates its path or content."""
    budget.add(path.stat().st_size)


def read_bounded(path: Path, budget: ReadBudget) -> bytes:
    """Read one file without allocating beyond the remaining profile budget."""
    size = path.stat().st_size
    if budget.files >= budget.max_files:
        raise ValueError(f"profile exceeds file limit {budget.max_files}")
    if size > budget.remaining_bytes:
        raise ValueError(f"profile exceeds size limit {budget.max_bytes} bytes")
    try:
        with path.open("rb") as source:
            raw = source.read(size + 1)
            if len(raw) > budget.remaining_bytes:
                raise ValueError(f"profile exceeds size limit {budget.max_bytes} bytes")
            extra = source.read(1)
            if extra:
                chunks = [raw, extra]
                total = len(raw) + 1
                while True:
                    chunk = source.read(min(64 * 1024, budget.remaining_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > budget.remaining_bytes:
                        raise ValueError(f"profile exceeds size limit {budget.max_bytes} bytes")
                    chunks.append(chunk)
                raw = b"".join(chunks)
    except OSError:
        budget.add(size)
        raise
    budget.add(len(raw))
    return raw


@dataclass(frozen=True)
class SetupManifest:
    """What one kind of agentic-coding setup exposes that is worth injecting.

    `files` are top-level filenames; `dirs` are top-level directories swept recursively.
    `mcp_hints` are files we deliberately do NOT inject but DO look at, so `profile check` can
    tell the operator "this setup declares MCP servers - enabling them is a separate, explicit
    step" (each server would add an egress host and forward a credential, which must never
    follow from a directory sweep).
    """

    default_root: str
    files: tuple[str, ...]
    dirs: tuple[str, ...]
    mcp_hints: tuple[str, ...] = ()


# Instruction file / skill / command / subagent-definition surface per kind. A path that does
# not exist is simply absent from the sweep, so one manifest can safely name the union of a
# tool's historical layouts (e.g. both `commands/` and `prompts/`).
SETUP_MANIFESTS: dict[str, SetupManifest] = {
    "claude": SetupManifest(
        default_root="~/.claude",
        files=("CLAUDE.md", "AGENTS.md"),
        dirs=("skills", "commands", "agents", "rules", "output-styles"),
        mcp_hints=("settings.json",),
    ),
    "codex": SetupManifest(
        default_root="~/.codex",
        files=("AGENTS.md", "CLAUDE.md"),
        dirs=("skills", "prompts", "commands", "agents", "rules"),
        mcp_hints=("config.toml",),
    ),
    "opencode": SetupManifest(
        default_root="~/.config/opencode",
        files=("AGENTS.md", "CLAUDE.md"),
        dirs=("skills", "command", "agent", "instructions"),
        mcp_hints=("opencode.json", "config.json"),
    ),
    "pi": SetupManifest(
        default_root="~/.pi",
        files=("AGENTS.md", "CLAUDE.md"),
        dirs=("skills", "commands", "prompts", "agents", "rules"),
    ),
}

SETUP_KINDS = tuple(SETUP_MANIFESTS)

# Directory names never descended into, wherever they appear in a swept tree. Transcripts and
# session state (privacy + size), caches/logs/plugin trees (noise + size), and VCS/vendor dirs.
DENY_DIRS = frozenset(
    {
        ".git",
        ".worktrees",
        "backups",
        "cache",
        "caches",
        "daemon",
        "debug",
        "file-history",
        "history",
        "log",
        "logs",
        "memories",
        "node_modules",
        "packages",
        "paste-cache",
        "plugins",
        "projects",
        "sessions",
        "shell-snapshots",
        "shell_snapshots",
        "statsig",
        "temp",
        "tmp",
        "todos",
        "vendor_imports",
        "venv",
        ".venv",
    }
)

# Basename patterns (fnmatch, case-insensitive) never injected. The include allowlist already
# excludes most of these by construction; this is the second gate, so widening a manifest later
# cannot accidentally start shipping credentials or local state.
DENY_NAMES = (
    ".credentials.json",
    ".ds_store",
    ".env",
    ".env.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.db",
    "*.jsonl",
    "*.key",
    "*.p12",
    "*.pem",
    "*.pfx",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "auth.json",
    "config.toml",
    "credentials.json",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "id_rsa*",
    "settings.json",
    "settings.local.json",
    "token.json",
)

# Where a PR-description spec lives by convention across these tools: a `pr` command / prompt
# (`commands/pr.md`, `prompts/pr.md`) or a `pr` skill (`skills/pr/SKILL.md`).
_PR_SPEC_STEMS = ("pr", "pull-request", "pull_request")


@dataclass
class SetupScan:
    """The outcome of sweeping one setup directory."""

    kind: str
    root: Path
    files: list[Path] = field(default_factory=list)
    skipped_binary: list[Path] = field(default_factory=list)
    mcp_hints: list[Path] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(_size(p) for p in self.files)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def denied_name(name: str) -> bool:
    """True if this basename may never be injected, whatever directory it sits in."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in DENY_NAMES)


def _scandir_bounded(root: Path, budget: ReadBudget) -> list[os.DirEntry[str]]:
    """Return sorted directory entries without reading past the shared entry limit."""
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                budget.visit_entry()
                entries.append(entry)
    except OSError:
        pass
    return sorted(entries, key=lambda entry: entry.name)


def _admissible(path: Path, bases: tuple[Path, ...]) -> bool:
    """Deny gate, applied to the declared name AND to the symlink-resolved realpath.

    `bases` are the resolved sweep root and HOME. The denied-DIRECTORY check runs ONLY on the
    part of the resolved path BELOW one of them - never on the absolute path. Components ABOVE
    the root are ambient and none of our business: a machine whose setups live under a directory
    named `tmp`, `cache`, or `log` (all DENY_DIRS entries) would otherwise sweep ZERO files and
    say nothing about it. That is exactly what happened on Linux CI, where pytest's tmp_path is
    `/tmp/...` while macOS uses `/private/var/folders/...` - the bug passed locally and failed
    everywhere else.

    A resolved path outside both bases means the operator symlinked out of their own tree; the
    name deny still governs it, but "is it inside a directory we exclude" has no meaning there.
    """
    if denied_name(path.name):
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if denied_name(resolved.name):
        return False
    for base in bases:
        try:
            rel = resolved.relative_to(base)
        except ValueError:
            continue
        # A symlink pointing INTO a denied directory (…/.claude/sessions/x.md) is denied too.
        return not any(part in DENY_DIRS for part in rel.parts)
    return True


def _admissible_dir(path: Path, bases: tuple[Path, ...]) -> bool:
    """Return whether traversal may enter a directory, including through a symlink."""
    if path.name in DENY_DIRS or path.name.startswith(".git"):
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for base in bases:
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        return not any(part in DENY_DIRS or part.startswith(".git") for part in relative.parts)
    return True


def _walk_dir(
    root: Path,
    bases: tuple[Path, ...],
    visited: set[str],
    budget: ReadBudget,
):
    """Yield admissible files under `root`, pruning denied dirs and symlink loops."""
    pending = [root]
    while pending:
        directory = pending.pop()
        # Symlinked dirs are followed (operators symlink skills in from plugin checkouts), so a
        # cycle is possible - remember realpaths and never revisit one.
        real = os.path.realpath(directory)
        if real in visited:
            continue
        if len(visited) >= budget.max_files:
            raise ValueError(f"setup traversal exceeds entry limit {budget.max_files}")
        visited.add(real)

        child_dirs: list[Path] = []
        for entry in _scandir_bounded(directory, budget):
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=True):
                    if _admissible_dir(path, bases):
                        child_dirs.append(path)
                    continue
                is_file = entry.is_file(follow_symlinks=True)
            except OSError:
                continue
            if not is_file or not _admissible(path, bases):
                continue
            yield path

        if len(pending) + len(child_dirs) > budget.max_files:
            raise ValueError(f"setup traversal exceeds entry limit {budget.max_files}")
        pending.extend(reversed(child_dirs))


def expand_setup(kind: str, root: Path, budget: ReadBudget | None = None) -> SetupScan:
    """Sweep one setup directory into the concrete files that may be injected.

    Raises ValueError for an unknown kind, a non-directory root, or a profile limit breach.
    `load_profile` supplies its shared budget. A direct call gets the full profile budget.
    """
    manifest = SETUP_MANIFESTS.get(kind)
    if manifest is None:
        raise ValueError(f"unknown setup kind {kind!r} - supported kinds: {', '.join(SETUP_KINDS)}")
    if not root.is_dir():
        raise ValueError(f"setup {kind!r} root is not a directory: {root}")

    # The bases the denied-DIRECTORY check is measured against: the sweep root, plus HOME so a
    # symlink hopping to another setup (~/.claude/skills/x -> ~/.codex/sessions/y) is still caught.
    bases = (root.resolve(), Path.home().resolve())

    budget = budget or profile_budget()
    scan = SetupScan(kind=kind, root=root)
    visited: set[str] = set()

    def add(path: Path) -> None:
        try:
            raw = read_bounded(path, budget)
        except OSError:
            scan.skipped_binary.append(path)
            return
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            scan.skipped_binary.append(path)
            return
        scan.files.append(path)

    for name in manifest.files:
        path = root / name
        if path.is_file() and _admissible(path, bases):
            add(path)
    for name in manifest.dirs:
        sub = root / name
        if sub.is_dir():
            for path in _walk_dir(sub, bases, visited, budget):
                add(path)

    for name in manifest.mcp_hints:
        hint = root / name
        if hint.is_file() and _declares_mcp(hint):
            scan.mcp_hints.append(hint)

    return scan


def _declares_mcp(path: Path) -> bool:
    """Cheap text probe for an MCP server declaration in a config file we do NOT inject."""
    try:
        text = read_bounded(path, profile_budget()).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return "mcp_servers" in text or "mcpServers" in text


def find_pr_spec(files: list[Path]) -> Path | None:
    """Pick the operator's PR-description spec out of a swept file list, by convention.

    Matches a `pr` command/prompt (`commands/pr.md`) or a `pr` skill (`skills/pr/SKILL.md`).
    Returns the SHORTEST matching path so a top-level command beats a nested reference file,
    and None when the setup has no PR spec (the prompt then just keeps Franky's own shape).
    """
    matches = [
        path
        for path in files
        if path.suffix.lower() == ".md"
        and (path.stem.lower() in _PR_SPEC_STEMS or path.parent.name.lower() in _PR_SPEC_STEMS)
    ]
    if not matches:
        return None
    return min(matches, key=lambda p: (len(p.parts), str(p)))
