"""Per-PR thread store for `review-pr --thread`, `build --thread`, `iterate --thread` and
`franky threads`.

WHY this exists: a team wants one AI conversation per pull request and role, resumed on the next
push or after a crash, so a re-review verifies its own earlier findings and a follow-up author
pass continues the session that wrote the PR instead of starting cold. For each (repo, PR, role)
the store keeps a small record, a bounded handoff (the last review's findings, no bodies; for the
author role only the PR head) and, for an engine with native resume, that engine's own session
files. The reviewer and author roles never share a session.

AUTHOR ROLE: a `build --thread` session is handed to the author thread once its PR exists
(`begin_run` with the build's `session_id`, the sidecar extracted by `extract_session`, then the
normal `finish_run` commit). Author runs resume at most MAX_AUTHOR_RESUMES times in a row
(`resumes` in the record, counted before launch); the next run re-seeds.

LAYOUT: <root>/<owner>__<repo>__<pr>__<role>/ (lowercase) holds `record.json` (0600),
`session/` (0700, a HOME-relative tree holding exactly one session:
`.claude/projects/-work/<id>.jsonl` plus its optional `<id>/` side dir) and `lock`. The root is
~/.franky/threads, overridden by FRANKY_THREADS_DIR (mirrors jobs.RUNS_DIR_VAR).

SECRET-SAFETY: session bytes move only by stream-in (tar over `docker exec -i`) and copy-out
(a `docker cp ... -` tar stream), never a mount. Only the session file and its side dir move in
either direction - never the rest of the engine's project dir (Claude's project memory there
would let one prompt-injected run plant trusted memory for the next). A copy-out is extracted as
regular files and directories only, then scrubbed and fail-closed verified with snapshot.py's
own scrub/verify before it replaces the stored session. The handoff is redacted (known values and
token patterns) before it is written. Nothing here is ever exported.

INVARIANT: `session_ok` is true exactly when `session/` holds `<session_id>.jsonl` for a
native-resume engine. Every path that marks a session unusable also deletes it, and
`open_thread` re-derives the flag from disk after a crash.

CONCURRENCY: every writer (a review run, prune, purge) holds an exclusive non-blocking flock on
the thread's `lock`. A review refuses a busy thread (`thread_busy`); prune and purge skip it.

CRASH-SAFETY: `begin_run` writes the session id before the container starts. A new session is
staged as `session.new/` and swapped in by renames; `open_thread` repairs an interrupted swap.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import jobs, snapshot
from .config import redact
from .engine import ENGINES
from .github import run_gh
from .profile import _SECRET_PATTERNS
from .result import TaskRejected

THREADS_DIR_VAR = "FRANKY_THREADS_DIR"
ROLES = ("reviewer", "author")
SCHEMA = 1
MAX_AGE = timedelta(days=14)
MAX_SESSION_BYTES = 64 * 1024**2
MAX_AUTHOR_RESUMES = 10
HANDOFF_MAX_FINDINGS = 40
ORPHAN_SECS = 3600
_GRAPHQL_BATCH = 50

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,9})")
_ID_RE = re.compile(r"[A-Za-z0-9_.-]+__[1-9][0-9]*__(?:reviewer|author)")
_SESSION_DIRS = ("session", "session.new", "session.old")
# Every temp file or dir the store creates inside a thread dir; swept by open_thread and purge.
_TMP_PREFIX = ".tmp-"
_SEVERITY_ORDER = {"blocking": 0, "normal": 1}

# Engine error lines meaning the stored session is unusable. Matched only on a failed run and
# only on non-JSON lines, so PR content that a tool echoed into the event stream cannot match.
_RESUME_FAILED_RE = re.compile(r"No conversation found with session ID")
_FLAGS_UNSUPPORTED_RE = re.compile(r"unknown option '?--(?:session-id|resume)")
# The verify step's token patterns, extended over the rest of the token for replacement.
_HANDOFF_PATTERNS = [re.compile(pattern.pattern + r"\S*") for _desc, pattern in _SECRET_PATTERNS]


def threads_dir(env: Mapping[str, str] | None = None) -> Path:
    """Root of the thread store. FRANKY_THREADS_DIR overrides; absent -> ~/.franky/threads."""
    env = os.environ if env is None else env
    override = env.get(THREADS_DIR_VAR)
    return Path(override) if override else Path.home() / ".franky" / "threads"


def thread_id(repo: str, pr: int, role: str) -> str:
    """`<owner>__<repo>__<pr>__<role>`, lowercase (GitHub names are case-insensitive), validated
    so it is always one safe path component."""
    owner, _, name = repo.lower().partition("/")
    if not (_NAME_RE.fullmatch(owner) and _NAME_RE.fullmatch(name)) or {owner, name} & {
        ".",
        "..",
    }:
        raise ValueError(f"invalid thread repository: {repo!r}")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise ValueError(f"invalid thread PR number: {pr!r}")
    if role not in ROLES:
        raise ValueError(f"invalid thread role: {role!r}")
    return f"{owner}__{name}__{pr}__{role}"


def parse_ref(ref: str) -> tuple[str, int]:
    """Parse `owner/repo#N` into (lowercase repo, N). Raises ValueError on anything else."""
    match = _REF_RE.fullmatch(ref or "")
    if not match:
        raise ValueError(f"invalid thread reference {ref!r} (expected OWNER/REPO#N)")
    thread_id(match[1], int(match[2]), ROLES[0])  # same validation as the stored id
    return match[1].lower(), int(match[2])


def session_paths(engine: str, session_id: str | None) -> list[str]:
    """HOME-relative paths of one engine session: the session file, then its optional side dir.
    Empty for an engine without native resume."""
    cls = ENGINES.get(engine)
    base = cls.session_dir if cls else ""
    if not base or not session_id:
        return []
    return [f"{base}/{session_id}.jsonl", f"{base}/{session_id}"]


@dataclass
class Thread:
    """An open, locked thread. The CLI holds it for the whole run and then calls close()."""

    id: str
    path: Path
    fd: int | None = None

    @property
    def session_dir(self) -> Path:
        return self.path / "session"

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _remove(path: Path) -> None:
    """Delete a file or a directory tree without following symlinks. Never raises."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _lock(path: Path) -> int | None:
    """Exclusive non-blocking lock on `<path>/lock`; None when another process holds it.

    A lock file that a concurrent purge already unlinked also reads as busy."""
    try:
        fd = os.open(path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(fd).st_nlink:
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _session_present(path: Path, record: dict) -> bool:
    paths = session_paths(record.get("engine"), record.get("session_id"))
    return bool(paths) and (path / "session" / paths[0]).is_file()


def _recover(path: Path) -> None:
    """Repair an interrupted commit, then re-derive `session_ok` from disk.

    A swap stopped halfway restores `session.old/`; staging leftovers are dropped. A crash after
    the swap but before the record update leaves a stored session the record calls not ok (or
    the reverse), so the flag is set to whether `<session_id>.jsonl` is actually present."""
    current, old = path / "session", path / "session.old"
    if old.is_dir() and not current.exists():
        os.rename(old, current)
    for leftover in (path / "session.new", old, *path.glob(f"{_TMP_PREFIX}*")):
        _remove(leftover)
    record = read_record(path)
    if record is not None and bool(record.get("session_ok")) != _session_present(path, record):
        record["session_ok"] = not record.get("session_ok")
        jobs._atomic_write(path / "record.json", record, prefix=_TMP_PREFIX)


def open_thread(repo: str, pr: int, role: str, env: Mapping[str, str] | None = None) -> Thread:
    """Create (0700) and lock one thread. Raises TaskRejected(kind="thread_busy") when another
    run, prune or purge holds it, and ValueError on an invalid id."""
    tid = thread_id(repo, pr, role)
    root = threads_dir(env)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / tid
    path.mkdir(mode=0o700, exist_ok=True)
    fd = _lock(path)
    if fd is None:
        raise TaskRejected(
            f"thread {tid} is busy (another run, prune or purge holds it) - refusing",
            kind="thread_busy",
        )
    _recover(path)
    return Thread(tid, path, fd)


def read_record(path: Path) -> dict | None:
    """The thread record in `path`, or None when missing or malformed. Never raises."""
    try:
        data = json.loads((path / "record.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def write_record(thread: Thread, record: dict) -> bool:
    """Atomically write the record (0600). Best-effort: False on failure."""
    return jobs._atomic_write(thread.path / "record.json", record, prefix=_TMP_PREFIX)


def _tree_bytes(root: Path) -> int:
    """Total size of the regular files under `root` (or `root` itself); links never followed."""
    try:
        info = os.lstat(root)
    except OSError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def session_bytes(thread: Thread) -> int:
    return _tree_bytes(thread.session_dir)


def _age(record: dict, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(record.get("updated_at") or "")
    except (TypeError, ValueError):
        return None


def plan_run(
    record: dict | None,
    *,
    engine: str,
    native: bool,
    model: str | None,
    rubric: str,
    session_bytes: int,
    now: datetime,
    role: str = "reviewer",
) -> tuple[str, str]:
    """Decide how this run starts: ("resumed" | "seeded" | "fresh", reason).

    resumed: the stored session is usable as-is (same engine/model/rubric, native resume, marked
    ok, non-empty, within the size cap, touched within 14 days, and for the author role fewer
    than MAX_AUTHOR_RESUMES resumes in a row). seeded: a record with a handoff exists but some
    condition fails; the prompt carries the handoff into a new session. fresh: nothing to carry.
    Pure."""
    if record is None:
        return "fresh", "new_thread"
    age = _age(record, now)
    if record.get("engine") != engine:
        reason = f"engine_changed:{record.get('engine')}->{engine}"
    elif record.get("model") != model:
        reason = "model_changed"
    elif record.get("rubric_version") != rubric:
        reason = "rubric_changed"
    elif not native:
        reason = "no_native_resume"
    elif not (record.get("session_id") and record.get("session_ok")):
        reason = "session_not_ok"
    elif session_bytes == 0:
        reason = "no_session"
    elif session_bytes > MAX_SESSION_BYTES:
        reason = "too_large"
    elif age is None or age >= MAX_AGE:
        reason = "stale"
    elif role == "author" and record.get("resumes", 0) >= MAX_AUTHOR_RESUMES:
        reason = "resume_cap"
    else:
        return "resumed", ""
    return seed_mode(record), reason


def seed_mode(record: dict | None) -> str:
    """How a run that cannot resume starts: seeded when there is a handoff, else fresh."""
    return "seeded" if record and record.get("handoff") else "fresh"


def begin_run(
    thread: Thread,
    record: dict | None,
    *,
    repo: str,
    pr: int,
    role: str,
    engine: str,
    native: bool,
    model: str | None,
    rubric: str,
    mode: str,
    job_id: str,
    now: datetime,
    session_id: str | None = None,
) -> tuple[dict, bool]:
    """Pin this run's session id in record.json BEFORE the container starts. Returns (record,
    written); the caller runs without session flags when the write failed.

    A crash at any later point leaves a known id. A non-resumed run never uses the old session,
    so it is deleted here; the handoff and last_sha carry over. `updated_at` moves only on a
    successful finish. `session_id` pins a known id for a non-resumed run (a `build --thread`
    session being bound) instead of minting one. An author record counts `resumes` in a row,
    incremented here so a crashed resumed run counts too."""
    if mode != "resumed":
        _remove(thread.session_dir)
    stamp = now.isoformat(timespec="seconds")
    prior = record or {}
    if mode == "resumed":
        session_id = prior.get("session_id")
    elif session_id is None:
        session_id = str(uuid.uuid4()) if native else None
    new = {
        "schema": SCHEMA,
        "repo": repo.lower(),
        "pr": pr,
        "role": role,
        "engine": engine,
        "model": model,
        "rubric_version": rubric,
        "session_id": session_id,
        "session_ok": mode == "resumed",
        "last_sha": prior.get("last_sha"),
        "last_job_id": job_id,
        "handoff": prior.get("handoff"),
        "created_at": prior.get("created_at") or stamp,
        "updated_at": prior.get("updated_at") or stamp,
    }
    if role == "author":
        new["resumes"] = prior.get("resumes", 0) + 1 if mode == "resumed" else 0
    return new, write_record(thread, new)


def new_incoming(thread: Thread) -> Path:
    """A 0700 copy-out landing dir inside the thread dir (same filesystem as session/)."""
    return Path(tempfile.mkdtemp(prefix=f"{_TMP_PREFIX}incoming-", dir=thread.path))


def session_tar(thread: Thread, paths: list[str]) -> Path | None:
    """Pack only `paths` (the session file and its side dir) from session/ into a 0600 gzip tar
    inside the thread dir: regular files, relative names, no links. None when the session file
    is missing or packing fails."""
    root = thread.session_dir
    if not paths or not (root / paths[0]).is_file() or (root / paths[0]).is_symlink():
        return None
    fd, name = tempfile.mkstemp(prefix=f"{_TMP_PREFIX}session-", suffix=".tar.gz", dir=thread.path)
    os.close(fd)
    try:
        with tarfile.open(name, "w:gz") as tar:
            for rel in paths:
                top = root / rel
                if top.is_symlink():
                    continue
                files = [top] if top.is_file() else []
                if top.is_dir():
                    files = sorted(p for p in snapshot._tree_paths(top) if p.is_file())
                for path in files:
                    if path.is_symlink():
                        continue
                    with path.open("rb") as stream:
                        snapshot._add_file(tar, str(path.relative_to(root)), stream)
    except (OSError, ValueError):
        Path(name).unlink(missing_ok=True)
        return None
    return Path(name)


def extract_session(archive: Path, dest: Path, paths: list[str]) -> bool:
    """Extract only `paths` (the session file and its side dir) from a gzip session sidecar into
    `dest`: regular files and directories only (snapshot._extract_plain), so an adopted sidecar
    takes the same regular-file-only path as a copy-out. False on a corrupt archive, more than
    MAX_SESSION_BYTES of files, or a missing session file; the caller then commits nothing."""
    if not paths:
        return False
    try:
        with gzip.open(archive, "rb") as stream:
            snapshot._extract_plain(stream, dest, only=paths, max_bytes=MAX_SESSION_BYTES)
    except (OSError, EOFError, ValueError, tarfile.TarError):
        return False
    target = dest / paths[0]
    return target.is_file() and not target.is_symlink()


def _sanitize(root: Path) -> None:
    """Defense in depth after the tar filter: keep only directories (0700) and regular files
    (0600). Anything else is deleted; os.walk never follows a symlink out of the tree."""
    for dirpath, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = os.path.join(dirpath, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(path, 0o700)
            elif stat.S_ISREG(mode):
                os.chmod(path, 0o600)
            else:
                os.unlink(path)
                if name in dirs:
                    dirs.remove(name)


def commit_session(
    thread: Thread, incoming: Path, secrets, runner=subprocess.run
) -> tuple[bool, str]:
    """Verify a copied-out session and swap it in. Returns (stored, reason).

    Sanitize, then scrub + fail-closed verify with the run's secrets. Any finding discards the
    copy. Success stages `session.new/`, renames session/ to `session.old/`, `session.new/` to
    session/, then removes `session.old/`. `incoming` is always removed."""
    secrets = list(secrets)
    try:
        _sanitize(incoming)
        size = _tree_bytes(incoming)
        if size == 0:
            return False, "no_session"
        if size > MAX_SESSION_BYTES:
            return False, "too_large"
        snapshot.scrub_workspace(incoming, secrets)
        if snapshot.verify_no_secrets(incoming, secrets, runner=runner):
            return False, "verify_failed"
        staged, current, old = (
            thread.path / "session.new",
            thread.session_dir,
            thread.path / "session.old",
        )
        _remove(staged)
        os.rename(incoming, staged)
        if current.exists():
            os.rename(current, old)
        os.rename(staged, current)
        _remove(old)
        return True, ""
    except (OSError, ValueError):
        return False, "commit_failed"
    finally:
        _remove(incoming)


def _clean_text(value, limit: int, secrets) -> str | None:
    """One line, known secret values and token patterns redacted, at most `limit` chars."""
    if not isinstance(value, str):
        return None
    text = redact(value, secrets)
    for pattern in _HANDOFF_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return " ".join(text.split())[:limit]


def build_handoff(shaped: dict, sha: str, secrets) -> dict:
    """The bounded, redacted summary a later run is seeded with: no finding bodies, resolved
    findings dropped, blocking first, at most HANDOFF_MAX_FINDINGS entries, text on one line."""
    kept = [f for f in shaped.get("findings", []) if f.get("status") != "resolved"]
    kept.sort(key=lambda f: _SEVERITY_ORDER.get(f.get("severity"), 2))  # stable
    return {
        "schema": SCHEMA,
        "sha": sha,
        "summary": _clean_text(shaped.get("summary") or "", 500, secrets),
        "findings": [
            {
                "title": _clean_text(f.get("title"), 200, secrets),
                "file": _clean_text(f.get("file"), 200, secrets),
                "line": f.get("line"),
                "severity": f.get("severity"),
                "status": f.get("status", "new"),
            }
            for f in kept[:HANDOFF_MAX_FINDINGS]
        ],
    }


def _error_lines(tail: str) -> str:
    """The non-JSON lines of an output tail (engine and CLI errors, not streamed events)."""
    return "\n".join(line for line in tail.splitlines() if not line.lstrip().startswith(("{", "[")))


def _is_event(line: str) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def startup_rejected(tail: str, *, truncated: bool = False) -> bool:
    """True when the engine refused its session flags at startup (the stored session is unknown,
    or the CLI lacks the flags). Nothing ran, so an author run may safely retry; any other
    failure of a write pass never re-runs.

    A real startup rejection exits before the engine emits any event, so a tail holding a line
    that parses as a JSON event is never a rejection, and the patterns match only non-JSON
    lines. `truncated` (the tail is cut from a longer output) drops its first, partial line, so
    a fragment of an event cannot pass as an error line."""
    lines = tail.splitlines()[1:] if truncated else tail.splitlines()
    if any(line.lstrip().startswith(("{", "[")) and _is_event(line) for line in lines):
        return False
    errors = _error_lines("\n".join(lines))
    return bool(_RESUME_FAILED_RE.search(errors) or _FLAGS_UNSUPPORTED_RE.search(errors))


def _drop_session(thread: Thread, record: dict) -> None:
    record["session_ok"] = False
    _remove(thread.session_dir)


def finish_run(
    thread: Thread,
    record: dict,
    *,
    mode: str,
    code: int,
    shaped: dict | None,
    sha: str,
    tail: str,
    incoming: Path | None,
    copy_status: str | None,
    secrets,
    now: datetime,
    timeout_code: int = 124,
    runner=subprocess.run,
) -> tuple[dict, str]:
    """Settle a run and write the record. Returns (record, session_reason label or "").

    Only a clean exit with parsed findings commits the session and the new handoff and moves
    last_sha/updated_at; otherwise the previous handoff stays. A failed resumed run (any code but
    0 or a timeout) drops its session so the next run seeds; engine error lines only label why.
    A stored session that is too large or fails verification is dropped too, so the next run
    seeds a compact session."""
    reason = ""
    if code not in (0, timeout_code):
        errors = _error_lines(tail)
        if _FLAGS_UNSUPPORTED_RE.search(errors):
            reason = "engine_flags_unsupported"
        elif mode == "resumed" and _RESUME_FAILED_RE.search(errors):
            reason = "resume_failed"
        if mode == "resumed" or reason:
            _drop_session(thread, record)
    if code == 0 and shaped is not None:
        if incoming is not None:
            if copy_status == "ok":
                stored, why = commit_session(thread, incoming, secrets, runner)
            else:
                stored, why = False, "too_large" if copy_status == "too_large" else "copy_failed"
            if stored:
                record["session_ok"] = True
            else:
                print(
                    f"franky: thread {thread.id}: session not stored reason={why}", file=sys.stderr
                )
                if why in ("too_large", "verify_failed"):
                    _drop_session(thread, record)
        record["handoff"] = build_handoff(shaped, sha, secrets)
        record["last_sha"] = sha
        record["updated_at"] = now.isoformat(timespec="seconds")
    if incoming is not None:
        _remove(incoming)
    if not write_record(thread, record):
        print(
            f"franky: thread {thread.id}: record not updated reason=write_failed", file=sys.stderr
        )
    return record, reason


# ---------------------------------------------------------------------------
# list / prune / purge
# ---------------------------------------------------------------------------


def _thread_dirs(env: Mapping[str, str] | None) -> list[Path]:
    try:
        entries = sorted(threads_dir(env).iterdir())
    except OSError:
        return []
    return [p for p in entries if _ID_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]


def list_threads(env: Mapping[str, str] | None = None) -> list[dict]:
    """Every readable record plus its thread id and stored session bytes."""
    out = []
    for path in _thread_dirs(env):
        record = read_record(path)
        if record is not None:
            out.append(
                {**record, "thread": path.name, "session_bytes": _tree_bytes(path / "session")}
            )
    return out


def _leftovers(path: Path) -> list[Path]:
    return [path / "session.new", path / "session.old", *path.glob(f"{_TMP_PREFIX}*")]


def _purge_dir(path: Path) -> int:
    """Delete one thread in a fixed order: sessions, temp files, record, lock, dir. Returns the
    session bytes freed. The caller holds the lock (unlinking a locked file is fine)."""
    freed = sum(_tree_bytes(p) for p in (path / "session", *_leftovers(path)))
    for leftover in (path / "session", *_leftovers(path)):
        _remove(leftover)
    (path / "record.json").unlink(missing_ok=True)
    (path / "lock").unlink(missing_ok=True)
    try:
        path.rmdir()
    except OSError:
        pass
    return freed


def _sweep(path: Path, now: datetime) -> int:
    """Under the lock: delete staging and temp leftovers older than ORPHAN_SECS. Returns the
    bytes of the younger leftovers that remain."""
    remaining = 0
    for leftover in _leftovers(path):
        try:
            old = now.timestamp() - os.lstat(leftover).st_mtime > ORPHAN_SECS
        except OSError:
            continue
        if old:
            _remove(leftover)
        else:
            remaining += _tree_bytes(leftover)
    return remaining


def _locked(path: Path, action):
    """Run `action(path)` under the thread lock; None when the thread is busy."""
    fd = _lock(path)
    if fd is None:
        return None
    try:
        return action(path)
    finally:
        os.close(fd)


def _pr_states(entries, env, gh, warn) -> dict:
    """{(repo, pr): state} from one batched read-only GraphQL query per 50 threads. Any error or
    missing alias leaves that PR out (kept). No usable gh skips the whole pass."""
    keys = sorted({(r["repo"], r["pr"]) for _path, r in entries})
    states: dict = {}
    for start in range(0, len(keys), _GRAPHQL_BATCH):
        batch = keys[start : start + _GRAPHQL_BATCH]
        fields = " ".join(
            f't{i}: repository(owner:"{repo.split("/")[0]}",name:"{repo.split("/")[1]}")'
            f"{{pullRequest(number:{pr}){{state}}}}"
            for i, (repo, pr) in enumerate(batch)
        )
        try:
            _code, out, _err = gh(
                ["api", "graphql", "-f", f"query=query{{{fields}}}"], env, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired):
            warn("franky: threads prune: gh is unavailable - skipping the --closed pass")
            return {}
        # Parse even on a nonzero exit: gh prints partial data with errors, and only an explicit
        # MERGED/CLOSED ever purges.
        try:
            data = json.loads(out).get("data") or {}
        except (ValueError, AttributeError):
            continue
        for i, key in enumerate(batch):
            node = data.get(f"t{i}") if isinstance(data, dict) else None
            pull = node.get("pullRequest") if isinstance(node, dict) else None
            state = pull.get("state") if isinstance(pull, dict) else None
            if isinstance(state, str):
                states[key] = state
    return states


def prune(
    env: Mapping[str, str] | None = None,
    *,
    closed: bool = False,
    older_than_days: int = 30,
    max_bytes: int = 2 * 1024**3,
    repo: str | None = None,
    now: datetime | None = None,
    gh=None,
    warn=lambda message: print(message, file=sys.stderr),
) -> dict:
    """Bound the store. Returns {"purged": [{thread, reason, bytes}], "kept": n, "bytes": total,
    "disk_skipped": bool}.

    Passes, in order: orphans (no record, dir older than 1h), closed PRs (--closed), idle threads
    (updated_at older than `older_than_days`), a sweep of staging/temp leftovers older than 1h in
    each remaining thread, then the disk cap (delete session/ dirs oldest first while session
    plus leftover bytes exceed `max_bytes`, keeping record and handoff). Every deletion happens
    under the thread lock and skips a busy thread. `kept` and `bytes` (bytes left) cover the
    threads considered.

    `repo` limits every pass to that repository's threads, so one repository-scoped read token
    covers the whole GraphQL query. A per-repo view cannot judge the global cap, so the disk pass
    is skipped (`disk_skipped`). Raises ValueError on an invalid `repo`."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    gh = gh or run_gh
    orphan_re = None
    if repo is not None:
        prefix = thread_id(repo, 1, ROLES[0]).rsplit("__", 2)[0]
        orphan_re = re.compile(re.escape(prefix) + r"__[1-9][0-9]*__(?:reviewer|author)")
        repo = repo.lower()
    purged: list[dict] = []
    entries = []
    for path in _thread_dirs(env):
        record = read_record(path)
        if record is not None:
            try:
                valid = (
                    thread_id(record.get("repo", ""), record.get("pr"), record.get("role"))
                    == path.name
                )
            except ValueError:
                valid = False
            if valid and repo in (None, record["repo"].lower()):
                entries.append((path, record))
            continue
        if orphan_re is not None and not orphan_re.fullmatch(path.name):
            continue
        try:
            orphan = now.timestamp() - path.stat().st_mtime > ORPHAN_SECS
        except OSError:
            orphan = False
        if orphan:
            done = _locked(
                path,
                lambda p: (
                    None
                    if read_record(p)
                    else {"thread": p.name, "reason": "orphan", "bytes": _purge_dir(p)}
                ),
            )
            if done:
                purged.append(done)

    def purge_if(path, reason, still):
        def action(p):
            record = read_record(p)
            if record is None or not still(record):
                return None
            return {"thread": p.name, "reason": reason, "bytes": _purge_dir(p)}

        done = _locked(path, action)
        if done:
            purged.append(done)
        return done is not None

    states = _pr_states(entries, env, gh, warn) if closed and entries else {}
    limit = timedelta(days=older_than_days)

    def is_closed(record):
        return states.get((record["repo"], record["pr"])) in ("MERGED", "CLOSED")

    def is_idle(record):
        age = _age(record, now)
        return age is not None and age > limit

    kept = []
    for path, record in entries:
        if is_closed(record) and purge_if(path, "closed", is_closed):
            continue
        if is_idle(record) and purge_if(path, "idle", is_idle):
            continue
        kept.append((path, record))
    entries = kept

    total = 0
    for path, _record in entries:
        extra = _locked(path, lambda p: _sweep(p, now))
        if extra is None:  # busy: count what is there, delete nothing
            extra = sum(_tree_bytes(p) for p in _leftovers(path))
        total += _tree_bytes(path / "session") + extra
    for path, _record in sorted(entries, key=lambda e: e[1].get("updated_at") or ""):
        if repo is not None or total <= max_bytes:
            break

        def drop_session(p):
            freed = _tree_bytes(p / "session")
            _remove(p / "session")
            return {"thread": p.name, "reason": "disk", "bytes": freed}

        done = _locked(path, drop_session)
        if done and done["bytes"]:
            purged.append(done)
            total -= done["bytes"]
    return {
        "purged": purged,
        "kept": len(entries),
        "bytes": total,
        "disk_skipped": repo is not None,
    }


def purge(
    env: Mapping[str, str] | None = None, *, ref: str | None = None, role: str | None = None
) -> dict:
    """Purge one PR's threads (both roles unless `role`), or every thread when `ref` is None.
    Returns {"purged": [{thread, reason: "manual", bytes}], "busy": [thread ids skipped]}."""
    if ref is None:
        paths = _thread_dirs(env)
    else:
        repo, pr = parse_ref(ref)
        roles = (role,) if role else ROLES
        paths = [threads_dir(env) / thread_id(repo, pr, r) for r in roles]
        paths = [p for p in paths if p.is_dir() and not p.is_symlink()]
    purged, busy = [], []
    for path in paths:
        done = _locked(
            path, lambda p: {"thread": p.name, "reason": "manual", "bytes": _purge_dir(p)}
        )
        if done is None:
            busy.append(path.name)
        else:
            purged.append(done)
    return {"purged": purged, "busy": busy}
