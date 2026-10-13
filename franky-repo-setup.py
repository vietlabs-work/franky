#!/usr/bin/env python3
"""franky-repo-setup: load a repository's own agent setup into the task container, before the engine.

Runs INSIDE the task container (python3 + git only, stdlib only) as uid 1001, started by the host
with `docker exec` during the profile-wait phase, so the engine starts with the repository's
skills and rules already in place. Two subcommands:

  map   --repo owner/name --ref <40-hex sha> --engine <name>
        Fetch ONLY the setup paths of `ref` (a blobless, depth-1, sparse fetch into a private temp
        dir), copy the text into the engine's native locations under $HOME, and write the index
        the prompt shows (`$HOME/.franky-repo-setup/index.txt`). Prints one JSON summary line.
  exec  --marker <token> -- <argv...>
        Replace the first occurrence of the per-run index marker (a random token the host made)
        with that index, then exec the engine. Untrusted prompt text cannot carry the token.

The host chooses the ref: the default-branch head, for every command. A PR author controls the
PR head and can pick the PR base, so neither is read as setup. The working directory stays
/work, so a repository's own `.claude/settings.json` hooks never load. Residual: the engine's
cwd holds the PR checkout, so an engine that also reads instruction files from its cwd can see
PR-head files; this mapper only controls what it installs, not what the engine finds there.

What it reads: AGENTS.md, CLAUDE.md, .claude/{skills,rules,agents}, .agents/skills,
.codex/skills, agent/rules. What it never takes: symlinks, executables, submodules, settings,
hooks, MCP files, env files, keys, credentials, non-UTF-8 files, anything outside those roots,
and, inside a skill directory, `*.yaml`, `*.yml`, `*.toml`, `*.json` files and `agents/` dirs.
Caps: 500 files, 4 MiB in total (both fail the run), 256 KiB per file (the file is skipped,
also at fetch time: a larger blob is never downloaded), 16 KiB for the index.
Repository text joins the instruction file inside one fenced code block (a fence longer than any
backtick run in it), so it can neither import a file with `@path` nor fake a heading.


Any fetch or mapping error exits nonzero with one short message and no secret in it: the host
then fails the run before the engine starts. A repository with no setup is normal.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = "__FRANKY_REPO_SETUP_INDEX__"
INDEX_DIR = ".franky-repo-setup"
NO_INDEX = "Repository setup: not loaded."

MAX_FILES = 500
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_SKILLS_LISTED = 60
MAX_RULES_LISTED = 60
MAX_INDEX_BYTES = 16 * 1024
BLOB_LIMIT = MAX_FILE_BYTES + 1  # blob:limit=<n> omits blobs of n bytes or more
SKILL_DENIED_SUFFIXES = (".yaml", ".yml", ".toml", ".json")
DESCRIPTION_CHARS = 160
# One deadline for the whole `map`: the host gives up at 100 s and the entrypoint at 120 s.
DEADLINE_SECS = 90
_deadline = [time.monotonic() + DEADLINE_SECS]

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# engine -> (skills dir, instruction file, agents dir or None), HOME-relative.
ENGINES = {
    "claude": (".claude/skills", ".claude/CLAUDE.md", ".claude/agents"),
    "codex": (".codex/skills", ".codex/AGENTS.md", None),
    "opencode": (".config/opencode/skills", ".config/opencode/AGENTS.md", None),
    "pi": (".pi/skills", ".pi/AGENTS.md", None),
}

# Repo roots in lookup order. The engine's own skills root wins over the neutral ones.
SKILL_ROOTS = {
    "claude": (".claude/skills", ".agents/skills", ".codex/skills"),
    "codex": (".codex/skills", ".agents/skills", ".claude/skills"),
    "opencode": (".agents/skills", ".claude/skills", ".codex/skills"),
    "pi": (".agents/skills", ".claude/skills", ".codex/skills"),
}
RULE_ROOTS = (".claude/rules", "agent/rules")
AGENT_ROOT = ".claude/agents"
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
SKILL_FRONTMATTER_KEYS = ("name", "description")
AGENT_FRONTMATTER_KEYS = ("name", "description", "tools", "model")
_ALL_ROOTS = (*RULE_ROOTS, AGENT_ROOT, *sorted({r for rs in SKILL_ROOTS.values() for r in rs}))

_DENY_NAMES = (
    ".credentials.json",
    ".ds_store",
    ".env",
    ".env.*",
    ".mcp.json",
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
    "settings*.json",
    "token.json",
)
_DENY_DIRS = frozenset({"hooks", "node_modules", "plugins", "projects", "sessions", "vendor"})


class SetupError(Exception):
    """A fetch or mapping failure. The message is short and never carries a secret."""


def denied(rel: str) -> bool:
    """True if this repo-relative path must never be taken, whatever root it sits under."""
    parts = rel.split("/")
    if any(part.lower() in _DENY_DIRS or part.startswith(".git") for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name.startswith(".git"):
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _DENY_NAMES)


def _under(rel: str, root: str) -> bool:
    return rel.startswith(root + "/")


def _skill_roots() -> set[str]:
    return {r for roots in SKILL_ROOTS.values() for r in roots}


def skill_denied(rel: str) -> bool:
    """True for a path inside a skill dir that must not load: structured config and `agents/`."""
    for root in _skill_roots():
        if not _under(rel, root):
            continue
        inner = rel[len(root) + 1 :].lower().split("/")
        if len(inner) < 2:
            return False
        return inner[-1].endswith(SKILL_DENIED_SUFFIXES) or "agents" in inner[1:-1]
    return False


def _git_env(remote: str, home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    token = os.environ.get("GH_TOKEN")
    if token and remote.startswith("https://github.com/"):
        # Through the environment, never the argv: the header is not visible in `ps`.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


def _git(args: list[str], cwd: Path, env: dict[str, str], *, stdin: bytes | None = None) -> bytes:
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    verb = next((a for a in args if not a.startswith("-") and "=" not in a), "command")
    left = _deadline[0] - time.monotonic()
    if left <= 1:
        raise SetupError(f"repository setup timed out after {DEADLINE_SECS}s")
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            timeout=left,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"git {verb} timed out (limit {DEADLINE_SECS}s)") from exc
    except OSError as exc:
        raise SetupError(f"could not run git: {exc.__class__.__name__}") from exc
    if proc.returncode != 0:
        # The git message may echo the remote URL; it never holds the header (env-only).
        last = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        raise SetupError(f"git {verb} failed: {last[:200]}")
    return proc.stdout


def select_paths(tree: bytes) -> list[str]:
    """Pick the setup paths out of `git ls-tree -r -z` output; symlinks, exec bits, submodules out."""
    chosen: list[str] = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        meta, _, raw = record.partition(b"\t")
        try:
            rel = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        fields = meta.split()
        if len(fields) < 2 or fields[1] != b"blob" or fields[0] != b"100644":
            continue  # 120000 symlink, 160000 submodule, 100755 executable: never taken
        if any(ch in rel for ch in "*?[]\\\n") or rel.startswith(("!", "#")):
            continue  # would be read as a sparse-checkout pattern
        if denied(rel) or skill_denied(rel):
            continue
        if rel in INSTRUCTION_FILES or any(_under(rel, root) for root in _ALL_ROOTS):
            chosen.append(rel)
    return sorted(chosen)


def fetch_setup(
    remote: str, ref: str, dest: Path, home: Path, too_large: list[str] | None = None
) -> list[str]:
    """Fetch only the setup paths of `ref` into `dest`; return the paths that landed.

    A blob over the per-file cap is never downloaded; its path goes into `too_large`.
    """
    env = _git_env(remote, home)
    _git(["init", "-q"], dest, env)
    _git(["remote", "add", "origin", remote], dest, env)
    _git(
        ["-c", "protocol.version=2", "fetch", "-q", "--depth=1", "--no-tags"]
        + [f"--filter=blob:limit={BLOB_LIMIT}", "origin", ref],
        dest,
        env,
    )
    specs = [f"{p}" for p in (*INSTRUCTION_FILES, *_ALL_ROOTS)]
    paths = select_paths(_git(["ls-tree", "-r", "-z", "--full-tree", ref, "--", *specs], dest, env))
    if len(paths) > MAX_FILES:
        raise SetupError(f"repository setup has {len(paths)} files, over the limit {MAX_FILES}")
    if not paths:
        return []
    paths, large = _split_missing_blobs(paths, ref, dest, env)
    if too_large is not None:
        too_large.extend(large)
    if not paths:
        return []
    patterns = "".join(f"/{p}\n" for p in paths).encode("utf-8")
    _git(["sparse-checkout", "set", "--no-cone", "--stdin"], dest, env, stdin=patterns)
    _git(["checkout", "-q", "--detach", ref], dest, env)
    return paths


def _split_missing_blobs(
    paths: list[str], ref: str, dest: Path, env: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Split the paths into (blob present, blob filtered out as too large).

    GIT_NO_LAZY_FETCH keeps `cat-file` from downloading the omitted blob just to look at it
    (git 2.44 and newer; an older git ignores it, and the per-file read cap still applies).
    """
    stdin = "".join(f"{ref}:{p}\n" for p in paths).encode("utf-8")
    out = _git(
        ["cat-file", "--batch-check=%(objectsize)"],
        dest,
        {**env, "GIT_NO_LAZY_FETCH": "1"},
        stdin=stdin,
    )
    kept: list[str] = []
    large: list[str] = []
    for path, line in zip(paths, out.decode("utf-8", "replace").splitlines()):
        (large if line.endswith(" missing") else kept).append(path)
    return kept, large


