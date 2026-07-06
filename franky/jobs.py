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

import io
import json
import os
import re
import stat
import statistics
import tarfile
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

# Terminal-status classification for `compute_stats` (issue #64). `running` is non-terminal (it
# is neither, and is reported separately as running_fresh / hangs). `already_open` never reaches
# the registry (it short-circuits before a record is written), so it is intentionally absent.
# `replay_complete` (issue #70) is ALSO intentionally absent: a reproduce-only replay is neither
# a build success nor failure, exactly like the diagnose statuses below it. A `--open-pr` replay
# instead reuses `pr_opened`/`no_pr` and so folds into pass-rate exactly as `iterate` already
# does - deliberate, not an oversight.
_SUCCESS_STATUSES = frozenset({"pr_opened", "iterate_complete"})
_FAILURE_STATUSES = frozenset({"no_pr", "agent_error", "timeout", "killed"})


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
    source: str | None = None,
    task_full: str | None = None,
    base_sha: str | None = None,
    replay_of: str | None = None,
    resumed_from: str | None = None,
) -> dict:
    """Shape the initial (status=running) record written before the container pass starts.

    `task` MUST already be redacted + truncated by the caller - the record stores no secret
    value. Runtime fields (status/ended_at/pr_url/economics/exit_code/log_path) are filled in
    by update_record when the pass finishes.

    Four fields added for `franky job replay` (issue #70), all null-default so a caller that
    never passes them (iterate, diagnose) is byte-identical to before: `source` (the TaskSpec's
    source: issue|jira|prose|pr) + `task_full` (the REDACTED, PROSE_MAX_CHARS-capped full task
    text - a replay's saved input, distinct from `task`'s 200-char display summary) together let
    a later `job replay` reconstruct the original TaskSpec; `base_sha` (the default-branch tip at
    build start, see baseref.resolve_base_sha) pins the commit a replay checks out; `replay_of`
    (set only on a run THAT IS a replay) names the original job id being reproduced.

    `resumed_from` + `snapshot_path` support `franky job resume` (issue #71): `resumed_from` (set
    only on a run THAT IS a resume) names the original job whose workspace was restored;
    `snapshot_path` (null here, set later via update_record like diagnostics) is the host-local
    path of the scrubbed, fail-closed-verified workspace snapshot this run produced on timeout.
    Both default null so a caller that never passes them is byte-identical to before.
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
        # Best-effort runtime signals captured host-side just before container teardown
        # (issue #69); None until (and unless) a diagnostics_sink was populated. See
        # container.capture_diagnostics for the field shape.
        "diagnostics": None,
        "source": source,
        "task_full": task_full,
        "base_sha": base_sha,
        "replay_of": replay_of,
        "resumed_from": resumed_from,
        "snapshot_path": None,
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
    directory = runs_dir(env)
    removed = 0
    for record in records[keep:]:
        if _is_fresh_running(record):
            continue
        job_id = record.get("job_id", "")
        path = _record_path(job_id, env)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
        # Remove the run's workspace snapshot sidecar (issue #71) alongside its record. The
        # sidecar path is inlined here (NOT via snapshot.snapshot_path_for) to avoid an import
        # cycle: snapshot imports jobs.runs_dir, so jobs must not import snapshot.
        try:
            (directory / f"{job_id}.snapshot.tar.gz").unlink(missing_ok=True)
        except OSError:
            pass
    # Sweep ORPHAN snapshots (issue #71): a snapshot whose <id>.json record no longer exists (a
    # failed record-write, or a record pruned in an earlier pass) would otherwise leak disk
    # forever. A snapshot whose record IS a fresh `running` one is never touched (its record
    # survives above, so its id is in `live_ids`).
    live_ids = {r.get("job_id", "") for r in records}
    try:
        snapshots = sorted(directory.glob("*.snapshot.tar.gz"))
    except OSError:
        snapshots = []
    for snap in snapshots:
        snap_id = snap.name[: -len(".snapshot.tar.gz")]
        if snap_id in live_ids:
            continue
        try:
            snap.unlink(missing_ok=True)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Cross-run analytics + forensic export (issue #64, items #6/#7) - both pure over the registry
# records / on-disk artifacts above; no docker, stdlib-only, so they stay in the fast suite.
# ---------------------------------------------------------------------------


def _median_or_none(values: list[float]) -> float | None:
    """Median of `values` (rounded), or None when empty - median([]) would raise."""
    return round(statistics.median(values), 3) if values else None


def _group_stats(records: list[dict]) -> dict:
    """The metric block shared by the overall / by_engine / by_repo views.

    success/failed classify each record by its terminal status (running records count as
    neither); success_rate is over terminal runs only (None when there are none). Durations
    and costs come from the economics block and skip records that lack a numeric value, so a
    still-running or economics-less record never skews the medians/totals.
    """
    success = failed = 0
    durations: list[float] = []
    costs: list[float] = []
    for record in records:
        status = record.get("status")
        if status in _SUCCESS_STATUSES:
            success += 1
        elif status in _FAILURE_STATUSES:
            failed += 1
        econ = record.get("economics") or {}
        dur = econ.get("duration_s")
        if isinstance(dur, (int, float)):
            durations.append(dur)
        cost = econ.get("cost_usd")
        if isinstance(cost, (int, float)):
            costs.append(cost)
    terminal = success + failed
    return {
        "n": len(records),
        "success": success,
        "failed": failed,
        "success_rate": round(success / terminal, 3) if terminal else None,
        "median_duration_s": _median_or_none(durations),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
    }


def compute_stats(records: list[dict]) -> dict:
    """Aggregate cross-run health over `records` (issue #64 item #7). Pure - no I/O.

    Returns overall counts + rates + a by_engine / by_repo breakdown. `running_fresh` is the
    count of genuinely in-flight runs (see `_is_fresh_running`); `hangs` is timeout runs PLUS
    stale `running` records (crash orphans that never finished) - note this deliberately mixes a
    terminal status (timeout) with a non-terminal one (stale running), so `hangs` is NOT a subset
    of `by_status`. Everything degrades cleanly on an empty list (rates/medians/total_cost ->
    None, counts -> 0).
    """
    by_status: dict[str, int] = {}
    for record in records:
        status = record.get("status") or "unknown"
        by_status[status] = by_status.get(status, 0) + 1

    overall = _group_stats(records)
    running_fresh = sum(1 for record in records if _is_fresh_running(record))
    stale_running = by_status.get("running", 0) - running_fresh
    hangs = by_status.get("timeout", 0) + stale_running

    def _breakdown(key: str) -> dict:
        names = sorted({(record.get(key) or "?") for record in records})
        return {
            name: _group_stats([r for r in records if (r.get(key) or "?") == name])
            for name in names
        }

    return {
        "total": len(records),
        "by_status": by_status,
        "success": overall["success"],
        "failed": overall["failed"],
        "running_fresh": running_fresh,
        "hangs": hangs,
        "success_rate": overall["success_rate"],
        "median_duration_s": overall["median_duration_s"],
        "total_cost_usd": overall["total_cost_usd"],
        "by_engine": _breakdown("engine"),
        "by_repo": _breakdown("repo"),
    }


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add `data` to `tar` as `name` with fixed, host-free metadata.

    mode 0600, mtime 0, uid/gid 0, empty uname/gname so the bundle's tar members are
    deterministic and leak no host username/timestamps (the gzip wrapper still stamps its own
    header time, but the archived files carry nothing host-specific).
    """
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def export_bundle(record: dict, dest: Path) -> dict:
    """Write a portable forensic .tar.gz for `record` to `dest` (issue #64 item #6).

    The bundle holds `record.json` (the secret-free run record) and, when the run's transcript
    still exists on disk, `transcript.log` (the ALREADY-redacted task log). Both are secret-free
    by construction, so this adds no new redaction surface. Returns
    {output_path, bytes, included}. Raises OSError on a write failure (the caller maps it to a
    clean typed error); a transcript that has since been deleted is simply omitted, not fatal.

    DELIBERATELY EXCLUDED (issue #71): the run's workspace snapshot sidecar
    (`<id>.snapshot.tar.gz`, sitting right beside the record) is NEVER added to the bundle. It is
    a host-local resume artifact that may contain workspace bytes; unlike record.json and the
    redacted transcript it is not a secret-free forensic artifact, so it stays on the host. Only
    the two members below are ever packed - do not extend this to pick up the sidecar.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with tarfile.open(dest, "w:gz") as tar:
        record_bytes = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _add_bytes(tar, "record.json", record_bytes)
        included.append("record.json")
        log_path = record.get("log_path") or ""
        if log_path and Path(log_path).exists():
            try:
                log_bytes = Path(log_path).read_bytes()
            except OSError:
                log_bytes = None
            if log_bytes is not None:
                _add_bytes(tar, "transcript.log", log_bytes)
                included.append("transcript.log")
    return {"output_path": str(dest), "bytes": dest.stat().st_size, "included": included}
