"""Workspace snapshot + restore for `franky job resume` (issue #71).

WHY this exists: a hung/timed-out/killed run throws away everything the agent did inside the
container's disposable `/work` (the repo clone + its branch state). `franky job resume` lets a fresh
engine CONTINUE that work instead of restarting from scratch. To do that we must capture `/work`
off the container before it is reaped, store it host-side, and later restore it into a freshly
launched (still fully hardened) container.

WHY it does NOT touch the container hardening: the snapshot is taken with host-side
`docker cp`/`docker exec` only - no bind mount, no host docker socket, no relaxation of
`_HARDENING`. The restore does NOT `docker cp` the tar back IN (the daemon refuses a `cp` into a
`--read-only` container, even to a writable target); instead it pipes the tar to `tar -xzf -` over
`docker exec -i` stdin, extracted as the run uid (1001) which owns `/work`, and the container
waits for a marker file (an env flag) before running the engine. The safety boundary is unchanged.

SECRET-SAFETY is the crux, because a workspace snapshot could otherwise leak the creds the agent
carried. Two layers, both fail-closed:
  1. `scrub_workspace` removes known cred FILES the agent might have copied into `/work`, strips
     tokenized userinfo + credential helpers out of every `.git/config`, and redacts known secret
     VALUES out of every non-`.git` file.
  2. `verify_no_secrets` then SCANS the scrubbed tree (including git object content, which is
     zlib-compressed and so invisible to a plain byte scan - we decompress it via
     `git cat-file`). ANY surviving known value, any freshly-minted-token pattern, or any failure
     to run the git verification at all => the snapshot is refused (no file written). We would
     rather lose the ability to resume than store an unverified workspace.

Nothing here raises into a run: the orchestrators wrap everything and degrade to False/None so a
snapshot failure can never change a run's outcome or wedge teardown. STDLIB-only apart from the
injected `runner` (default subprocess.run) so the pure argv builders + scrub/verify logic stay
trivially unit-testable without real docker.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from . import jobs
from .profile import CONTAINER_HOME, scan_for_secrets

# The container-side marker the host touches (via `docker exec`) once the workspace tar has been
# copied in and extracted; the entrypoint waits for it before exec-ing the engine (see
# franky-dind-entrypoint.sh). Living under /work means it lands on the writable volume.
SNAPSHOT_MARKER = ".franky-resume-ready"

# Env flag that puts the entrypoint into resume-wait mode (by-value, non-secret). build_docker_argv
# passes `-e FRANKY_RESUME_WAIT=1` when resuming; the entrypoint unsets it after the marker lands.
RESUME_WAIT_ENV = "FRANKY_RESUME_WAIT"

# Session hold (see franky-dind-entrypoint.sh): with SESSION_HOLD_ENV=<nonce> the entrypoint
# prints the line `SESSION_HOLD_LINE <nonce>` after a clean engine exit and keeps the --rm
# container alive (capped) until the host has copied the session out and touched
# `SESSION_COPIED_MARKER-<nonce>`. The nonce keeps engine output that merely quotes the phrase
# (a review of this very file) from starting a copy mid-run. /tmp is a writable volume.
SESSION_HOLD_ENV = "FRANKY_SESSION_HOLD"
SESSION_HOLD_LINE = "franky: engine exited, holding for session copy"
SESSION_COPIED_MARKER = "/tmp/.franky-session-copied"

# Credential FILES that must never live inside a stored snapshot. HOME cred files are not
# snapshotted (only /work is), but the autonomous agent COULD copy one into /work - so we delete
# these defensively wherever they appear in the tree. `*.pem` (private keys) is handled separately
# by suffix.
_CRED_FILENAMES = {
    ".git-credentials",
    ".netrc",
    "hosts.yml",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "id_ecdsa",
}

# Strips URL userinfo from any line: `https://x-access-token:TOKEN@github.com` -> `https://github.com`.
# `[^/@\s]*` stops at the first `/`, `@`, or whitespace so only the authority's userinfo is removed.
_GIT_URL_USERINFO_RE = re.compile(rb"://[^/@\s]*@")

# The bytes we substitute a matched secret VALUE with when redacting non-.git files in the tree.
_REDACTED = b"[REDACTED]"

# Prefix of the temp tar finalize_snapshot packs beside its destination; jobs.prune sweeps
# stale `.tmp-` leftovers in the runs dir.
TMP_PACK_PREFIX = ".tmp-pack-"

_CHUNK_BYTES = 64 * 1024
_MAX_SCAN_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 100_000
_MAX_FILE_BYTES = 4 * 1024**3
_MAX_TREE_BYTES = 16 * 1024**3

# Set the native output-file limit in a fresh process; preexec_fn is unsafe with host threads.
_GIT_LIMITED_EXEC = (
    "import os,resource,sys; cap=int(sys.argv[1]); "
    "resource.setrlimit(resource.RLIMIT_FSIZE,(cap,cap)); "
    "os.execvp('git',['git',*sys.argv[2:]])"
)


def _tree_paths(root: Path):
    """Bound traversal and refuse unreadable trees; never follow symbolic links."""
    count = total = 0

    def walk(directory, depth):
        nonlocal count, total
        if depth > 128:
            raise ValueError("snapshot directory depth exceeded")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise ValueError("snapshot entry limit exceeded")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    continue
                path = Path(entry.path)
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if info.st_size > _MAX_FILE_BYTES or total > _MAX_TREE_BYTES:
                        raise ValueError("snapshot size limit exceeded")
                    yield path
                elif stat.S_ISDIR(info.st_mode):
                    yield path
                    yield from walk(path, depth + 1)

    yield from walk(root, 0)


def _contains_values(stream, values):
    overlap = max((len(value) for value in values), default=1) - 1
    if overlap > _MAX_SCAN_BYTES:
        raise ValueError("snapshot secret length exceeded")
    pending = b""
    total = 0
    while chunk := stream.read(_CHUNK_BYTES):
        total += len(chunk)
        if total > _MAX_TREE_BYTES:
            raise ValueError("snapshot object size exceeded")
        data = pending + chunk
        if any(value in data for value in values):
            return True
        pending = data[-overlap:] if overlap else b""
    return False


def stream_contains_values(stream, secrets) -> bool:
    """True iff the byte stream holds any non-empty secret VALUE (exact match, chunk-safe).

    Values only: pattern scanning of an exported diff is the caller's job. Reads the stream to
    the end unless a value is found first."""
    return _contains_values(stream, [s.encode("utf-8") for s in secrets if s])


def _has_pattern(stream):
    # All current patterns are line-local except NAME whitespace = whitespace VALUE.
    # Three nonempty lines cover that assignment, including intervening blank lines.
    # ponytail: refuse windows over 4 MiB; use an incremental regex parser if needed.
    lines = deque()
    size = 0
    nonempty = 0
    while line := stream.readline(_MAX_SCAN_BYTES + 1):
        size += len(line)
        if size > _MAX_SCAN_BYTES or len(lines) >= _MAX_ENTRIES:
            raise ValueError("snapshot pattern window exceeded")
        text = line.decode("utf-8", errors="replace")
        lines.append((text, len(line)))
        if not text.strip():
            continue
        nonempty += 1
        if scan_for_secrets("".join(text for text, _ in lines)):
            return True
        if nonempty == 3:
            while lines:
                old, length = lines.popleft()
                size -= length
                if old.strip():
                    nonempty -= 1
                    break
    return bool(scan_for_secrets("".join(text for text, _ in lines)))


def _redact_file(path, values):
    with path.open("rb") as source:
        if not _contains_values(source, values):
            return
    # One bounded disk pass per value preserves longest-first bytes.replace semantics.
    for value in values:
        if len(value) > _MAX_SCAN_BYTES:
            raise ValueError("snapshot secret length exceeded")
        with path.open("rb") as source, tempfile.TemporaryFile() as output:
            pending = b""
            changed = False
            while chunk := source.read(_CHUNK_BYTES):
                data = pending + chunk
                end = max(0, len(data) - len(value) + 1)
                start = 0
                while (index := data.find(value, start)) >= 0 and index < end:
                    output.write(data[start:index])
                    output.write(_REDACTED)
                    start = index + len(value)
                    changed = True
                end = max(end, start)
                output.write(data[start:end])
                pending = data[end:]
            output.write(pending)
            if changed:
                output.seek(0)
                with path.open("wb") as target:
                    shutil.copyfileobj(output, target, _CHUNK_BYTES)


# ---------------------------------------------------------------------------
# Pure argv builders (no docker invoked; asserted verbatim by tests)
# ---------------------------------------------------------------------------


def build_extract_argv(task: str, dest_dir: str) -> list[str]:
    """`docker cp <task>:/work/. <dest_dir>` - copy the container's /work contents to a host dir.

    The trailing `/.` copies the DIRECTORY CONTENTS (not the /work dir itself) into dest_dir, so
    the host tree mirrors the workspace root."""
    return ["docker", "cp", f"{task}:/work/.", dest_dir]


def build_home_extract_argv(task: str, rel: str) -> list[str]:
    """`docker cp <task>:<HOME>/<rel> -` - stream one HOME path out as a tar on stdout.

    `rel` is a HOME-relative engine session path (threads.session_paths), never caller input."""
    return ["docker", "cp", f"{task}:{CONTAINER_HOME}/{rel}", "-"]


def _extract_plain(
    stream, dest: Path, only: list[str] | None = None, max_bytes: int | None = None
) -> None:
    """Extract a tar stream into `dest`, keeping only regular files and directories with
    relative names and no `..`. Links, hard links, devices, FIFOs and anything else are skipped,
    and files are opened O_NOFOLLOW, so nothing can land outside `dest`. `only` (relative paths)
    further keeps just those paths and what sits under them. More than `max_bytes` of extracted
    file data raises ValueError."""
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            parts = Path(member.name).parts
            name = "/".join(parts)
            if (
                not parts
                or member.name.startswith("/")
                or ".." in parts
                or not (member.isreg() or member.isdir())
                or (
                    only is not None
                    and not any(name == p or name.startswith(p + "/") for p in only)
                )
            ):
                continue
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            total += member.size
            if max_bytes is not None and total > max_bytes:
                raise ValueError("archive size limit exceeded")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = tar.extractfile(member)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(source, out, _CHUNK_BYTES)


def copy_home_path(
    task: str,
    rel: str,
    dest_dir: Path,
    *,
    max_bytes: int,
    popen=subprocess.Popen,
    timeout: float = 20.0,
) -> tuple[str, int]:
    """Stream `<HOME>/<rel>` out of a live container and extract it under `dest_dir`.

    Returns (status, tar_bytes): "ok", "too_large" (the stream passed `max_bytes` and was
    killed), or "failed" (docker error, timeout, bad archive). The stream is spooled to an
    anonymous temp file, never held in memory, and a watchdog kills a silent `docker cp` at
    `timeout`. Never raises."""
    total = 0
    try:
        with tempfile.TemporaryFile() as spool:
            proc = popen(
                build_home_extract_argv(task, rel),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            watchdog = threading.Timer(timeout, proc.kill)
            watchdog.daemon = True
            watchdog.start()
            try:
                while chunk := proc.stdout.read(_CHUNK_BYTES):
                    total += len(chunk)
                    if total > max_bytes:
                        return "too_large", total
                    spool.write(chunk)
                code = proc.wait(timeout=timeout)
            finally:
                watchdog.cancel()
                proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            if code != 0:
                return "failed", total
            spool.seek(0)
            _extract_plain(spool, Path(dest_dir))
    except Exception:
        return "failed", total
    return "ok", total


def copy_session(
    task: str, paths: list[str], dest, *, max_bytes: int, popen=subprocess.Popen
) -> str:
    """Stream an engine session (`paths`: the session file, then its optional side dir) out of a
    live container into `dest` with `copy_home_path`, all paths sharing one `max_bytes` budget.
    Returns ok, too_large, or failed. The first path is required, the rest optional; no paths,
    or a session file that did not land as a regular file, is failed."""
    if not paths:
        return "failed"
    budget = max_bytes + 1
    for index, rel in enumerate(paths):
        target = Path(dest) / Path(rel).parent
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        result, used = copy_home_path(task, rel, target, max_bytes=budget, popen=popen)
        budget -= used
        if result == "too_large" or (result != "ok" and index == 0):
            return result
    try:
        landed = stat.S_ISREG(os.lstat(Path(dest) / paths[0]).st_mode)
    except OSError:
        landed = False
    return "ok" if landed else "failed"


def build_untar_argv(task: str, target: str = "/work") -> list[str]:
    """`docker exec -i <task> tar --no-same-owner -xzf - -C <target>` - extract from STDIN as uid 1001.

    WHY the tar arrives on STDIN and not via `docker cp` + a file: the resume container runs
    `--read-only` (part of `_HARDENING`), and `docker cp` INTO a read-only container is refused by
    the daemon outright ("container rootfs is marked read-only") even when the destination is a
    writable volume. So we never copy a file in - we pipe the snapshot bytes straight to `tar -xzf -`
    through `docker exec -i` (the same stdin channel `deliver_steer` uses for steering).

    WHY as the image's default uid 1001, NOT root: the `/work` volume is owned by uid 1001, and the
    task profile is `--cap-drop=ALL` (only SETUID/SETGID added back), so root inside the container
    has NO `CAP_DAC_OVERRIDE`/`CAP_CHOWN` and cannot even write into a 1001-owned dir. Extracting as
    uid 1001 (which owns `/work`) writes freely; `--no-same-owner` makes `tar` ignore the archived
    uid/gid so the tree lands owned by 1001 with no chown step needed. Reading the archive is never
    gated by file perms because it arrives on the exec's stdin, not as an on-disk root-owned file."""
    return [
        "docker",
        "exec",
        "-i",
        task,
        "tar",
        "--no-same-owner",
        "-xzf",
        "-",
        "-C",
        target,
    ]


