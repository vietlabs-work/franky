"""Tests for the run registry + `franky jobs` / `franky job ...` (issue #63).

No docker, no network, no live creds: the registry is a filesystem module (pointed at a tmp dir
via FRANKY_RUNS_DIR), the container helpers take an injected runner, and the CLI is driven with
CliRunner. The load-bearing checks are (1) the on-disk record never contains a secret value and
(2) a user-supplied job id can't traverse out of the runs dir.

Job ids are always uuid hex (see jobs.new_job_id), so every id in these tests is [0-9a-f] -
non-hex ids exercise the path-traversal rejection on purpose.
"""

import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import franky.cli as cli
import franky.profile
import franky.container as container
import franky.jobs as jobs
import pytest
from click.testing import CliRunner


def _env(tmp_path):
    return {"FRANKY_RUNS_DIR": str(tmp_path / "runs")}


def _rec(job_id="abc123", status="running", started_at="2026-07-05T10:00:00+00:00"):
    # new_record always starts status=running (it's the initial record); override to simulate a
    # finished run.
    record = jobs.new_record(
        job_id=job_id,
        command="build",
        repo="o/r",
        engine="pi",
        task="do a thing",
        container=f"franky-run-{job_id}",
        network=f"franky-net-{job_id}",
        proxy=f"franky-proxy-{job_id}",
        branch="franky/thing",
        started_at=started_at,
    )
    record["status"] = status
    return record


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------


def test_write_read_roundtrip(tmp_path):
    env = _env(tmp_path)
    assert jobs.write_record(_rec("aaa111"), env) is True
    got = jobs.read_record("aaa111", env)
    assert got["job_id"] == "aaa111"
    assert got["status"] == "running"


def test_read_missing_returns_none(tmp_path):
    assert jobs.read_record("deadbeef", _env(tmp_path)) is None


def test_new_record_includes_null_diagnostics():
    # diagnostics (issue #69) starts null - it is populated by a diagnostics_sink only when the
    # container pass actually captures something.
    record = _rec()
    assert record["diagnostics"] is None


def test_new_record_includes_null_steer_notes():
    # steer_notes (issue #72) starts null - it is populated (best-effort) only when `franky job
    # attach` injects at least one correction while the run is live.
    record = _rec()
    assert record["steer_notes"] is None


def test_new_record_includes_null_replay_fields():
    # source/task_full/base_sha/replay_of (issue #70) all default to null when a caller (like
    # iterate/diagnose) never passes them - a record with no saved inputs cannot be replayed.
    record = _rec()
    assert record["source"] is None
    assert record["task_full"] is None
    assert record["base_sha"] is None
    assert record["replay_of"] is None


def test_new_record_carries_replay_fields_when_given():
    record = jobs.new_record(
        job_id="eee555",
        command="build",
        repo="o/r",
        engine="pi",
        task="do a thing",
        container="c",
        network="n",
        proxy="p",
        branch="franky/thing",
        started_at="2026-07-05T10:00:00+00:00",
        source="prose",
        task_full="do a thing in full",
        base_sha="abc1234",
        replay_of=None,
    )
    assert record["source"] == "prose"
    assert record["task_full"] == "do a thing in full"
    assert record["base_sha"] == "abc1234"
    assert record["replay_of"] is None


def test_read_malformed_returns_none(tmp_path):
    env = _env(tmp_path)
    jobs.runs_dir(env).mkdir(parents=True, exist_ok=True)
    (jobs.runs_dir(env) / "ccc333.json").write_text("{not json", encoding="utf-8")
    assert jobs.read_record("ccc333", env) is None


