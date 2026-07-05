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

import franky.cli as cli
import franky.container as container
import franky.jobs as jobs
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
    assert any("rm -f franky-run-abc123" in c for c in flat)
    assert any("rm -f franky-proxy-abc123" in c for c in flat)
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


def _cli(monkeypatch, tmp_path, args):
    monkeypatch.setattr(cli.os, "environ", _env(tmp_path))
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
    monkeypatch.setattr(cli, "container_running", lambda name: True)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ccdd03"])
    assert res.exit_code == 0
    assert "ccdd03" in res.stdout
    assert "running" in res.stdout


def test_job_status_json_includes_live_flag(monkeypatch, tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec("ddee04"), env)
    monkeypatch.setattr(cli, "container_running", lambda name: False)
    res = _cli(monkeypatch, tmp_path, ["job", "status", "ddee04", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["container_running"] is False


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
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "ab0007"])
    assert res.exit_code == 0
    assert "killed" in res.stdout
    assert jobs.read_record("ab0007", env)["status"] == "killed"


def test_job_kill_finished_job_not_relabelled(monkeypatch, tmp_path):
    """Killing an already-finished job (no container to reap) must not overwrite its status."""
    env = _env(tmp_path)
    jobs.write_record(_rec("cd0008", status="pr_opened"), env)
    monkeypatch.setattr(cli, "reap_run", lambda job_id: False)
    res = _cli(monkeypatch, tmp_path, ["job", "kill", "cd0008"])
    assert res.exit_code == 0
    assert jobs.read_record("cd0008", env)["status"] == "pr_opened"  # unchanged


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