def build_marker_argv(task: str, marker: str = f"/work/{SNAPSHOT_MARKER}") -> list[str]:
    """`docker exec <task> touch <marker>` - signal the entrypoint that /work is restored.

    Touched LAST, only after the untar succeeds, so the entrypoint never runs the engine on a
    half-restored /work. Runs as the default uid 1001, which owns `/work`."""
    return ["docker", "exec", task, "touch", marker]


def snapshot_path_for(job_id: str, env=None) -> Path:
    """The host-local path of a run's workspace snapshot: `<runs_dir>/<job_id>.snapshot.tar.gz`.

    Sits beside the run's JSON record in the same runs dir (reused from jobs, so FRANKY_RUNS_DIR
    overrides it for hermetic tests). The job_id is validated with jobs' own handle regex so a
    caller-supplied id can never traverse out of the runs dir; a malformed id is a programming
    error (every real caller passes a generated/validated id) so we refuse it loudly."""
    if not jobs._JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return jobs.runs_dir(env) / f"{job_id}.snapshot.tar.gz"


def session_path_for(job_id: str, env=None) -> Path:
    """The host-local engine-session sidecar of a `--thread` run:
    `<runs_dir>/<job_id>.session.tar.gz`. Same validation and directory as `snapshot_path_for`;
    never exported."""
    return snapshot_path_for(job_id, env).with_name(f"{job_id}.session.tar.gz")