def test_update_record(tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ddd444"), env)
    assert jobs.update_record("ddd444", {"status": "pr_opened", "pr_url": "u"}, env) is True
    got = jobs.read_record("ddd444", env)
    assert got["status"] == "pr_opened"
    assert got["pr_url"] == "u"


def test_update_missing_returns_false(tmp_path):
    assert jobs.update_record("dead01", {"status": "x"}, _env(tmp_path)) is False


def test_list_records_newest_first(tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("aa1111", started_at="2026-07-01T00:00:00+00:00"), env)
    jobs.write_record(_rec("bb2222", started_at="2026-07-05T00:00:00+00:00"), env)
    ids = [r["job_id"] for r in jobs.list_records(env)]
    assert ids == ["bb2222", "aa1111"]


def test_list_skips_malformed(tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("c0ffee"), env)
    jobs.runs_dir(env).joinpath("junk.json").write_text("nope", encoding="utf-8")
    ids = [r["job_id"] for r in jobs.list_records(env)]
    assert ids == ["c0ffee"]


# ---------------------------------------------------------------------------
# Path-traversal safety
# ---------------------------------------------------------------------------


def test_unsafe_job_id_rejected(tmp_path):
    env = _env(tmp_path)
    # A traversal / non-hex id never resolves to a path -> read is None, write is False.
    assert jobs.read_record("../../etc/passwd", env) is None
    assert jobs.write_record({"job_id": "../evil"}, env) is False
    assert jobs.read_record("NotHex", env) is None


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_prune_removes_finished_tail(tmp_path):
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("aaa002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("aaa001", status="pr_opened", started_at=(now - timedelta(hours=1)).isoformat()), env
    )
    removed = jobs.prune(env, keep=1)
    remaining = {r["job_id"] for r in jobs.list_records(env)}
    assert remaining == {"aaa002"}  # newest kept, older finished one pruned
    assert removed == 1


def test_prune_keeps_fresh_running(tmp_path):
    """A genuinely in-flight (fresh) run in the prune tail is never removed."""
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("ddd002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("ccc001", status="running", started_at=(now - timedelta(hours=1)).isoformat()), env
    )
    removed = jobs.prune(env, keep=1)
    remaining = {r["job_id"] for r in jobs.list_records(env)}
    assert "ccc001" in remaining  # fresh running is protected even in the tail
    assert removed == 0


def test_prune_reclaims_stale_running(tmp_path):
    """A crash orphan (a 'running' record far older than any real run) becomes reclaimable so a
    crash loop can't grow the dir unbounded."""
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("eee002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("ff0001", status="running", started_at=(now - timedelta(hours=48)).isoformat()), env
    )
    removed = jobs.prune(env, keep=1)
    remaining = {r["job_id"] for r in jobs.list_records(env)}
    assert "ff0001" not in remaining  # stale orphan reclaimed
    assert removed == 1


# ---------------------------------------------------------------------------
# Redaction: the record must never carry a secret value
# ---------------------------------------------------------------------------


class _FakeEngine:
    name = "pi"


class _FakeCfg:
    engine = _FakeEngine()

    def secret_values(self):
        return ["gho_supersecret"]


class _Spec:
    def __init__(self, text, repo="o/r"):
        self.text = text
        self.repo = repo


def test_record_start_redacts_task_summary(tmp_path):
    env = _env(tmp_path)
    spec = _Spec("investigate gho_supersecret leaking " + "x" * 500)
    cli._record_run_start(
        "5ec123",
        command="build",
        cfg=_FakeCfg(),
        repo=spec.repo,
        summary=spec.text,
        branch=None,
        env=env,
    )
    record = jobs.read_record("5ec123", env)
    assert record is not None
    assert "gho_supersecret" not in json.dumps(record)  # secret scrubbed from the whole record
    assert len(record["task"]) <= cli._JOB_TASK_SUMMARY_MAX  # and truncated


def test_record_run_end_updates(tmp_path):
    from franky.economics import Usage

    env = _env(tmp_path)
    cli._record_run_start(
        "aced01", command="build", cfg=_FakeCfg(), repo="o/r", summary="t", branch=None, env=env
    )
    cli._record_run_end(
        "aced01",
        status="pr_opened",
        pr_url="https://example/pr/1",
        usage=Usage(),
        duration=1.5,
        exit_code=0,
        log_path="tasks/x.log",
        env=env,
    )
    record = jobs.read_record("aced01", env)
    assert record["status"] == "pr_opened"
    assert record["pr_url"] == "https://example/pr/1"
    assert record["exit_code"] == 0
    assert record["log_path"] == "tasks/x.log"
    assert record["ended_at"] is not None
    assert record["economics"]["duration_s"] == 1.5


def test_record_helpers_swallow_errors(tmp_path, monkeypatch):
    """A registry failure must never propagate into a build (best-effort contract)."""
    from franky.economics import Usage

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(jobs, "write_record", boom)
    monkeypatch.setattr(jobs, "update_record", boom)
    # Neither call raises despite the underlying registry ops blowing up.
    cli._record_run_start(
        "bad001",
        command="build",
        cfg=_FakeCfg(),
        repo="o/r",
        summary="t",
        branch=None,
        env=_env(tmp_path),
    )
    cli._record_run_end(
        "bad001",
        status="x",
        pr_url=None,
        usage=Usage(),
        duration=0.0,
        exit_code=1,
        log_path="",
        env=_env(tmp_path),
    )


def test_record_run_start_prunes(tmp_path, monkeypatch):
    """The write path (not any read) is what triggers pruning."""
    called = {}
    monkeypatch.setattr(jobs, "prune", lambda env=None, **k: called.setdefault("yes", True))
    cli._record_run_start(
        "cafe01",
        command="build",
        cfg=_FakeCfg(),
        repo="o/r",
        summary="t",
        branch=None,
        env=_env(tmp_path),
    )
    assert called.get("yes") is True


# ---------------------------------------------------------------------------
# container helpers
# ---------------------------------------------------------------------------


def test_run_names_prefixes():
    net, proxy, task = container.run_names("abc123")
    assert net == "franky-net-abc123"
    assert proxy == "franky-proxy-abc123"
    assert task == "franky-run-abc123"


def test_container_running_true_false():
    assert container.container_running(
        "x", runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "true\n", "")
    )
    assert not container.container_running(
        "x", runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "false\n", "")
    )
    # docker error / missing container -> not running (never raises).
    assert not container.container_running(
        "x", runner=lambda a, **k: (_ for _ in ()).throw(OSError())
    )


def test_reap_run_targets_all_three_by_id():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert container.reap_run("abc123", runner=runner) is True
    flat = [" ".join(a) for a in calls]
    assert any("rm -f -v franky-run-abc123" in c for c in flat)
    assert any("rm -f -v franky-proxy-abc123" in c for c in flat)
    assert any("network rm franky-net-abc123" in c for c in flat)


def test_run_in_container_run_id_pins_names():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    from tests.test_container import NOOP_SLEEP, _cfg, _orchestration_runner

    runner, calls = _orchestration_runner(task)
    container.run_in_container(
        _cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP, run_id="feedface1234"
    )
    # The task container is named from the run id.
    assert any(
        a[:2] == ["docker", "run"] and any("franky-run-feedface1234" in x for x in a) for a in calls
    )