def read_text(path: Path) -> str | None:
    """The file's text, or None for a non-regular, oversize or non-UTF-8 file. Never follows links."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def frontmatter(text: str) -> dict[str, str]:
    """`name` and `description` from a SKILL.md YAML header, flattened to one line each."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if match:
            key = match.group(1).lower()
            value = match.group(2).strip()
            out[key] = "" if value in ("|", ">", "|-", ">-") else value.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out.get(key, "") + " " + line.strip()).strip()
    return out


_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")


def sanitize_frontmatter(text: str, allowed) -> str | None:
    """Keep only the `allowed` top-level keys of a Markdown YAML header; None if unreadable.

    Claude Code and similar engines honor header keys such as `hooks`, `mcpServers` and
    `permissionMode` in a skill or agent file, so a copied file must never carry them. (Same
    rules as franky/setups.py `sanitize_frontmatter`; a test pins the two together.) A leading BOM
    is dropped, and a first line that starts with `---` but is not exactly `---` is unreadable.
    """
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[0].strip() != "---":
        # `--- x` or `----` opens a header in some parsers: unreadable, never passed through.
        return None if lines[0].startswith("---") else text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    kept: list[str] = []
    current = None
    keep = False
    for line in lines[1:end]:
        if not line.strip():
            if keep:
                kept.append(line)
            continue
        if line.lstrip().startswith("#"):
            continue
        if line[0] in " \t-":
            if current is None:
                return None
            if keep:
                kept.append(line)
            continue
        match = _FM_KEY_RE.match(line)
        if not match:
            return None
        current = match.group(1).lower()
        keep = current in allowed
        if keep:
            kept.append(line)
    return "\n".join(["---", *kept, "---", *lines[end + 1 :]])