# `build --no-publish` bundle header: the one part of a container-made bundle the host reads.
_BUNDLE_HEADER_MAX = 64 * 1024
_BUNDLE_SHA_RE = re.compile(rb"^[0-9a-f]{40}$")


def parse_bundle_header(path, branch: str) -> str:
    """Return the head SHA of the one ref in a git bundle, or raise ValueError. Pure, bounded.

    The host never runs git on a container-made file; it only reads this header as BYTES (at
    most 64 KiB, up to the first blank line) and refuses anything unexpected: a version other
    than v2/v3, a capability other than `@object-format=sha1`, a malformed or non-40-hex SHA, no
    ref, more than one ref, or a ref other than `refs/heads/<branch>`. SHAs and the ref are
    checked as ASCII. A prerequisite line (`-<sha> <description>`) checks the SHA only: the
    description is opaque bytes (a commit subject can be any encoding)."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(_BUNDLE_HEADER_MAX + 1)
    except OSError as exc:
        raise ValueError("bundle unreadable") from exc
    end = head.find(b"\n\n")
    if end < 0 or end > _BUNDLE_HEADER_MAX:
        raise ValueError("bundle header not terminated within bound")
    lines = head[:end].split(b"\n")
    if lines[0] not in (b"# v2 git bundle", b"# v3 git bundle"):
        raise ValueError("unsupported bundle version")
    try:
        want_ref = f"refs/heads/{branch}".encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("branch is not ASCII") from exc
    refs = []
    for line in lines[1:]:
        if line.startswith(b"@"):
            if lines[0] != b"# v3 git bundle" or line != b"@object-format=sha1":
                raise ValueError("unsupported bundle capability")
        elif line.startswith(b"-"):
            if not _BUNDLE_SHA_RE.match(line[1:41]) or line[41:42] not in (b"", b" "):
                raise ValueError("malformed bundle prerequisite")
        else:
            sha, space, ref = line.partition(b" ")
            if not space or not _BUNDLE_SHA_RE.match(sha):
                raise ValueError("malformed bundle ref line")
            refs.append((sha, ref))
    if len(refs) != 1 or refs[0][1] != want_ref:
        raise ValueError("bundle must hold exactly the expected branch")
    return refs[0][0].decode("ascii")


# ---------------------------------------------------------------------------
# Scrub + verify (secret-safety crux)
# ---------------------------------------------------------------------------


def _is_under_git(path: Path, root: Path) -> bool:
    """True if `path` has a `.git` component anywhere between `root` and itself (a git internal
    file - object stores, refs, config - that we must not byte-redact)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True  # outside root: treat as off-limits
    return ".git" in rel.parts


def _iter_git_dirs(root: Path):
    """Yield every `.git` DIRECTORY under root (top-level repo + any submodule/module dirs).

    A `.git` that is itself a SYMLINK is skipped (`is_dir()` follows symlinks, so a symlinked
    `.git` could point outside `root`) - consistent with the symlink guards on the file walks."""
    for path in _tree_paths(root):
        if path.name != ".git":
            continue
        if path.is_symlink():
            continue
        if path.is_dir():
            yield path


def _scrub_git_config(config_path: Path) -> None:
    """Rewrite one `.git/config` in place: strip URL userinfo and drop credential-helper lines.

    `.git/config` is plaintext, so mutating it (unlike compressed objects) is safe. A tokenized
    remote (`https://x-access-token:TOKEN@github.com/...`) or a `[credential] helper = store`
    line would otherwise carry a live credential into the snapshot. Push still works after this:
    the resume container re-authenticates from GH_TOKEN, so a bare `https://github.com/...` remote
    is fine."""
    with config_path.open("rb") as source, tempfile.TemporaryFile() as output:
        while data := source.readline(_MAX_SCAN_BYTES + 1):
            if len(data) > _MAX_SCAN_BYTES:
                raise ValueError("snapshot config line exceeded")
            if data.strip().startswith((b"helper =", b"helper=")):
                continue
            output.write(_GIT_URL_USERINFO_RE.sub(b"://", data))
        output.seek(0)
        with config_path.open("wb") as target:
            shutil.copyfileobj(output, target, _CHUNK_BYTES)


