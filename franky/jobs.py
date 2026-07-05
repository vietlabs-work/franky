"""Run registry for `franky jobs` / `franky job ...` (issue #63).

WHY this exists: Franky's caller is an agent that dispatches a build and later needs to ask
"what happened to it?" - is it still running, stuck, or dead. Franky used to assign each run a
container name and throw it away; there was no handle to come back with. This module persists a
small JSON record per run under `~/.franky/runs/<job_id>.json` so `franky jobs` / `job status` /
`job logs` / `job kill` can list, inspect, read, and reap runs after the fact - and, crucially,
observe or kill a run that is STILL IN FLIGHT (the half-day-stuck-run scenario) from another
shell.

SECRET-SAFETY: the record stores NO secret values - only names, paths, status, timings, and a
REDACTED task summary. The caller redacts before handing text in; this module never sees a raw
secret. Callers on the build path use these best-effort (a registry hiccup must never fail a
build), mirroring economics.py's "never raises into a run" contract.

STDLIB-only, filesystem-only (no docker) so it stays trivially unit-testable; the docker
mechanics for `job status`/`kill` live in container.py. The runs dir is overridable via
FRANKY_RUNS_DIR for hermetic tests, mirroring userconfig's FRANKY_CONFIG_FILE.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR_VAR = "FRANKY_RUNS_DIR"

# A job id is our own generated 12-hex handle. Validate any id that reaches the filesystem so a
# user-supplied `franky job status <id>` can never traverse out of the runs dir (e.g. "../..").
_JOB_ID_RE = re.compile(r"^[0-9a-f]{1,64}$")

# Default number of run records to keep; prune() trims older ones so the dir stays bounded.
DEFAULT_KEEP = 200

# A `status=running` record is normally never pruned (an in-flight run must stay observable).
# But a run that crashed mid-flight (an unexpected, non-FrankyError exit before the record was
# finalized) leaves a "running" record forever, and would otherwise grow the dir unbounded. A
# real run never lasts anywhere near this long (the --max-duration default is 30 min), so a
# "running" record older than this is a crash orphan and becomes eligible for pruning.
STALE_RUNNING_SECS = 24 * 3600


def runs_dir(env: Mapping[str, str] | None = None) -> Path:
    """Directory holding per-run JSON records. FRANKY_RUNS_DIR overrides (used as-is, for
    hermetic tests); absent -> ~/.franky/runs."""
    env = os.environ if env is None else env
    override = env.get(RUNS_DIR_VAR)
    if override:
        return Path(override)
    return Path.home() / ".franky" / "runs"


def new_job_id() -> str:
    """A fresh 12-hex job handle (also the suffix of the container/net/proxy names)."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision), for started_at/ended_at."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_record(
    *,
    job_id: str,
    command: str,
    repo: str,
    engine: str,
    task: str,
    container: str,
    network: str,
    proxy: str,
    branch: str | None,
    started_at: str,
) -> dict:
    """Shape the initial (status=running) record written before the container pass starts.

    `task` MUST already be redacted + truncated by the caller - the record stores no secret
    value. Runtime fields (status/ended_at/pr_url/economics/exit_code/log_path) are filled in
    by update_record when the pass finishes.
    """
    return {
        "job_id": job_id,
        "command": command,
        "repo": repo,
        "engine": engine,
        "task": task,
        "container": container,
        "network": network,
        "proxy": proxy,
        "branch": branch,
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "pr_url": None,
        "log_path": "",
        "economics": None,
        "exit_code": None,
    }


def _record_path(job_id: str, env: Mapping[str, str] | None) -> Path | None:
    """Path of the record file for `job_id`, or None if the id is not a safe handle."""
    if not _JOB_ID_RE.match(job_id):
        return None
    return runs_dir(env) / f"{job_id}.json"


def write_record(record: dict, env: Mapping[str, str] | None = None) -> bool:
    """Atomically write `record` to ~/.franky/runs/<job_id>.json (dir 0700, file 0600).

    Returns True on success, False on any failure - best-effort, so a registry write can never
    break a build. Mirrors userconfig's tempfile+os.replace atomic write. The `except` is broad
    (not just OSError) so a non-serializable value in the record degrades to False rather than
    raising, honoring the "False on any failure" contract.
    """
    job_id = record.get("job_id", "")
    path = _record_path(job_id, env)
    if path is None:
        return False
    try:
        parent = path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps(record, indent=2, sort_keys=True) + "\n"
        fd, tmp_str = tempfile.mkstemp(dir=parent, prefix=".franky-run-")
        tmp = Path(tmp_str)
        try:
            os.chmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before writing
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        return False
    return True


def read_record(job_id: str, env: Mapping[str, str] | None = None) -> dict | None:
    """Return the record for `job_id`, or None if it is missing, malformed, or not a valid id.

    Never raises - a missing/corrupt/unsafe entry is reported as None so the caller can emit a
    clean "job not found" error instead of a traceback (fail-closed).
    """
    path = _record_path(job_id, env)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_record(job_id: str, patch: dict, env: Mapping[str, str] | None = None) -> bool:
    """Read-modify-write `patch` into an existing record. Best-effort (returns False on any
    failure or a missing/malformed record); never raises into a build."""
    current = read_record(job_id, env)
    if current is None:
        return False
    current.update(patch)
    return write_record(current, env)


def list_records(env: Mapping[str, str] | None = None) -> list[dict]:
    """All valid records, newest first by started_at. Malformed files are skipped, not fatal."""
    directory = runs_dir(env)
    out: list[dict] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("job_id"):
            out.append(data)
    out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return out


def _is_fresh_running(record: dict) -> bool:
    """True if the record is `status=running` AND recent enough to be a genuinely in-flight run
    (younger than STALE_RUNNING_SECS). A stale "running" record is a crash orphan - not fresh -
    so prune may reclaim it. An unparseable started_at is treated as fresh (keep, don't guess)."""
    if record.get("status") != "running":
        return False
    started = record.get("started_at")
    if not started:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return True
    return age < STALE_RUNNING_SECS


def prune(env: Mapping[str, str] | None = None, keep: int = DEFAULT_KEEP) -> int:
    """Delete the oldest records beyond `keep` so the dir stays bounded. Returns the count
    removed. Best-effort - an unremovable file is skipped.

    Called only from the WRITE path (when a new run starts), never as a side effect of a read
    (`jobs`/`job status` are pure reads). A genuinely in-flight `running` record is never pruned
    (it must stay observable/killable no matter how many runs precede it); a STALE `running`
    record (a crash orphan, older than STALE_RUNNING_SECS) is reclaimable so a crash loop can't
    grow the dir unbounded.
    """
    records = list_records(env)  # newest first
    removed = 0
    for record in records[keep:]:
        if _is_fresh_running(record):
            continue
        path = _record_path(record.get("job_id", ""), env)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed
