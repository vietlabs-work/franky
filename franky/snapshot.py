"""Workspace snapshot + restore for `franky job resume` (issue #71).

WHY this exists: a hung/timed-out/killed run throws away everything the agent did inside the
container's tmpfs `/work` (the repo clone + its branch state). `franky job resume` lets a fresh
engine CONTINUE that work instead of restarting from scratch. To do that we must capture `/work`
off the container before it is reaped, store it host-side, and later restore it into a freshly
launched (still fully hardened) container.

WHY it does NOT touch the container hardening: the snapshot is taken with host-side
`docker cp`/`docker exec` only - no bind mount, no host docker socket, no relaxation of
`_HARDENING`. The restore does NOT `docker cp` the tar back IN (the daemon refuses a `cp` into a
`--read-only` container, even to a tmpfs target); instead it pipes the tar to `tar -xzf -` over
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

import io
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from . import jobs
from .profile import scan_for_secrets

# The container-side marker the host touches (via `docker exec`) once the workspace tar has been
# copied in and extracted; the entrypoint waits for it before exec-ing the engine (see
# franky-dind-entrypoint.sh). Living under /work means it lands on the writable tmpfs.
SNAPSHOT_MARKER = ".franky-resume-ready"

# Env flag that puts the entrypoint into resume-wait mode (by-value, non-secret). build_docker_argv
# passes `-e FRANKY_RESUME_WAIT=1` when resuming; the entrypoint unsets it after the marker lands.
RESUME_WAIT_ENV = "FRANKY_RESUME_WAIT"

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
_GIT_URL_USERINFO_RE = re.compile(r"://[^/@\s]*@")

# The bytes we substitute a matched secret VALUE with when redacting non-.git files in the tree.
_REDACTED = b"[REDACTED]"


# ---------------------------------------------------------------------------
# Pure argv builders (no docker invoked; asserted verbatim by tests)
# ---------------------------------------------------------------------------


def build_extract_argv(task: str, dest_dir: str) -> list[str]:
    """`docker cp <task>:/work/. <dest_dir>` - copy the container's /work contents to a host dir.

    The trailing `/.` copies the DIRECTORY CONTENTS (not the /work dir itself) into dest_dir, so
    the host tree mirrors the workspace root."""
    return ["docker", "cp", f"{task}:/work/.", dest_dir]


def build_untar_argv(task: str, target: str = "/work") -> list[str]:
    """`docker exec -i <task> tar --no-same-owner -xzf - -C <target>` - extract from STDIN as uid 1001.

    WHY the tar arrives on STDIN and not via `docker cp` + a file: the resume container runs
    `--read-only` (part of `_HARDENING`), and `docker cp` INTO a read-only container is refused by
    the daemon outright ("container rootfs is marked read-only") even when the destination is a
    writable tmpfs. So we never copy a file in - we pipe the snapshot bytes straight to `tar -xzf -`
    through `docker exec -i` (the same stdin channel `deliver_steer` uses for steering).

    WHY as the image's default uid 1001, NOT root: the `/work` tmpfs is owned by uid 1001, and the
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
    for path in root.rglob(".git"):
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
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    out_lines = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        # Drop any credential-helper line (a helper = <program> config directive) wholesale.
        if stripped.startswith("helper =") or stripped.startswith("helper="):
            changed = True
            continue
        new_line = _GIT_URL_USERINFO_RE.sub("://", line)
        if new_line != line:
            changed = True
        out_lines.append(new_line)
    if changed:
        try:
            config_path.write_text("".join(out_lines), encoding="utf-8")
        except OSError:
            pass