def scrub_workspace(root: Path, secrets: list[str]) -> None:
    """Scrub a snapshot tree before packing. Errors refuse the snapshot at the caller.

    Three actions:
      1. DELETE known credential files (`_CRED_FILENAMES`, any `*.pem`) and the resume marker,
         wherever they sit in the tree.
      2. Rewrite every `.git/config` (top-level + submodule) to strip URL userinfo + drop
         credential-helper lines.
      3. REDACT known secret VALUES (longest-first, so a value that is a prefix of another cannot
         mask it) out of every NON-`.git` regular file, reading/writing BYTES so binary files are
         handled too. `.git/` internal files are deliberately skipped here - their compressed
         objects would be corrupted by a naive byte-replace, and `verify_no_secrets` covers them
         via decompression instead.
    Symlinks are skipped throughout (never followed - we never want to rewrite outside the tree).
    """
    values = sorted({s for s in secrets if s}, key=len, reverse=True)

    # 1. Delete cred files + marker.
    for path in _tree_paths(root):
        if not path.is_file():
            continue
        name = path.name
        if name in _CRED_FILENAMES or name.endswith(".pem") or name == SNAPSHOT_MARKER:
            path.unlink()

    # 2. Rewrite .git/config files (recursively, to catch submodule module configs too).
    for path in _tree_paths(root):
        if path.name == "config" and path.is_file() and _is_under_git(path, root):
            _scrub_git_config(path)

    # 3. Redact known secret values in non-.git files (bytes in, bytes out).
    if values:
        value_bytes = [v.encode("utf-8") for v in values]
        for path in _tree_paths(root):
            if path.is_file() and not _is_under_git(path, root):
                _redact_file(path, value_bytes)