# ---------------------------------------------------------------------------
# CLI: franky jobs / job status / logs / kill
# ---------------------------------------------------------------------------


def _cli(monkeypatch, tmp_path, args, extra=None):
    # `job kill` loads the config for its scrub secrets: never the developer's real config file.
    env = {**_env(tmp_path), "FRANKY_CONFIG_FILE": str(tmp_path / "no-config"), **(extra or {})}
    monkeypatch.setattr(cli.os, "environ", env)
    # ...nor the developer's real ~/.franky/profile.toml (the kill scrub reads MCP credentials).
    if cli.profile_path is franky.profile.profile_path:  # unless a test stubbed it already
        monkeypatch.setattr(cli, "profile_path", lambda *a, **k: None)
    return CliRunner().invoke(cli.main, args)


def test_jobs_list_empty(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["jobs"])
    assert res.exit_code == 0
    assert "no runs recorded" in res.stderr


def test_jobs_list_shows_records(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("aabb01", status="pr_opened"), env)
    res = _cli(monkeypatch, tmp_path, ["jobs"])
    assert res.exit_code == 0
    assert "aabb01" in res.stdout
    assert "pr_opened" in res.stdout


def test_jobs_list_json(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("bbcc02"), env)
    res = _cli(monkeypatch, tmp_path, ["jobs", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data[0]["job_id"] == "bbcc02"


def test_jobs_list_limit(monkeypatch, tmp_path):
    env = _env(tmp_path)
    for i in range(5):
        jobs.write_record(_rec(f"aa000{i}", started_at=f"2026-07-0{i + 1}T00:00:00+00:00"), env)
    res = _cli(monkeypatch, tmp_path, ["jobs", "-n", "2"])
    assert res.exit_code == 0
    # Only the 2 newest ids appear (aa0004, aa0003); the older three do not.
    assert res.stdout.count("\n") == 2
    assert "aa0004" in res.stdout and "aa0003" in res.stdout
    assert "aa0000" not in res.stdout


def test_job_status_found(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ccdd03"), env)
    monkeypatch.setattr(cli, "container_state", lambda name: True)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ccdd03"])
    assert res.exit_code == 0
    assert "ccdd03" in res.stdout
    assert "running" in res.stdout


def test_job_status_json_includes_live_flag(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ddee04"), env)
    monkeypatch.setattr(cli, "container_state", lambda name: False)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ddee04", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["container_running"] is False


def test_job_status_renders_diagnostics_block(monkeypatch, tmp_path):
    env = _env(tmp_path)
    rec = _rec("ee0100", status="killed")
    rec["diagnostics"] = {
        "task_exit_code": 137,
        "task_state": "exited",
        "oom_killed": True,
        "dind_ready": False,
        "tmpfs_full": True,
        "proxy_denied_count": 2,
        "egress_denied": [{"host": "evil.example.com", "count": 2}],
    }
    jobs.write_record(rec, env)
    monkeypatch.setattr(cli, "container_state", lambda name: False)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ee0100"])
    assert res.exit_code == 0
    assert "diagnostics:" in res.stdout
    assert "task_exit_code: 137" in res.stdout
    assert "oom_killed: True" in res.stdout
    assert "egress_denied: evil.example.com (x2)" in res.stdout


def test_job_status_json_includes_diagnostics(monkeypatch, tmp_path):
    env = _env(tmp_path)
    rec = _rec("ee0101", status="killed")
    rec["diagnostics"] = {"task_exit_code": 1, "oom_killed": False}
    jobs.write_record(rec, env)
    monkeypatch.setattr(cli, "container_state", lambda name: False)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ee0101", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["diagnostics"] == {"task_exit_code": 1, "oom_killed": False}


def test_job_status_not_found_exits_2(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["job", "status", "abcdef"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_status_unsafe_id_exits_2(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["job", "status", "../../etc/passwd"])
    assert res.exit_code == 2  # rejected as not-found, no traceback


def test_job_logs_prints_file(monkeypatch, tmp_path):
    env = _env(tmp_path)
    log = tmp_path / "run.log"
    log.write_text("transcript here\n", encoding="utf-8")
    rec = _rec("eeff05", status="pr_opened")
    rec["log_path"] = str(log)
    jobs.write_record(rec, env)
    res = _cli(monkeypatch, tmp_path, ["job", "logs", "eeff05"])
    assert res.exit_code == 0
    assert "transcript here" in res.stdout


def test_job_logs_running_has_no_log(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ff0006", status="running"), env)  # log_path == ""
    res = _cli(monkeypatch, tmp_path, ["job", "logs", "ff0006"])
    assert res.exit_code == 2
    assert "no log available" in res.stderr


def test_job_kill_reaps_and_marks_killed(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ab0007", status="running"), env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: True)
    # No-op capture so the running-record path never shells out to real docker (issue #69).
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: {})
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0007"])
    assert res.exit_code == 0
    assert "killed" in res.stdout
    assert jobs.read_record("ab0007", env)["status"] == "killed"


def test_job_kill_finished_job_not_relabelled(monkeypatch, tmp_path):
    """Killing an already-finished job (no container to reap) must not overwrite its status."""
    env = _env(tmp_path)
    jobs.write_record(_rec("cd0008", status="pr_opened"), env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: False)
    # A finished record must NOT trigger a capture (its containers are already gone); patch to a
    # raiser to prove the gate skips it entirely (issue #69).
    monkeypatch.setattr(
        cli, "capture_diagnostics", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called"))
    )
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "cd0008"])
    assert res.exit_code == 0
    assert jobs.read_record("cd0008", env)["status"] == "pr_opened"  # unchanged


def test_job_kill_captures_diagnostics_for_running(monkeypatch, tmp_path):
    """A running run's kill captures diagnostics (issue #69) and persists them on the record."""
    env = _env(tmp_path)
    jobs.write_record(_rec("ab0010", status="running"), env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: True)
    canned = {"task_exit_code": 137, "oom_killed": True, "task_state": "exited"}
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: canned)
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0010"])
    assert res.exit_code == 0
    persisted = jobs.read_record("ab0010", env)
    assert persisted["status"] == "killed"
    assert persisted["diagnostics"] == canned


def test_job_kill_swallows_capture_failure(monkeypatch, tmp_path):
    """A raising capture must never break the kill: the run still ends up killed (issue #69)."""
    env = _env(tmp_path)
    jobs.write_record(_rec("ab0011", status="running"), env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: True)
    monkeypatch.setattr(
        cli, "capture_diagnostics", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0011"])
    assert res.exit_code == 0
    persisted = jobs.read_record("ab0011", env)
    assert persisted["status"] == "killed"
    assert persisted["diagnostics"] is None  # capture failed -> no diagnostics patch written


def test_job_kill_not_found_exits_2(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "abc999"])
    assert res.exit_code == 2


# ---------------------------------------------------------------------------
# compute_stats (issue #64 item #7)
# ---------------------------------------------------------------------------


def _rec_econ(job_id, status, *, engine="pi", repo="o/r", cost=0.5, dur=100.0, started_at=None):
    """A finished record with an economics block + overridable engine/repo, for stats tests."""
    r = _rec(job_id, status=status, started_at=started_at or "2026-07-05T10:00:00+00:00")
    r["engine"] = engine
    r["repo"] = repo
    r["economics"] = {"tokens_in": 1, "tokens_out": 2, "cost_usd": cost, "duration_s": dur}
    return r


def test_compute_stats_empty_is_none_safe():
    stats = jobs.compute_stats([])
    assert stats["total"] == 0
    assert stats["by_status"] == {}
    assert stats["success_rate"] is None
    assert stats["median_duration_s"] is None
    assert stats["total_cost_usd"] is None
    assert stats["hangs"] == 0
    assert stats["running_fresh"] == 0
    assert stats["by_engine"] == {} and stats["by_repo"] == {}


def test_compute_stats_rates_and_medians():
    now = datetime.now(timezone.utc)
    records = [
        _rec_econ("aa0001", "pr_opened", dur=100.0, cost=0.5),
        _rec_econ("aa0002", "iterate_complete", dur=200.0, cost=1.0),
        _rec_econ("aa0003", "no_pr", dur=300.0, cost=1.5),
        _rec_econ("aa0004", "agent_error", dur=400.0, cost=2.0),
        _rec("aa0005", status="running", started_at=now.isoformat()),  # fresh, no econ
    ]
    stats = jobs.compute_stats(records)
    assert stats["success"] == 2 and stats["failed"] == 2
    assert stats["success_rate"] == 0.5  # 2 / (2+2), the running one is excluded
    assert stats["running_fresh"] == 1
    assert stats["median_duration_s"] == 250.0  # median(100,200,300,400)
    assert stats["total_cost_usd"] == 5.0
    assert stats["hangs"] == 0  # no timeout, and the only running record is fresh
    assert stats["by_status"]["pr_opened"] == 1 and stats["by_status"]["running"] == 1


def test_compute_stats_hangs_counts_timeout_and_stale_running():
    now = datetime.now(timezone.utc)
    records = [
        _rec_econ("bb0001", "timeout"),
        _rec("bb0002", status="running", started_at=(now - timedelta(hours=48)).isoformat()),
        _rec("bb0003", status="running", started_at=now.isoformat()),
    ]
    stats = jobs.compute_stats(records)
    assert stats["running_fresh"] == 1  # only the recent running record
    assert stats["hangs"] == 2  # timeout (1) + stale-running (1); the fresh one is not a hang
    assert stats["failed"] == 1  # timeout is a terminal failure


def test_compute_stats_classifies_review_pr_statuses():
    # review_published/review_complete are review-pr successes; no_findings,
    # publish_blocked_stale_head, and publish_failed are review-pr failures (#F7/#F8/#F9).
    records = [
        _rec_econ("dd0001", "review_published"),
        _rec_econ("dd0002", "review_complete"),
        _rec_econ("dd0003", "no_findings"),
        _rec_econ("dd0004", "publish_blocked_stale_head"),
        _rec_econ("dd0005", "publish_failed"),
    ]
    stats = jobs.compute_stats(records)
    assert stats["success"] == 2
    assert stats["failed"] == 3


def test_compute_stats_breakdown_by_engine_and_repo():
    records = [
        _rec_econ("cc0001", "pr_opened", engine="pi", repo="o/r", dur=10.0, cost=1.0),
        _rec_econ("cc0002", "no_pr", engine="claude", repo="o/r", dur=20.0, cost=2.0),
        _rec_econ("cc0003", "pr_opened", engine="pi", repo="x/y", dur=30.0, cost=3.0),
    ]
    stats = jobs.compute_stats(records)
    pi = stats["by_engine"]["pi"]
    assert pi["n"] == 2 and pi["success"] == 2 and pi["success_rate"] == 1.0
    assert pi["median_duration_s"] == 20.0 and pi["total_cost_usd"] == 4.0
    claude = stats["by_engine"]["claude"]
    assert claude["success_rate"] == 0.0 and claude["total_cost_usd"] == 2.0
    assert stats["by_repo"]["o/r"]["n"] == 2 and stats["by_repo"]["o/r"]["success_rate"] == 0.5
    assert stats["by_repo"]["x/y"]["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# export_bundle (issue #64 item #6)
# ---------------------------------------------------------------------------


def test_export_bundle_contains_record_and_transcript(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("redacted transcript\n", encoding="utf-8")
    rec = _rec("da7a01", status="pr_opened")
    rec["log_path"] = str(log)
    dest = tmp_path / "bundle.tar.gz"

    summary = jobs.export_bundle(rec, dest)
    assert summary["included"] == ["record.json", "transcript.log"]
    assert summary["output_path"] == str(dest)
    assert summary["bytes"] > 0 and dest.exists()

    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        assert names == ["record.json", "transcript.log"]
        record_member = tar.getmember("record.json")
        # Deterministic, host-free member metadata (no uid/username/mtime leak).
        assert record_member.mode == 0o600
        assert record_member.mtime == 0
        assert record_member.uid == 0 and record_member.uname == ""
        assert json.loads(tar.extractfile("record.json").read())["job_id"] == "da7a01"
        assert tar.extractfile("transcript.log").read() == b"redacted transcript\n"


def test_export_bundle_omits_missing_transcript(tmp_path):
    rec = _rec("da7a02", status="running")  # log_path == ""
    dest = tmp_path / "bundle.tar.gz"
    summary = jobs.export_bundle(rec, dest)
    assert summary["included"] == ["record.json"]
    with tarfile.open(dest, "r:gz") as tar:
        assert tar.getnames() == ["record.json"]


def test_export_bundle_skips_unreadable_transcript(tmp_path):
    # log_path points at a directory: .exists() is True but read_bytes() raises IsADirectoryError
    # (an OSError) - the transcript is skipped, record.json still bundled, no raise.
    rec = _rec("da7a04", status="pr_opened")
    rec["log_path"] = str(tmp_path)  # a directory, not a file
    dest = tmp_path / "bundle.tar.gz"
    summary = jobs.export_bundle(rec, dest)
    assert summary["included"] == ["record.json"]


def test_export_bundle_creates_parent_dirs(tmp_path):
    rec = _rec("da7a03", status="pr_opened")
    dest = tmp_path / "nested" / "deep" / "b.tar.gz"
    summary = jobs.export_bundle(rec, dest)
    assert dest.exists() and summary["included"] == ["record.json"]


# ---------------------------------------------------------------------------
# CLI: franky jobs --stats / franky job export
# ---------------------------------------------------------------------------


def test_jobs_stats_prose(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec_econ("aade01", "pr_opened"), env)
    jobs.write_record(_rec_econ("aade02", "no_pr"), env)
    res = _cli(monkeypatch, tmp_path, ["jobs", "--stats"])
    assert res.exit_code == 0
    assert "runs:" in res.stdout and "success:" in res.stdout and "hangs:" in res.stdout


def test_jobs_stats_json(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec_econ("bbde01", "pr_opened"), env)
    res = _cli(monkeypatch, tmp_path, ["jobs", "--stats", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["total"] == 1 and data["success"] == 1
    assert "by_engine" in data and "hangs" in data


def test_jobs_stats_ignores_limit(monkeypatch, tmp_path):
    env = _env(tmp_path)
    for i in range(3):
        jobs.write_record(_rec_econ(f"ccde0{i}", "pr_opened"), env)
    # -n 1 would cap the LIST to 1, but --stats must aggregate over ALL 3 records.
    res = _cli(monkeypatch, tmp_path, ["jobs", "--stats", "--json", "-n", "1"])
    assert json.loads(res.stdout)["total"] == 3


def test_jobs_stats_empty(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["jobs", "--stats"])
    assert res.exit_code == 0
    assert "runs:" in res.stdout and "0" in res.stdout


def test_job_export_creates_bundle(monkeypatch, tmp_path):
    env = _env(tmp_path)
    log = tmp_path / "r.log"
    log.write_text("hi\n", encoding="utf-8")
    rec = _rec("ee0f01", status="pr_opened")
    rec["log_path"] = str(log)
    jobs.write_record(rec, env)
    dest = tmp_path / "out.tar.gz"
    res = _cli(monkeypatch, tmp_path, ["job", "export", "ee0f01", "-o", str(dest)])
    assert res.exit_code == 0
    assert "exported job ee0f01" in res.stdout and dest.exists()


def test_job_export_json(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ee0f02", status="pr_opened"), env)
    dest = tmp_path / "out.tar.gz"
    res = _cli(monkeypatch, tmp_path, ["job", "export", "ee0f02", "-o", str(dest), "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["job_id"] == "ee0f02" and data["included"] == ["record.json"]
    assert data["output_path"] == str(dest) and data["bytes"] > 0


def test_job_export_default_path(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ee0f03", status="pr_opened"), env)
    monkeypatch.setattr(cli.os, "environ", env)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["job", "export", "ee0f03", "--json"])
        assert res.exit_code == 0
        data = json.loads(res.stdout)
        assert data["output_path"].endswith("franky-job-ee0f03.tar.gz")
        from pathlib import Path

        assert Path(data["output_path"]).exists()


def test_job_export_not_found_exits_2(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["job", "export", "abcdef"])
    assert res.exit_code == 2
    assert "no run found" in res.stderr


def test_job_export_write_failure_exits_2(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ee0f04", status="pr_opened"), env)

    def boom(record, dest):
        raise OSError("disk full")

    monkeypatch.setattr(cli.jobs, "export_bundle", boom)
    res = _cli(monkeypatch, tmp_path, ["job", "export", "ee0f04", "-o", str(tmp_path / "x.tar.gz")])
    assert res.exit_code == 2
    assert "could not write export bundle" in res.stderr


def test_job_export_write_failure_json_kind(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ee0f05", status="pr_opened"), env)
    monkeypatch.setattr(cli.jobs, "export_bundle", lambda r, d: (_ for _ in ()).throw(OSError("x")))
    res = _cli(
        monkeypatch, tmp_path, ["job", "export", "ee0f05", "-o", str(tmp_path / "x"), "--json"]
    )
    assert res.exit_code == 2
    assert json.loads(res.stdout)["error"]["kind"] == "export_failed"


# ---------------------------------------------------------------------------
# Workspace snapshot sidecar (issue #71)
# ---------------------------------------------------------------------------


def test_new_record_includes_null_resume_fields():
    # resumed_from + snapshot_path (issue #71) default null: a plain build/iterate record carries
    # neither until a resume run / a timeout snapshot populates them.
    record = _rec()
    assert record["resumed_from"] is None
    assert record["snapshot_path"] is None


def test_new_record_carries_resumed_from_when_given():
    record = jobs.new_record(
        job_id="ab12cd",
        command="resume",
        repo="o/r",
        engine="pi",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch="franky/thing",
        started_at="2026-07-05T10:00:00+00:00",
        resumed_from="deadbeef",
    )
    assert record["resumed_from"] == "deadbeef"
    assert record["snapshot_path"] is None


def test_prune_unlinks_snapshot_sidecar_with_record(tmp_path):
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("aa0002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("aa0001", status="pr_opened", started_at=(now - timedelta(hours=1)).isoformat()), env
    )
    # Give the older run a snapshot sidecar; it must be reaped alongside its record.
    sidecar = jobs.runs_dir(env) / "aa0001.snapshot.tar.gz"
    sidecar.write_bytes(b"fake snapshot")
    jobs.prune(env, keep=1)
    assert not (jobs.runs_dir(env) / "aa0001.json").exists()
    assert not sidecar.exists()


def test_prune_sweeps_orphan_snapshot(tmp_path):
    env = _env(tmp_path)
    jobs.runs_dir(env).mkdir(parents=True, exist_ok=True)
    # A snapshot with NO matching record (a failed record-write / already-pruned record) is swept.
    orphan = jobs.runs_dir(env) / "0badf00d.snapshot.tar.gz"
    orphan.write_bytes(b"orphan")
    jobs.prune(env)
    assert not orphan.exists()


def test_prune_keeps_fresh_running_snapshot(tmp_path):
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    # A fresh running record's snapshot must survive the orphan sweep (its record is live).
    jobs.write_record(_rec("cc0001", status="running", started_at=now.isoformat()), env)
    sidecar = jobs.runs_dir(env) / "cc0001.snapshot.tar.gz"
    sidecar.write_bytes(b"in flight")
    jobs.prune(env)
    assert sidecar.exists()


def test_export_bundle_excludes_snapshot_sidecar(tmp_path):
    # A snapshot tar sitting beside the record must NEVER be packed into the export bundle - it is
    # a host-local resume artifact that may contain workspace bytes (issue #71).
    env = _env(tmp_path)
    rec = _rec("da7a05", status="killed")
    jobs.write_record(rec, env)
    (jobs.runs_dir(env) / "da7a05.snapshot.tar.gz").write_bytes(b"workspace bytes")
    dest = tmp_path / "bundle.tar.gz"
    summary = jobs.export_bundle(rec, dest)
    assert "snapshot" not in " ".join(summary["included"])
    with tarfile.open(dest, "r:gz") as tar:
        assert all("snapshot" not in n for n in tar.getnames())


def test_job_kill_snapshots_workspace_before_reap(monkeypatch, tmp_path):
    """A running run's kill captures a workspace snapshot (issue #71) BEFORE reap and records
    its path."""
    env = _env(tmp_path)
    jobs.write_record(_rec("ab0099", status="running"), env)
    order = []
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(cli, "reap_run", lambda job_id: (order.append("reap"), True)[1])

    def fake_snapshot_workspace(container, dest, secrets, runner, **kwargs):
        order.append("snapshot")
        return str(dest)

    monkeypatch.setattr(cli.snapshot, "snapshot_workspace", fake_snapshot_workspace)
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0099"])
    assert res.exit_code == 0
    # Snapshot must be captured while the container is still alive - before reap_run.
    assert order == ["snapshot", "reap"]
    persisted = jobs.read_record("ab0099", env)
    assert persisted["status"] == "killed"
    assert persisted["snapshot_path"] == str(jobs.runs_dir(env) / "ab0099.snapshot.tar.gz")


def test_job_kill_iterate_running_does_not_snapshot(monkeypatch, tmp_path):
    """An iterate run has no resumable workspace, so killing it captures diagnostics but must NOT
    call snapshot_workspace (issue #71)."""
    env = _env(tmp_path)
    rec = _rec("ab00aa", status="running")
    rec["command"] = "iterate"
    jobs.write_record(rec, env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: True)
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(
        cli.snapshot,
        "snapshot_workspace",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("snapshot_workspace called")),
    )
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab00aa"])
    assert res.exit_code == 0
    persisted = jobs.read_record("ab00aa", env)
    assert persisted["status"] == "killed"
    assert persisted["snapshot_path"] is None


def test_new_record_adds_thread_id_only_when_given():
    # thread_id names the `review-pr --thread` thread a run belongs to; every other record keeps
    # exactly its previous keys.
    assert "thread_id" not in _rec()
    record = jobs.new_record(
        job_id="abc",
        command="review-pr",
        repo="o/r",
        engine="claude",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch=None,
        started_at="2026-07-05T10:00:00+00:00",
        thread_id="o__r__1__reviewer",
    )
    assert record["thread_id"] == "o__r__1__reviewer"


def test_new_record_adds_session_keys_only_when_a_session_is_set():
    assert not {"session_id", "model", "session_path", "thread_bound"} & set(_rec())
    record = jobs.new_record(
        job_id="abc",
        command="build",
        repo="o/r",
        engine="claude",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch=None,
        started_at="2026-07-05T10:00:00+00:00",
        session_id="11111111-2222-3333-4444-555555555555",
        model="opus",
    )
    assert record["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert record["model"] == "opus"


def test_prune_removes_session_sidecars_with_the_record_and_as_orphans(tmp_path):
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("aa0002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("aa0001", status="timeout", started_at=(now - timedelta(hours=1)).isoformat()), env
    )
    runs = jobs.runs_dir(env)
    kept = runs / "aa0002.session.tar.gz"
    pruned = runs / "aa0001.session.tar.gz"
    orphan = runs / "0badf00d.session.tar.gz"
    for path in (kept, pruned, orphan):
        path.write_bytes(b"session")
    jobs.prune(env, keep=1)
    assert kept.exists() and not pruned.exists() and not orphan.exists()


def test_export_bundle_excludes_the_session_sidecar(tmp_path):
    env = _env(tmp_path)
    rec = _rec("da7a06", status="timeout")
    rec["session_path"] = str(jobs.runs_dir(env) / "da7a06.session.tar.gz")
    jobs.write_record(rec, env)
    (jobs.runs_dir(env) / "da7a06.session.tar.gz").write_bytes(b"session bytes")
    dest = tmp_path / "bundle.tar.gz"
    assert jobs.export_bundle(rec, dest)["included"] == ["record.json"]
    with tarfile.open(dest, "r:gz") as tar:
        assert tar.getnames() == ["record.json"]


SID = "11111111-2222-3333-4444-555555555555"
KILL_ENV = {
    "FRANKY_ALLOWED_REPOS": "o/r",
    "GH_TOKEN": "ghp_kill",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-kill-secret",
}


def _threaded_running(env, job_id):
    rec = _rec(job_id, status="running")
    rec.update(engine="claude", session_id=SID, model=None)
    jobs.write_record(rec, env)


def _kill_session_env(monkeypatch, order=None, seen=None):
    order = [] if order is None else order
    seen = {} if seen is None else seen
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(cli, "reap_run", lambda job_id: (order.append("reap"), True)[1])
    monkeypatch.setattr(
        cli.snapshot,
        "snapshot_workspace",
        lambda container, dest, secrets, runner, **k: seen.update(workspace=secrets),
    )

    def fake_copy(container, paths, dest, **kwargs):
        order.append("copy")
        seen.update(paths=paths, container=container, dest=dest)
        Path(dest, paths[0]).parent.mkdir(parents=True)
        Path(dest, paths[0]).write_text("{}\n")
        return "ok"

    real_finalize = cli.snapshot.finalize_snapshot

    def fake_finalize(src, dest, secrets, *a, **k):
        order.append("finalize")
        seen.update(session=secrets)
        return real_finalize(src, dest, secrets, *a, **k)

    monkeypatch.setattr(cli.snapshot, "copy_session", fake_copy)
    monkeypatch.setattr(cli.snapshot, "finalize_snapshot", fake_finalize)
    return order, seen


def _mcp_profile(monkeypatch, value):
    """Stub the profile loaders: one MCP credential, or a profile that fails to load."""
    monkeypatch.setattr(cli, "profile_path", lambda *a, **k: Path("/nonexistent/profile.toml"))
    if value is None:
        monkeypatch.setattr(cli, "load_profile", lambda path: (_ for _ in ()).throw(ValueError()))
    else:
        monkeypatch.setattr(cli, "load_profile", lambda path: object())
        monkeypatch.setattr(cli, "resolve_mcp_credentials", lambda spec, env: {"MCP_T": value})


def test_job_kill_copies_before_the_reap_and_finalizes_after_with_the_full_secret_set(
    monkeypatch, tmp_path
):
    env = _env(tmp_path)
    _threaded_running(env, "ab0100")
    order, seen = _kill_session_env(monkeypatch)
    _mcp_profile(monkeypatch, "mcp-kill-secret")
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0100"], KILL_ENV)
    assert res.exit_code == 0, res.output
    assert order == ["copy", "reap", "finalize"]
    assert seen["paths"] == [f".claude/projects/-work/{SID}.jsonl", f".claude/projects/-work/{SID}"]
    # The temp copy lives in the runs dir under the swept prefix, and is gone afterwards.
    assert Path(seen["dest"]).parent == jobs.runs_dir(env)
    assert Path(seen["dest"]).name.startswith(".tmp-session-") and not Path(seen["dest"]).exists()
    # The config's secrets, the profile's MCP credentials, and the env's secret keys.
    for key in ("session", "workspace"):
        assert {"ghp_kill", "claude-kill-secret", "mcp-kill-secret"} <= set(seen[key])
    persisted = jobs.read_record("ab0100", env)
    assert persisted["session_path"] == str(jobs.runs_dir(env) / "ab0100.session.tar.gz")
    assert (jobs.runs_dir(env) / "ab0100.session.tar.gz").is_file()


@pytest.mark.parametrize("broken", ["config", "profile"])
def test_job_kill_skips_the_session_capture_without_the_full_secret_set(
    monkeypatch, tmp_path, broken
):
    env = _env(tmp_path)
    _threaded_running(env, "ab0101")
    order, _seen = _kill_session_env(monkeypatch)
    extra = KILL_ENV
    if broken == "config":
        extra = {}  # no allowlist -> the config cannot load
    else:
        _mcp_profile(monkeypatch, None)
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0101"], extra)
    assert res.exit_code == 0, res.output
    assert order == ["reap"]
    assert "session_path" not in jobs.read_record("ab0101", env)


def test_job_kill_session_copy_failure_records_nothing(monkeypatch, tmp_path):
    env = _env(tmp_path)
    _threaded_running(env, "ab0102")
    monkeypatch.setattr(cli, "capture_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(cli, "reap_run", lambda job_id: True)
    monkeypatch.setattr(cli.snapshot, "snapshot_workspace", lambda *a, **k: None)
    monkeypatch.setattr(cli.snapshot, "copy_home_path", lambda *a, **k: ("failed", 0))
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0102"], KILL_ENV)
    assert res.exit_code == 0, res.output
    persisted = jobs.read_record("ab0102", env)
    assert persisted["status"] == "killed" and "session_path" not in persisted
    assert not (jobs.runs_dir(env) / "ab0102.session.tar.gz").exists()
    assert not list(jobs.runs_dir(env).glob(".tmp-*"))


def test_prune_sweeps_stale_temp_session_leftovers_only(tmp_path):
    import os

    env = _env(tmp_path)
    runs = jobs.runs_dir(env)
    runs.mkdir(parents=True)
    stale_dir, stale_tar, young = (
        runs / ".tmp-session-a",
        runs / ".tmp-pack-b.tar.gz",
        runs / ".tmp-session-c",
    )
    stale_dir.mkdir()
    (stale_dir / "s.jsonl").write_text("x")
    stale_tar.write_bytes(b"x")
    young.mkdir()
    old = datetime.now(timezone.utc).timestamp() - jobs.STALE_RUNNING_SECS - 60
    for path in (stale_dir, stale_tar):
        os.utime(path, (old, old))
    jobs.prune(env)
    assert not stale_dir.exists() and not stale_tar.exists() and young.exists()


def test_new_record_marks_threaded_runs_only():
    assert "threaded" not in _rec()
    record = jobs.new_record(
        job_id="abc",
        command="build",
        repo="o/r",
        engine="pi",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch=None,
        started_at="2026-07-05T10:00:00+00:00",
        threaded=True,
    )
    assert record["threaded"] is True and "session_id" not in record


def test_new_record_marks_a_no_publish_run_only_when_asked():
    assert "no_publish" not in _rec()
    record = jobs.new_record(
        job_id="ab12cd",
        command="build",
        repo="o/r",
        engine="pi",
        task="t",
        container="c",
        network="n",
        proxy="p",
        branch="franky/thing",
        started_at="2026-07-05T10:00:00+00:00",
        no_publish=True,
    )
    assert record["no_publish"] is True


def test_prune_unlinks_the_bundle_sidecar_with_its_record_and_sweeps_orphans(tmp_path):
    env = _env(tmp_path)
    now = datetime.now(timezone.utc)
    jobs.write_record(_rec("aa0002", status="pr_opened", started_at=now.isoformat()), env)
    jobs.write_record(
        _rec("aa0001", status="pr_opened", started_at=(now - timedelta(hours=1)).isoformat()), env
    )
    old = jobs.runs_dir(env) / "aa0001.bundle"
    old.write_bytes(b"bundle")
    orphan = jobs.runs_dir(env) / "0badf00d.bundle"
    orphan.write_bytes(b"orphan")
    jobs.prune(env, keep=1)
    assert not old.exists() and not orphan.exists()


def test_a_fresh_running_runs_bundle_survives_prune(tmp_path):
    env = _env(tmp_path)
    jobs.write_record(
        _rec("cc0001", status="running", started_at=datetime.now(timezone.utc).isoformat()), env
    )
    sidecar = jobs.runs_dir(env) / "cc0001.bundle"
    sidecar.write_bytes(b"in flight")
    jobs.prune(env)
    assert sidecar.exists()


def test_export_bundle_excludes_the_git_bundle_sidecar(tmp_path):
    env = _env(tmp_path)
    rec = _rec("da7a06", status="branch_ready")
    jobs.write_record(rec, env)
    (jobs.runs_dir(env) / "da7a06.bundle").write_bytes(b"workspace commits")
    dest = tmp_path / "bundle.tar.gz"
    jobs.export_bundle(rec, dest)
    import tarfile

    with tarfile.open(dest) as tar:
        assert not any(n.endswith(".bundle") for n in tar.getnames())