def clean_line(value: str, limit: int) -> str:
    flat = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def code_path(value: str, limit: int) -> str:
    """A repository path as an inline code span: no `@path` import or heading resolves in it."""
    return f"`{clean_line(value.replace('`', ''), limit)}`"


def fence_text(text: str) -> str:
    """Wrap repository text in ONE fenced code block so it is inert in the instruction file.

    Claude Code resolves no `@path` import and no heading inside a fenced code block, and
    CommonMark never closes a backtick fence on a shorter backtick run, so the fence is longer
    than the longest backtick run in the text (3 at least). CR and CRLF become LF, NUL goes.
    """
    text = text.replace("\0", "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def map_setup(
    work: Path,
    paths: list[str],
    engine: str,
    home: Path,
    repo: str,
    ref: str,
    too_large: list[str] | None = None,
) -> dict:
    """Copy the fetched text into the engine's native dirs and write the index."""
    skills_dir, instruction, agents_dir = ENGINES[engine]
    total = 0
    texts: dict[str, str] = {}
    skipped: list[str] = list(too_large or [])
    for rel in paths:
        text = read_text(work / rel)
        if text is None:
            skipped.append(rel)
            continue
        total += len(text.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise SetupError(f"repository setup is over the size limit {MAX_TOTAL_BYTES} bytes")
        texts[rel] = text

    # Skill and agent headers are rewritten to an allow-list; an unreadable one is skipped.
    unsafe: set[str] = set()
    for rel in sorted(texts):
        parts = rel.split("/")
        is_skill = parts[-1].lower() == "skill.md" and any(
            _under(rel, r) and len(parts) >= len(r.split("/")) + 2 for r in _skill_roots()
        )
        is_agent = _under(rel, AGENT_ROOT) and rel.endswith(".md")
        if not (is_skill or is_agent):
            continue
        keys = SKILL_FRONTMATTER_KEYS if is_skill else AGENT_FRONTMATTER_KEYS
        clean = sanitize_frontmatter(texts[rel], keys)
        if clean is None:
            unsafe.add(rel)
        else:
            texts[rel] = clean
    for rel in unsafe:
        skipped.append(rel)
        root = next((r for r in _skill_roots() if _under(rel, r)), None)
        if root is not None and rel.rsplit("/", 1)[-1].lower() == "skill.md":
            # Any unreadable SKILL.md, at any depth, drops the whole skill directory.
            base = "/".join(rel.split("/")[: len(root.split("/")) + 1]) + "/"
            for other in [r for r in texts if r.startswith(base)]:
                del texts[other]
        else:
            texts.pop(rel, None)

    # Skills: a directory with a SKILL.md directly under one root. First root in order wins.
    skills: dict[str, tuple[str, str, str]] = {}  # name -> (root, description, path)
    for root in SKILL_ROOTS[engine]:
        for rel in sorted(texts):
            parts = rel.split("/")
            if not rel.startswith(root + "/") or len(parts) != len(root.split("/")) + 2:
                continue
            name = parts[-2]
            if parts[-1] != "SKILL.md" or name in skills or not _SKILL_NAME_RE.match(name):
                continue
            meta = frontmatter(texts[rel])
            desc = clean_line(meta.get("description", ""), DESCRIPTION_CHARS)
            skills[name] = (root, desc, str(home / skills_dir / name / "SKILL.md"))
    for name, (root, _, _) in skills.items():
        dest = home / skills_dir / name
        shutil.rmtree(dest, ignore_errors=True)  # the repo skill overrides a team skill
        for rel, text in texts.items():
            if rel.startswith(f"{root}/{name}/"):
                _write(dest / rel[len(f"{root}/{name}/") :], text)

    agents = []
    if agents_dir is not None:
        for rel, text in sorted(texts.items()):
            if _under(rel, AGENT_ROOT) and rel.endswith(".md"):
                _write(home / agents_dir / rel[len(AGENT_ROOT) + 1 :], text)
                agents.append(rel)

    # Instruction text: the team text already in the instruction file, then the repository's.
    sections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in INSTRUCTION_FILES:
        text = texts.get(rel)
        if text and text.strip() and text.strip() not in seen:
            seen.add(text.strip())
            sections.append((rel, fence_text(text.strip())))
    rules = sorted(r for r in texts if r.endswith(".md") and any(_under(r, p) for p in RULE_ROOTS))
    for rel in rules:
        if texts[rel].strip():
            sections.append((rel, fence_text(texts[rel].strip())))
    if sections:
        target = home / instruction
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        head = existing.rstrip() + "\n\n" if existing.strip() else ""
        body = "\n\n".join(f"### {code_path(rel, 200)}\n\n{text}" for rel, text in sections)
        _write(target, f"{head}## Repository rules (from {repo} at {ref[:12]})\n\n{body}\n")

    instruction_files = [rel for rel, _ in sections if rel in INSTRUCTION_FILES]
    index = render_index(repo, ref, skills, rules, instruction_files)
    if unsafe:
        index += "\n- Skipped (unreadable header): " + ", ".join(
            code_path(r, 200) for r in sorted(unsafe)[:MAX_RULES_LISTED]
        )
    if too_large:
        index += "\n- Skipped (too large): " + ", ".join(
            code_path(r, 200) for r in sorted(too_large)[:MAX_RULES_LISTED]
        )
    index = cap_index(index)
    _write(home / INDEX_DIR / "index.txt", index)
    return {
        "skills": len(skills),
        "rules": len(rules),
        "instructions": len(instruction_files),
        "agents": len(agents),
        "skipped": len(skipped),
        "files": len(texts),
    }


def cap_index(index: str) -> str:
    raw = index.encode("utf-8")
    if len(raw) <= MAX_INDEX_BYTES:
        return index
    return raw[: MAX_INDEX_BYTES - 16].decode("utf-8", "ignore").rstrip() + "\n- ... (cut)"


def render_index(repo, ref, skills, rules, instruction_files) -> str:
    head = f"Repository setup Franky installed (read from {repo} at {ref[:12]}, the default-branch commit):"
    if not skills and not rules and not instruction_files:
        return head + " none found."
    lines = [head]
    if instruction_files:
        lines.append(
            "- Instruction files: "
            + ", ".join(code_path(f, 200) for f in instruction_files)
            + ' (appended to your instruction file under "Repository rules")'
        )
    if rules:
        shown = [code_path(r, 200) for r in rules[:MAX_RULES_LISTED]]
        more = (
            f", ... and {len(rules) - MAX_RULES_LISTED} more"
            if len(rules) > MAX_RULES_LISTED
            else ""
        )
        lines.append("- Rules (in that same section): " + ", ".join(shown) + more)
    if skills:
        lines.append(f"- Skills ({len(skills)}), installed where your engine looks for them:")
        for name in sorted(skills)[:MAX_SKILLS_LISTED]:
            _, desc, where = skills[name]
            where = clean_line(where, 300)
            lines.append(f"  - {name}: {desc} ({where})" if desc else f"  - {name} ({where})")
        if len(skills) > MAX_SKILLS_LISTED:
            lines.append(f"  - ... and {len(skills) - MAX_SKILLS_LISTED} more")
    return "\n".join(lines)


def cmd_map(args: argparse.Namespace) -> int:
    if not _REPO_RE.match(args.repo) or not _SHA_RE.match(args.ref):
        raise SetupError("invalid --repo or --ref")
    if args.engine not in ENGINES:
        raise SetupError(f"unsupported engine {args.engine!r}")
    if args.remote.startswith("-"):
        raise SetupError("invalid --remote")
    home = Path(args.home or os.environ.get("HOME", "")).resolve()
    if not home.is_dir():
        raise SetupError("HOME is not a directory")
    remote = args.remote or f"https://github.com/{args.repo}.git"
    _deadline[0] = time.monotonic() + DEADLINE_SECS
    work = Path(tempfile.mkdtemp(prefix="franky-repo-setup-"))
    too_large: list[str] = []
    try:
        paths = fetch_setup(remote, args.ref, work, home, too_large)
        summary = map_setup(work, paths, args.engine, home, args.repo, args.ref, too_large)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(summary, sort_keys=True))
    return 0