def _iter_object_stores(root: Path):
    """Yield `(label, git_dir)` for every git object store under root that `git cat-file` must
    decompress-verify: each top-level `.git` dir PLUS every submodule store beneath it.

    WHY submodules need their own entry: a submodule's objects live in `.git/modules/<name>/`,
    which is NOT itself named `.git`, so `_iter_git_dirs` misses it entirely. A secret committed
    into a submodule's history would then ride into the tar unverified. We find every dir under a
    `.git/modules` tree that has its own `objects/` store (handles nested submodules too) and
    verify each. Symlinked dirs are skipped throughout (never escape `root`)."""
    for git_dir in _iter_git_dirs(root):
        yield (f"{git_dir.parent.name}/.git", git_dir)
        modules = git_dir / "modules"
        if not modules.is_dir() or modules.is_symlink():
            continue
        for sub in _tree_paths(modules):
            if sub.is_symlink() or not sub.is_dir():
                continue
            objects = sub / "objects"
            if objects.is_dir() and not objects.is_symlink():
                yield (f".git/modules/{sub.name}", sub)


def verify_no_secrets(root: Path, secrets: list[str], runner=subprocess.run) -> list[str]:
    """Fail-closed scan of a scrubbed tree. Returns a SECRET-FREE list of finding LABELS.

    Empty list == verified clean. Every label is a relative path or a short descriptor - never a
    secret value - so the caller may log it. Three parts, drive the SAME file set `_pack_dir`
    tars (so nothing that gets tarred escapes the value scan):

    - VALUE scan over EVERY packed regular file's RAW BYTES, INCLUDING files under `.git`. A
      known secret value surviving ANYWHERE - a plaintext `.git` file (`.git/config` extraHeader,
      `.git/logs/*`, `.git/packed-refs`, `.git/COMMIT_EDITMSG`), a source file, anything - is a
      finding. (We do NOT skip `.git` here: the earlier decision to skip it was the fail-closed
      gap - only the byte-mutating SCRUB skips `.git`, never this read-only value scan.)
    - PATTERN scan (`profile.scan_for_secrets`) over non-`.git` files and git config files,
      catching a fresh token the agent minted (a new `ghp_...`/`sk-ant-...` not in `secrets`).
      Other git internals can contain committed token-shaped test fixtures, so they stay exempt.
    - OBJECT-CONTENT scan: git objects are zlib-compressed, so the byte scan above is blind to
      them. For each object store (top-level `.git` AND every submodule `.git/modules/*`) we run
      `git cat-file --batch-all-objects --unordered --batch` and scan its stdout BYTES for known
      secret VALUES only (not the pattern scan, same fixture-false-positive reason). A git call
      that errors or times out on ANY store is itself a FINDING - an unverifiable history is
      refused, never stored.
    """
    findings: list[str] = []
    value_bytes = [s.encode("utf-8") for s in secrets if s]

    # Scan all raw values; scan patterns in working files and git configuration.
    try:
        for path in _tree_paths(root):
            if path.is_symlink() or not path.is_file():
                continue
            rel = str(path.relative_to(root))
            with path.open("rb") as stream:
                if _contains_values(stream, value_bytes):
                    return [f"secret value survived in {rel}"]
                if not _is_under_git(path, root) or path.name == "config":
                    stream.seek(0)
                    if _has_pattern(stream):
                        return [f"credential pattern in {rel}"]
    except (OSError, ValueError):
        return ["workspace verification failed"]

    # Object-content scan (decompress via git) over every store. Any failure is a finding.
    try:
        for label, git_dir in _iter_object_stores(root):
            with tempfile.TemporaryFile() as output:
                proc = runner(
                    [
                        sys.executable,
                        "-c",
                        _GIT_LIMITED_EXEC,
                        str(_MAX_TREE_BYTES),
                        "--git-dir",
                        str(git_dir),
                        "cat-file",
                        "--batch-all-objects",
                        "--unordered",
                        "--batch",
                    ],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=20.0,
                )
                if getattr(proc, "returncode", 1) != 0:
                    return [f"git object verification errored for {label}"]
                output.seek(0)
                if _contains_values(output, value_bytes):
                    return [f"secret value in git object content ({label})"]
    except Exception:
        return ["git object verification failed"]

    return findings


# ---------------------------------------------------------------------------
# Deterministic tar packing (mirrors jobs._add_bytes' host-free metadata)
# ---------------------------------------------------------------------------


def _add_file(tar: tarfile.TarFile, arcname: str, stream) -> None:
    """Add a file with fixed, host-free metadata (mode 0600, mtime 0, uid/gid 0,
    empty uname/gname) so the tar members leak no host username/timestamps and pack
    deterministically - same discipline as jobs._add_bytes for the forensic export."""
    info = tarfile.TarInfo(name=arcname)
    info.size = os.fstat(stream.fileno()).st_size
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, stream)


def _pack_dir(src_dir: Path, dest_tar: Path) -> None:
    """Pack every regular file under `src_dir` into `dest_tar` as a deterministic gzip tar.

    Files are added in sorted order with relative arcnames; symlinks and non-regular files are
    skipped (a snapshot is a plain file tree). `tar -xzf ... -C /work` recreates the parent dirs
    on restore, so we need not store directory entries."""
    with tarfile.open(dest_tar, "w:gz") as tar:
        for path in sorted(_tree_paths(src_dir)):
            if path.is_symlink() or not path.is_file():
                continue
            arcname = str(path.relative_to(src_dir))
            with path.open("rb") as stream:
                _add_file(tar, arcname, stream)


# ---------------------------------------------------------------------------
# Orchestrators (best-effort, never raise)
# ---------------------------------------------------------------------------


def extract_workspace(task: str, dest_dir: str, runner, *, timeout: float = 20.0) -> bool:
    """`docker cp` the container's /work contents into `dest_dir`. Return True iff rc == 0.

    This is the ONLY snapshot step that needs the container ALIVE, so it must run before the reap
    and is capped (default 20s) so it can never delay teardown. Never raises."""
    try:
        proc = runner(
            build_extract_argv(task, dest_dir), capture_output=True, text=True, timeout=timeout
        )
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


def finalize_snapshot(src_dir: Path, dest_tar: Path, secrets, runner=subprocess.run) -> str | None:
    """Scrub + verify + pack an extracted workspace into `dest_tar`. Return the path or None.

    Runs OFF the critical path (the container is already reaped by the time this is called on the
    timeout path), so the potentially slower scrub/verify/pack never delays teardown. Fail-closed:
    if `verify_no_secrets` reports ANYTHING, no tar is written and None is returned (nothing
    sensitive is logged - only the count of findings is knowable to the caller via the None).
    On success the tar is written 0600 under a 0700 parent, packed to a temp file beside it and
    renamed into place. The whole body is wrapped so any failure degrades to None (the temp file
    is unlinked, `dest_tar` is never partial), and `src_dir` is always removed at the end."""
    src_dir = Path(src_dir)
    dest_tar = Path(dest_tar)
    tmp_tar = None
    try:
        scrub_workspace(src_dir, list(secrets))
        findings = verify_no_secrets(src_dir, list(secrets), runner=runner)
        if findings:
            return None
        dest_tar.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Pack beside the destination and rename, so a failed pack never leaves a partial tar.
        fd, name = tempfile.mkstemp(prefix=TMP_PACK_PREFIX, suffix=".tar.gz", dir=dest_tar.parent)
        os.close(fd)
        tmp_tar = Path(name)
        _pack_dir(src_dir, tmp_tar)
        tmp_tar.chmod(0o600)
        os.replace(tmp_tar, dest_tar)
        return str(dest_tar)
    except Exception:
        if tmp_tar is not None:
            tmp_tar.unlink(missing_ok=True)
        return None
    finally:
        _rmtree(src_dir)