def scrub_workspace(root: Path, secrets: list[str]) -> None:
    """Best-effort, in-place scrub of a snapshot tree before it is packed. Never raises.

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
    try:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            name = path.name
            if name in _CRED_FILENAMES or name.endswith(".pem") or name == SNAPSHOT_MARKER:
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    # 2. Rewrite .git/config files (recursively, to catch submodule module configs too).
    for git_dir in _iter_git_dirs(root):
        for config_path in git_dir.rglob("config"):
            if config_path.is_file() and not config_path.is_symlink():
                _scrub_git_config(config_path)

    # 3. Redact known secret values in non-.git files (bytes in, bytes out).
    if values:
        value_bytes = [v.encode("utf-8") for v in values]
        try:
            for path in root.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                if _is_under_git(path, root):
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                new_data = data
                for vb in value_bytes:
                    if vb in new_data:
                        new_data = new_data.replace(vb, _REDACTED)
                if new_data != data:
                    try:
                        path.write_bytes(new_data)
                    except OSError:
                        pass
        except OSError:
            pass


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
        for sub in modules.rglob("*"):
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
    - PATTERN scan (`profile.scan_for_secrets`) over NON-`.git` files only, catching a FRESH
      token the agent minted (a new `ghp_...`/`sk-ant-...` not in `secrets`). Kept off `.git`
      deliberately: a committed test fixture could legitimately hold a token-shaped string, and
      pattern-flagging it would make every such repo unresumable.
    - OBJECT-CONTENT scan: git objects are zlib-compressed, so the byte scan above is blind to
      them. For each object store (top-level `.git` AND every submodule `.git/modules/*`) we run
      `git cat-file --batch-all-objects --unordered --batch` and scan its stdout BYTES for known
      secret VALUES only (not the pattern scan, same fixture-false-positive reason). A git call
      that errors or times out on ANY store is itself a FINDING - an unverifiable history is
      refused, never stored.
    """
    findings: list[str] = []
    value_bytes = [s.encode("utf-8") for s in secrets if s]

    # Value scan over the full packed file set (INCLUDING .git); pattern scan on non-.git only.
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            rel = str(path.relative_to(root))
            if any(vb in data for vb in value_bytes):
                findings.append(f"secret value survived in {rel}")
                continue
            if _is_under_git(path, root):
                continue  # pattern scan is non-.git only (fixture false positives)
            text = data.decode("utf-8", errors="replace")
            if scan_for_secrets(text):
                findings.append(f"credential pattern in {rel}")
    except OSError:
        pass

    # Object-content scan (decompress via git) over every store. Any failure is a finding.
    for label, git_dir in _iter_object_stores(root):
        try:
            proc = runner(
                [
                    "git",
                    "--git-dir",
                    str(git_dir),
                    "cat-file",
                    "--batch-all-objects",
                    "--unordered",
                    "--batch",
                ],
                capture_output=True,
                timeout=20.0,
            )
        except Exception:
            findings.append(f"git object verification failed for {label}")
            continue
        if getattr(proc, "returncode", 1) != 0:
            findings.append(f"git object verification errored for {label}")
            continue
        stdout = getattr(proc, "stdout", b"") or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="replace")
        if any(vb in stdout for vb in value_bytes):
            findings.append(f"secret value in git object content ({label})")

    return findings


# ---------------------------------------------------------------------------
# Deterministic tar packing (mirrors jobs._add_bytes' host-free metadata)
# ---------------------------------------------------------------------------


def _add_file(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Add `data` as `arcname` with fixed, host-free metadata (mode 0600, mtime 0, uid/gid 0,
    empty uname/gname) so the tar members leak no host username/timestamps and pack
    deterministically - same discipline as jobs._add_bytes for the forensic export."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def _pack_dir(src_dir: Path, dest_tar: Path) -> None:
    """Pack every regular file under `src_dir` into `dest_tar` as a deterministic gzip tar.

    Files are added in sorted order with relative arcnames; symlinks and non-regular files are
    skipped (a snapshot is a plain file tree). `tar -xzf ... -C /work` recreates the parent dirs
    on restore, so we need not store directory entries."""
    with tarfile.open(dest_tar, "w:gz") as tar:
        for path in sorted(src_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            arcname = str(path.relative_to(src_dir))
            _add_file(tar, arcname, data)


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
    On success the tar is written 0600 under a 0700 parent. The whole body is wrapped so any
    failure degrades to None (a partial dest_tar is unlinked), and `src_dir` is always removed at
    the end."""
    src_dir = Path(src_dir)
    dest_tar = Path(dest_tar)
    try:
        scrub_workspace(src_dir, list(secrets))
        findings = verify_no_secrets(src_dir, list(secrets), runner=runner)
        if findings:
            return None
        dest_tar.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _pack_dir(src_dir, dest_tar)
        dest_tar.chmod(0o600)
        return str(dest_tar)
    except Exception:
        try:
            dest_tar.unlink(missing_ok=True)
        except OSError:
            pass
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
    INTO a read-only container even for a tmpfs target, so a cp-based restore fails on every resume;
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