def cmd_exec(marker: str, argv: list[str]) -> int:
    if not argv:
        raise SetupError("exec needs a command")
    if not (marker.startswith(MARKER) and len(marker) >= len(MARKER) + 16):
        raise SetupError("exec needs the per-run --marker")
    home = Path(os.environ.get("HOME", ""))
    try:
        index = (home / INDEX_DIR / "index.txt").read_text(encoding="utf-8")
    except OSError:
        index = NO_INDEX
    swapped = []
    done = False
    for arg in argv:
        # Once only: the prompt may quote untrusted text, and the token is not in it.
        if not done and marker in arg:
            arg, done = arg.replace(marker, index, 1), True
        swapped.append(arg)
    try:
        os.execvp(argv[0], swapped)
    except OSError as exc:
        raise SetupError(f"could not start the engine: {exc.__class__.__name__}") from exc
    return 1  # unreachable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="franky-repo-setup")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mapper = sub.add_parser("map")
    mapper.add_argument("--repo", required=True)
    mapper.add_argument("--ref", required=True)
    mapper.add_argument("--engine", required=True)
    mapper.add_argument("--home", default="")
    mapper.add_argument("--remote", default="", help=argparse.SUPPRESS)
    sub.add_parser("exec", help="exec --marker M -- <argv...>: swap the index marker, then exec")
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:1] == ["exec"]:
            # Parsed by hand: argparse would swallow the engine's own flags.
            if argv[1:2] != ["--marker"] or argv[3:4] != ["--"]:
                raise SetupError("usage: exec --marker <token> -- <argv...>")
            return cmd_exec(argv[2], argv[4:])
        return cmd_map(parser.parse_args(argv))
    except SetupError as exc:
        print(f"franky-repo-setup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