def snapshot_workspace(
    task: str, dest_tar: Path, secrets, runner, *, timeout: float = 20.0
) -> str | None:
    """Convenience one-shot for the `job kill` path (container reaped right after this returns).

    Extract /work to a fresh tmpdir, then scrub/verify/pack it. Returns the snapshot path or None
    (extract failed, or verification refused it). The `run_in_container` timeout path does NOT use
    this - it calls extract_workspace + finalize_snapshot separately so the reap happens BETWEEN
    them. Never raises."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="franky-snapshot-"))
    except Exception:
        return None
    if not extract_workspace(task, str(tmp), runner, timeout=timeout):
        _rmtree(tmp)
        return None
    return finalize_snapshot(tmp, Path(dest_tar), secrets, runner)


def restore_into_container(
    task: str,
    snapshot_tar: Path,
    runner,
    *,
    ready_polls: int = 30,
    poll_interval: float = 0.5,
    timeout: float = 20.0,
    sleeper=time.sleep,
) -> bool:
    """Pipe a workspace tar INTO a freshly launched container over `docker exec -i` stdin, extract
    it into /work as the run uid (1001), and touch the ready marker. Return True iff every docker
    step succeeded; never raises.

    The container starts in resume-wait mode (see the entrypoint) and blocks until the marker
    appears, so we first poll `container_running` (up to `ready_polls`) to confirm it is up, then:
    untar-from-stdin as uid 1001 (`-i --no-same-owner`) -> touch marker, in that order. WHY stdin,
    not `docker cp` the tar in: the resume container is `--read-only`, and the daemon refuses a `cp`
    INTO a read-only container even for a writable target, so a cp-based restore fails on every resume;
    piping the bytes to `tar -xzf -` avoids the cp entirely. WHY extract as uid 1001, not root +
    chown: the task profile is `--cap-drop=ALL`, so in-container root lacks the caps to write into
    the 1001-owned `/work` or to chown - extracting as 1001 (which owns `/work`) with
    `--no-same-owner` lands the tree owned by 1001 directly, no chown needed (see build_untar_argv).
    The snapshot file is streamed via `stdin=` (not read fully into memory). If reading the snapshot
    fails, or the untar returns nonzero, we return False WITHOUT touching the marker, so the
    entrypoint times out and exits nonzero rather than running the engine on an empty/half-restored
    /work. `container` is imported lazily to avoid an import cycle (container imports snapshot)."""
    from . import container

    try:
        for _ in range(ready_polls):
            if container.container_running(task, runner):
                break
            sleeper(poll_interval)

        # Stream the snapshot bytes to `tar -xzf -` over the exec's stdin; no file is copied in.
        # No `text=True` here (unlike the marker call below): stdin is a BINARY file object and the
        # captured stdout/stderr are unused - text mode would try to str-decode them for nothing.
        with open(snapshot_tar, "rb") as tar_fh:
            untar = runner(
                build_untar_argv(task),
                stdin=tar_fh,
                capture_output=True,
                timeout=timeout,
            )
        if getattr(untar, "returncode", 1) != 0:
            return False
        marker = runner(build_marker_argv(task), capture_output=True, text=True, timeout=timeout)
        return getattr(marker, "returncode", 1) == 0
    except Exception:
        return False


def _rmtree(path: Path) -> None:
    """Best-effort recursive delete of a temp dir. Never raises."""
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
