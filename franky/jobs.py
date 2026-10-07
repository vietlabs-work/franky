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
import shutil
import socket
import stat
import statistics
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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

# Host-local sidecars that live beside a run record: the workspace snapshot (issue #71), the
# engine session of a `--thread` run, the git bundle of a `build --no-publish` run, and the
# liveness heartbeat. Pruned with the record, never
# exported. The heartbeat file carries no `job_id` key so list_records never mistakes it for a run.
_SIDECAR_SUFFIXES = (".snapshot.tar.gz", ".session.tar.gz", ".progress.json", ".bundle")

# Temp session dirs and packed tars that `--thread` runs create in the runs dir (`.tmp-session-`,
# `.tmp-pack-`). A crash can leave one behind; prune sweeps those older than a real run can last.
TMP_PREFIX = ".tmp-"

# Terminal-status classification for `compute_stats` (issue #64). `running` is non-terminal (it
# is neither, and is reported separately as running_fresh / hangs). `already_open` never reaches
# the registry (it short-circuits before a record is written), so it is intentionally absent.
# `replay_complete` (issue #70) is ALSO intentionally absent: a reproduce-only replay is neither
# a build success nor failure, exactly like the diagnose statuses below it. A `--open-pr` replay
# instead reuses `pr_opened`/`no_pr` and so folds into pass-rate exactly as `iterate` already
# does - deliberate, not an oversight.
# `review_published`/`review_complete` (review-pr) are the review analogs of a successful
# build/iterate pass. `no_findings` is a FAILURE, not a neutral outcome: it exits with
# EXIT_AGENT (7) exactly like `agent_error`, and is the review analog of `no_pr` (already a
# failure status). `publish_blocked_stale_head`/`publish_failed` are review-pr failures too -
# the review ran but its outcome could not reach the PR.
_SUCCESS_STATUSES = frozenset(
    {"pr_opened", "iterate_complete", "review_published", "review_complete", "branch_ready"}
)
_FAILURE_STATUSES = frozenset(
    {
        "no_pr",
        "agent_error",
        "timeout",
        "killed",
        "no_findings",
        "publish_blocked_stale_head",
        "publish_failed",
        "publish_uncertain",
        "no_changes",
        "export_failed",
        "export_refused",
    }
)


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
    thread_id: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    threaded: bool = False,
    no_publish: bool = False,
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

    `steer_notes` (issue #72) is a bounded audit trail of operator corrections injected via
    `franky job attach` while this run was live - null until (and unless) `job attach`
    annotates it via update_record. See cli.job_attach.

    `thread_id` names the thread (threads.py) a run belongs to: a `review-pr --thread` or
    `iterate --thread` run, or a `build --thread` / resumed run once its session is bound. It is
    added ONLY when set, so every other record keeps exactly its previous keys. `threaded` (only
    when true) marks a `build --thread` run and a resume of one, native resume or not.

    `session_id` (with the `model` it runs on) is the engine session a `build --thread` or a
    resume of one pins BEFORE launch, so `job kill`, `job resume` and a later bind know it. Both
    are added ONLY when a session is set. `session_path` (the scrubbed, verified session sidecar
    `<job_id>.session.tar.gz`, never exported) and `thread_bound` are patched in later.
    """
    record = {
        "job_id": job_id,
        "command": command,
        # Liveness identity for `job status`: which process owns this run, on which host, and a
        # process start marker so a reused pid is not mistaken for the run (see pid_alive).
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "pid_started_at": pid_start_marker(os.getpid()),
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
        "steer_notes": None,
    }
    if thread_id is not None:
        record["thread_id"] = thread_id
    if threaded:
        record["threaded"] = True
    if no_publish:
        record["no_publish"] = True
    if session_id is not None:
        record["session_id"] = session_id
        record["model"] = model
    return record


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
    return _atomic_write(path, record, prefix=".franky-run-")


def _atomic_write(path: Path, data: dict, *, prefix: str) -> bool:
    """Write `data` as JSON to `path` via tempfile + os.replace (dir 0700, file 0600).

    Shared by the run registry and the thread store (threads.py). Returns False on any failure
    (including a non-serializable value) instead of raising.
    """
    try:
        parent = path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, tmp_str = tempfile.mkstemp(dir=parent, prefix=prefix)
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
        # Remove the run's workspace snapshot (issue #71) and session sidecars alongside its
        # record. The sidecar paths are inlined here (NOT via snapshot.snapshot_path_for) to
        # avoid an import cycle: snapshot imports jobs.runs_dir, so jobs must not import snapshot.
        for suffix in _SIDECAR_SUFFIXES:
            try:
                (directory / f"{job_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
    # Sweep ORPHAN sidecars (issue #71): a sidecar whose <id>.json record no longer exists (a
    # failed record-write, or a record pruned in an earlier pass) would otherwise leak disk
    # forever. A sidecar whose record IS a fresh `running` one is never touched (its record
    # survives above, so its id is in `live_ids`).
    live_ids = {r.get("job_id", "") for r in records}
    for suffix in _SIDECAR_SUFFIXES:
        try:
            sidecars = sorted(directory.glob(f"*{suffix}"))
        except OSError:
            sidecars = []
        for sidecar in sidecars:
            if sidecar.name[: -len(suffix)] in live_ids:
                continue
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
    now = datetime.now(timezone.utc).timestamp()
    try:
        leftovers = sorted(directory.glob(f"{TMP_PREFIX}*"))
    except OSError:
        leftovers = []
    for leftover in leftovers:
        try:
            if now - leftover.lstat().st_mtime <= STALE_RUNNING_SECS:
                continue
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
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
    redacted transcript it is not a secret-free forensic artifact, so it stays on the host. The
    same holds for the engine session sidecar (`<id>.session.tar.gz`). Only the two members
    below are ever packed - do not extend this to pick up either sidecar.
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
                source = Path(log_path).open("rb")
            except OSError:
                source = None
            if source is not None:
                with source:
                    info = tarfile.TarInfo("transcript.log")
                    info.size = os.fstat(source.fileno()).st_size
                    info.mode = 0o600
                    tar.addfile(info, source)
                    included.append("transcript.log")
    return {"output_path": str(dest), "bytes": dest.stat().st_size, "included": included}


# ---------------------------------------------------------------------------
# Liveness (issue: agents polling a run could not tell "working" from "dead" or what to do next).
# A heartbeat sidecar records progress; `derive_liveness` turns record + sidecar + probes into a
# state and one safe next step. Both stay stdlib-only and take every input by injection.
# ---------------------------------------------------------------------------

# No agent output for this long marks an alive run `quiet` (still alive, worth a slower poll).
QUIET_AFTER_SECS = 120
# At most one heartbeat write per this many seconds, so a chatty agent costs ~nothing.
HEARTBEAT_THROTTLE_SECS = 15
_DONE_STATUSES = _SUCCESS_STATUSES | {"replay_complete", "diagnosed"}
_REVIEW_PRE_PUBLISH_STATUSES = frozenset({"timeout", "agent_error", "no_findings"})


def _review_rerun_command(record: dict) -> str:
    """Rebuild a review-pr rerun with the options that guard publishing."""
    # review-pr records the canonical PR URL as its task until the run ends.
    argv = ["franky", "review-pr", str(record.get("pr_url") or record.get("task"))]
    if record.get("reviewed_sha"):
        argv += ["--expected-head-sha", str(record["reviewed_sha"])]
    if record.get("no_publish"):
        argv.append("--no-publish")
    if record.get("thread_id"):
        argv.append("--thread")
    return " ".join(argv + ["--json"])


def progress_path(job_id: str, env: Mapping[str, str] | None = None) -> Path | None:
    """Path of the heartbeat sidecar for `job_id`, or None if the id is not a safe handle."""
    if not _JOB_ID_RE.match(job_id):
        return None
    return runs_dir(env) / f"{job_id}.progress.json"


def read_progress(job_id: str, env: Mapping[str, str] | None = None) -> dict | None:
    """The heartbeat sidecar as a dict, or None if missing/corrupt. Never raises."""
    path = progress_path(job_id, env)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Heartbeat:
    """Best-effort progress sidecar for one run: `<job_id>.progress.json`.

    A SEPARATE file from the run record on purpose: the record is an unlocked read-modify-write
    that `job kill` also updates, so a periodic heartbeat writing it could resurrect a finished
    run's status. It stores the tool NAME only (never arguments, which can carry decoded secret
    fragments), and never raises into a run.
    """

    def __init__(self, job_id: str, env: Mapping[str, str] | None = None, clock=time.monotonic):
        self._job_id = job_id
        self._env = env
        self._clock = clock
        self._last_write: float | None = None
        self.state: dict = {
            "phase": "setup",
            "attempt": 1,
            "retry_reason": None,
            "last_output_at": None,
            "last_tool": None,
            "output_lines": 0,
        }

    def output(self, tool: str | None = None) -> None:
        """Note one agent output line (`tool` = already-redacted tool name, if the line had one)."""
        s = self.state
        s["output_lines"] += 1
        s["last_output_at"] = now_iso()
        if tool:
            s["last_tool"] = tool
        if s["phase"] == "setup":
            s["phase"] = "agent"
            self.flush()
        elif (
            self._last_write is None or self._clock() - self._last_write >= HEARTBEAT_THROTTLE_SECS
        ):
            self.flush()

    def set(self, **fields) -> None:
        """Change phase/attempt/retry_reason and write immediately."""
        self.state.update(fields)
        self.flush()

    def flush(self) -> None:
        try:
            path = progress_path(self._job_id, self._env)
            if path is not None:
                _atomic_write(
                    path, {**self.state, "updated_at": now_iso()}, prefix=".franky-progress-"
                )
            self._last_write = self._clock()
        except Exception:
            pass


def pid_start_marker(pid: int) -> str | None:
    """Process start time in clock ticks from /proc (Linux), else None (e.g. macOS)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def pid_alive(
    record: dict, *, kill=os.kill, marker_of=pid_start_marker, hostname=socket.gethostname
) -> bool | None:
    """Is the franky process that owns `record` still running? True / False / None (unknown).

    None for a legacy record with no pid or a run started on another host - we cannot probe
    it, so callers must not treat that as dead. A pid whose start marker changed was reused.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0 or record.get("host") != hostname():
        return None
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    except OSError:
        return None
    marker = record.get("pid_started_at")
    current = marker_of(pid) if marker else None
    return not (current is not None and current != marker)


def _secs_between(start: str | None, end: datetime) -> int | None:
    try:
        return max(0, int((end - datetime.fromisoformat(start)).total_seconds()))
    except (TypeError, ValueError):
        return None


def derive_liveness(
    record: dict,
    progress: dict | None,
    *,
    now: datetime,
    alive: bool | None,
    container: bool | None,
    snapshot_exists: bool,
) -> dict:
    """Turn a run record + heartbeat + probe results into `state` and one structured `next` step.

    Pure: `now`, the pid probe (`alive`), the docker probe (`container`) and the snapshot check
    are all injected. Each probe is True/False/None (None = could not tell).
    States: finished, orphaned (owner process confirmed gone while the record says running),
    unknown (a probe is uncertain - never guess), active (output in the last QUIET_AFTER_SECS),
    quiet (alive, silent longer than that).
    """
    progress = progress or {}
    status = record.get("status")
    running = status == "running"
    elapsed = _secs_between(record.get("started_at"), now if running else _end_of(record, now))
    idle = None
    if running:
        idle = _secs_between(progress.get("last_output_at") or record.get("started_at"), now)
    if not running:
        state = "finished"
    elif alive is None or container is None:
        state = "unknown"
    elif alive is False:
        state = "orphaned"
    else:
        state = "active" if idle is not None and idle < QUIET_AFTER_SECS else "quiet"
    return {
        "state": state,
        "elapsed_secs": elapsed,
        "idle_secs": idle,
        "phase": progress.get("phase"),
        "attempt": progress.get("attempt"),
        "retry_reason": progress.get("retry_reason"),
        "last_tool": progress.get("last_tool"),
        "next": _next_step(record, progress, state, now, snapshot_exists),
    }


def _end_of(record: dict, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(record.get("ended_at"))
    except (TypeError, ValueError):
        return default


def _next_step(
    record: dict, progress: dict, state: str, now: datetime, snapshot_exists: bool
) -> dict:
    """The single next action for a caller. One step at a time: never chains commands."""
    job_id = record.get("job_id", "")
    status_cmd = f"franky job status {job_id} --json"

    def step(action, command, retry_safe, why, wait=None):
        after = (now + timedelta(seconds=wait)).isoformat(timespec="seconds") if wait else None
        return {
            "action": action,
            "command": command,
            "retry_safe": retry_safe,
            "why": why,
            "check_after": after,
        }

    if state in ("active", "quiet"):
        return step(
            "wait",
            status_cmd,
            False,
            "the run is alive"
            + (" but silent" if state == "quiet" else "")
            + "; the watchdog stops it at --max-duration (default 30m)",
            60 if state == "quiet" else 120,
        )
    if state == "unknown":
        return step(
            "check",
            status_cmd,
            False,
            "cannot confirm whether the run is alive (other host, older record or docker error); "
            "do not kill or rerun on a guess",
            60,
        )
    if state == "orphaned":
        return step(
            "kill",
            f"franky job kill {job_id} --json",
            False,
            "the franky process that owns this run is gone; kill reaps its containers and "
            "snapshots the workspace. Run status again after the kill for the recovery step",
        )
    status = record.get("status")
    command = record.get("command")
    if status in _DONE_STATUSES:
        url = record.get("review_url") or record.get("pr_url")
        return step(
            "done", None, False, f"finished with status {status}" + (f": {url}" if url else "")
        )
    if (
        command in ("build", "resume", "replay")
        and record.get("snapshot_path")
        and snapshot_exists
        and not record.get(
            "no_publish"
        )  # resume would push and open a PR; a no-publish run never does
    ):
        return step(
            "resume",
            f"franky job resume {job_id} --json",
            True,
            f"status {status}; the workspace snapshot is intact, resume continues from it",
        )
    if command == "review-pr":
        # These statuses are set before review-pr reaches its publish step, so no review can
        # be on the PR. Decided from the final status, never from the best-effort heartbeat.
        if status in _REVIEW_PRE_PUBLISH_STATUSES:
            return step(
                "rerun",
                _review_rerun_command(record),
                True,
                f"status {status} before any publish step, so no review was posted; the command "
                "keeps the stored --expected-head-sha, --no-publish and --thread options, add any "
                "other original options",
            )
        return step(
            "inspect",
            f"franky job logs {job_id}",
            False,
            f"status {status}; a review may already be on the PR, check it before rerunning",
        )
    if command == "iterate":
        return step(
            "inspect",
            f"franky job logs {job_id}",
            False,
            f"status {status}; it may already have pushed, check the branch before any rerun",
        )
    return step("inspect", f"franky job logs {job_id}", False, f"status {status}; read the log")
