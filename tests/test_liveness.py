"""Tests for run liveness: the start event, the heartbeat sidecar and `job status` state/next.

No docker, network or credentials: probes, clocks and the container runner are injected.
"""

import json
from datetime import datetime, timedelta, timezone

import franky.cli as cli
import franky.jobs as jobs
import pytest
from click.testing import CliRunner
from franky.engine import tool_name

NOW = datetime(2026, 7, 5, 10, 10, 0, tzinfo=timezone.utc)
STARTED = "2026-07-05T10:00:00+00:00"


def _env(tmp_path):
    return {"FRANKY_RUNS_DIR": str(tmp_path / "runs")}


def _rec(job_id="abc123", status="running", command="build", **extra):
    record = jobs.new_record(
        job_id=job_id,
        command=command,
        repo="o/r",
        engine="pi",
        task="t",
        container=f"franky-run-{job_id}",
        network="n",
        proxy="p",
        branch=None,
        started_at=STARTED,
    )
    record["status"] = status
    record.update(extra)
    return record


def _derive(record, progress=None, *, alive=True, container=True, snap=False):
    return jobs.derive_liveness(
        record, progress, now=NOW, alive=alive, container=container, snapshot_exists=snap
    )


def _out(last_output_secs_ago):
    at = (NOW - timedelta(seconds=last_output_secs_ago)).isoformat()
    return {"phase": "agent", "attempt": 1, "last_output_at": at, "last_tool": "bash"}


# ---------------------------------------------------------------------------
# derive_liveness: state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,alive,container,progress,state",
    [
        ("pr_opened", True, True, None, "finished"),
        ("running", False, True, None, "orphaned"),
        ("running", False, None, None, "unknown"),  # docker unsure: never a destructive next
        ("running", None, True, None, "unknown"),
        ("running", True, None, None, "unknown"),
        ("running", True, True, _out(30), "active"),
        ("running", True, False, _out(30), "active"),  # container gone between phases is fine
        ("running", True, True, _out(120), "quiet"),
        ("running", True, True, None, "quiet"),  # 600s since start, no output yet
    ],
)
def test_state_table(status, alive, container, progress, state):
    live = _derive(_rec(status=status), progress, alive=alive, container=container)
    assert live["state"] == state


def test_idle_and_elapsed_and_progress_fields():
    live = _derive(_rec(), {**_out(45), "attempt": 2, "retry_reason": "resume_failed"})
    assert live["elapsed_secs"] == 600
    assert live["idle_secs"] == 45
    assert (live["phase"], live["attempt"], live["retry_reason"], live["last_tool"]) == (
        "agent",
        2,
        "resume_failed",
        "bash",
    )
    finished = _derive(_rec(status="timeout", ended_at="2026-07-05T10:05:00+00:00"))
    assert finished["elapsed_secs"] == 300
    assert finished["idle_secs"] is None


# ---------------------------------------------------------------------------
# derive_liveness: next
# ---------------------------------------------------------------------------


def _next(record, progress=None, **kw):
    return _derive(record, progress, **kw)["next"]


def test_next_wait_when_alive():
    active = _next(_rec(), _out(10))
    quiet = _next(_rec(), _out(500))
    for nxt in (active, quiet):
        assert nxt["action"] == "wait"
        assert nxt["command"] == "franky job status abc123 --json"
        assert nxt["retry_safe"] is False
        assert "--max-duration" in nxt["why"]
    assert active["check_after"] == (NOW + timedelta(seconds=120)).isoformat(timespec="seconds")
    assert quiet["check_after"] == (NOW + timedelta(seconds=60)).isoformat(timespec="seconds")


def test_next_check_when_unknown_never_kill_or_rerun():
    nxt = _next(_rec(), alive=None)
    assert (nxt["action"], nxt["retry_safe"]) == ("check", False)
    assert "kill" in nxt["why"] and "rerun" in nxt["why"]


def test_next_kill_when_orphaned_is_one_step():
    nxt = _next(_rec(), alive=False)
    assert nxt["action"] == "kill"
    assert nxt["command"] == "franky job kill abc123 --json"
    assert "&&" not in nxt["command"]
    assert nxt["retry_safe"] is False


def test_next_done_names_the_result_url():
    nxt = _next(_rec(status="pr_opened", pr_url="https://github.com/o/r/pull/1"))
    assert (nxt["action"], nxt["command"]) == ("done", None)
    assert "https://github.com/o/r/pull/1" in nxt["why"]
    review = _next(_rec(status="review_published", command="review-pr", review_url="https://x/r"))
    assert "https://x/r" in review["why"]


def test_next_resume_needs_an_existing_snapshot():
    record = _rec(status="timeout", snapshot_path="/x/abc123.snapshot.tar.gz")
    nxt = _next(record, snap=True)
    assert (nxt["action"], nxt["command"], nxt["retry_safe"]) == (
        "resume",
        "franky job resume abc123 --json",
        True,
    )
    assert _next(record, snap=False)["action"] == "inspect"


def test_next_never_offers_resume_for_a_no_publish_build():
    record = _rec(status="timeout", snapshot_path="/x/abc123.snapshot.tar.gz", no_publish=True)
    assert _next(record, snap=True)["action"] == "inspect"
    assert _next(_rec(status="branch_ready", no_publish=True))["action"] == "done"


def test_next_review_rerun_only_for_pre_publish_statuses():
    url = "https://github.com/o/r/pull/9"
    for status in ("timeout", "agent_error", "no_findings"):
        # The heartbeat phase is ignored: a failed sidecar write must not make a rerun unsafe.
        nxt = _next(_rec(status=status, command="review-pr", pr_url=url), {"phase": "publish"})
        assert (nxt["action"], nxt["retry_safe"]) == ("rerun", True)
        assert nxt["command"] == f"franky review-pr {url} --json"
    for status in ("killed", "publish_failed", "publish_blocked_stale_head"):
        nxt = _next(_rec(status=status, command="review-pr", pr_url=url), {"phase": "agent"})
        assert (nxt["action"], nxt["retry_safe"]) == ("inspect", False)
        assert "already be on the PR" in nxt["why"]


def test_next_review_rerun_keeps_publish_guards():
    # A timed-out orphan never set pr_url: fall back to the task URL, keep the guard options.
    record = _rec(
        status="timeout",
        command="review-pr",
        task="https://github.com/o/r/pull/9",
        reviewed_sha="a" * 40,
        no_publish=True,
        thread_id="t1",
    )
    assert _next(record)["command"] == (
        f"franky review-pr https://github.com/o/r/pull/9 --expected-head-sha {'a' * 40} "
        "--no-publish --thread --json"
    )


def test_next_iterate_and_other_failures_inspect():
    nxt = _next(_rec(status="agent_error", command="iterate"), {"phase": "agent"})
    assert (nxt["action"], nxt["retry_safe"]) == ("inspect", False)
    assert "pushed" in nxt["why"]
    nxt = _next(_rec(status="agent_error"))
    assert (nxt["action"], nxt["command"]) == ("inspect", "franky job logs abc123")


# ---------------------------------------------------------------------------
# Probes: pid_alive
# ---------------------------------------------------------------------------


def _pid_rec(**kw):
    return {"pid": 4242, "host": "h1", "pid_started_at": "111", **kw}


def _probe(record, *, kill=lambda p, s: None, marker="111", host="h1"):
    return jobs.pid_alive(record, kill=kill, marker_of=lambda p: marker, hostname=lambda: host)


def test_pid_alive_true_false_and_permission():
    assert _probe(_pid_rec()) is True

    def gone(pid, sig):
        raise ProcessLookupError

    def other_user(pid, sig):
        raise PermissionError

    assert _probe(_pid_rec(), kill=gone) is False
    assert _probe(_pid_rec(), kill=other_user) is True


def test_pid_reuse_is_dead_and_unreadable_marker_is_alive():
    assert _probe(_pid_rec(), marker="999") is False
    assert _probe(_pid_rec(), marker=None) is True


def test_legacy_record_and_other_host_are_unknown():
    assert _probe({"job_id": "abc123"}) is None
    assert _probe(_pid_rec(), host="other") is None
    assert _probe(_pid_rec(pid=0)) is None  # kill(0, 0) would signal a process group
    state = _derive(_rec(), _out(5), alive=_probe({"job_id": "abc123"}))["state"]
    assert state == "unknown"


# ---------------------------------------------------------------------------
# Heartbeat sidecar
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_heartbeat_throttles_and_flushes_on_phase_change(tmp_path):
    env, clock = _env(tmp_path), _Clock()
    hb = jobs.Heartbeat("abc123", env, clock=clock)
    hb.output("bash")  # first line: setup -> agent, written now
    assert jobs.read_progress("abc123", env)["phase"] == "agent"
    hb.output("edit")
    hb.output("edit")
    assert jobs.read_progress("abc123", env)["output_lines"] == 1  # throttled
    clock.t += jobs.HEARTBEAT_THROTTLE_SECS
    hb.output("grep")
    got = jobs.read_progress("abc123", env)
    assert (got["output_lines"], got["last_tool"]) == (4, "grep")
    hb.output("grep")
    hb.set(phase="publish")  # phase change is never throttled
    got = jobs.read_progress("abc123", env)
    assert (got["phase"], got["output_lines"]) == ("publish", 5)
    assert got["updated_at"] and got["attempt"] == 1 and got["retry_reason"] is None


def test_heartbeat_never_raises(tmp_path):
    hb = jobs.Heartbeat("../../etc", _env(tmp_path))  # unsafe id: no path, no write
    hb.output("bash")
    hb.set(phase="publish")


def test_late_heartbeat_does_not_touch_a_finished_record(tmp_path):
    env = _env(tmp_path)
    jobs.write_record(_rec(status="timeout"), env)
    jobs.Heartbeat("abc123", env).output("bash")
    assert jobs.read_record("abc123", env)["status"] == "timeout"
    live = _derive(jobs.read_record("abc123", env), jobs.read_progress("abc123", env))
    assert live["state"] == "finished"


def test_progress_sidecar_is_not_a_run_and_is_pruned_with_its_record(tmp_path):
    env = _env(tmp_path)
    old = _rec("aa0001", status="pr_opened")
    old["started_at"] = "2026-01-01T00:00:00+00:00"
    jobs.write_record(old, env)
    jobs.write_record(_rec("aa0002", status="pr_opened"), env)
    for job_id in ("aa0001", "aa0002"):
        jobs.Heartbeat(job_id, env).flush()
    assert [r["job_id"] for r in jobs.list_records(env)] == ["aa0002", "aa0001"]
    jobs.prune(env, keep=1)
    assert jobs.read_progress("aa0001", env) is None
    assert jobs.read_progress("aa0002", env) is not None


def test_tool_name_reads_the_name_only():
    secret_cmd = "echo TOPSECRET"
    lines = [
        {"type": "tool_use", "name": "bash", "input": {"command": secret_cmd}},  # pi
        {"type": "message", "content": [{"type": "tool_use", "name": "bash", "input": {}}]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "bash"}]}},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": secret_cmd}}}},
    ]
    for event in lines:
        assert tool_name(json.dumps(event)) == "bash"
    codex = {"type": "item.started", "item": {"type": "command_execution", "command": secret_cmd}}
    assert tool_name(json.dumps(codex)) == "command_execution"
    for junk in ("plain text", "{not json", "[]", '{"type": "result"}'):
        assert tool_name(junk) is None


def test_last_tool_is_redacted_and_capped(tmp_path):
    env = _env(tmp_path)
    hb = jobs.Heartbeat("abc123", env)
    cb = cli._liveness_progress(hb, None, ["sekrit"])
    cb(json.dumps({"type": "tool_use", "name": "sekrit-tool", "input": {"command": "sekrit"}}))
    cb(json.dumps({"type": "tool_use", "name": "x" * 200}))
    hb.flush()
    got = jobs.read_progress("abc123", env)
    assert len(got["last_tool"]) == 64
    hb.set(phase="agent")
    cb(json.dumps({"type": "tool_use", "name": "sekrit-tool"}))
    hb.flush()
    text = (tmp_path / "runs" / "abc123.progress.json").read_text()
    assert "sekrit" not in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(monkeypatch, tmp_path, args):
    env = {**_env(tmp_path), "FRANKY_CONFIG_FILE": str(tmp_path / "no-config")}
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "container_state", lambda name: True)
    return CliRunner().invoke(cli.main, args)


def test_job_status_json_has_state_and_next(monkeypatch, tmp_path):
    record = _rec()
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    jobs.write_record(record, _env(tmp_path))
    res = _cli(monkeypatch, tmp_path, ["job", "status", "abc123", "--json"])
    data = json.loads(res.stdout)
    assert data["state"] == "active"  # this test process is the recorded owner
    assert data["next"]["action"] == "wait"
    assert data["container_running"] is True and data["job_id"] == "abc123"


def test_job_status_text_leads_with_state_and_next(monkeypatch, tmp_path):
    jobs.write_record(_rec(status="timeout"), _env(tmp_path))
    res = _cli(monkeypatch, tmp_path, ["job", "status", "abc123"])
    lines = res.stdout.splitlines()
    assert lines[0] == "state:      finished"
    assert lines[1] == "next:       inspect franky job logs abc123"


def test_job_status_legacy_record_is_unknown(monkeypatch, tmp_path):
    record = _rec()
    for key in ("pid", "host", "pid_started_at"):
        del record[key]
    jobs.write_record(record, _env(tmp_path))
    res = _cli(monkeypatch, tmp_path, ["job", "status", "abc123", "--json"])
    data = json.loads(res.stdout)
    assert (data["state"], data["next"]["action"]) == ("unknown", "check")


def test_job_not_found_hint_explains_ids(monkeypatch, tmp_path):
    res = _cli(monkeypatch, tmp_path, ["job", "status", "abcdef", "--json"])
    err = json.loads(res.stdout)["error"]
    assert err["kind"] == "job_not_found"
    assert "12 hex" in err["hint"] and "franky jobs --json" in err["hint"]


def test_job_logs_on_running_job_reports_state_and_tool(monkeypatch, tmp_path):
    jobs.write_record(_rec(), _env(tmp_path))
    jobs.Heartbeat("abc123", _env(tmp_path)).output("bash")
    res = _cli(monkeypatch, tmp_path, ["job", "logs", "abc123"])
    assert res.exit_code == 2
    assert "state=" in res.stderr and "last_tool=bash" in res.stderr
    assert "franky job status abc123 --json" in res.stderr


def test_run_end_persists_review_fields_only_when_set(tmp_path):
    env = _env(tmp_path)
    for job_id, url in (
        ("aa0001", "https://github.com/o/r/pull/1#pullrequestreview-1"),
        ("aa0002", None),
    ):
        jobs.write_record(_rec(job_id), env)
        cli._record_run_end(
            job_id,
            status="review_published",
            pr_url=url,
            usage=cli.Usage(),
            duration=1.0,
            exit_code=0,
            log_path="",
            extra={"review_url": url, "reviewed_sha": "abc"},
            env=env,
        )
    assert jobs.read_record("aa0001", env)["review_url"].endswith("pullrequestreview-1")
    assert jobs.read_record("aa0001", env)["reviewed_sha"] == "abc"
    assert "review_url" not in jobs.read_record("aa0002", env)


def test_json_start_event_on_stderr_even_when_quiet(monkeypatch, tmp_path):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "FRANKY_CONFIG_FILE": str(tmp_path / "no-config"),
        **_env(tmp_path),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    tool = {"type": "tool_use", "name": "bash", "input": {"command": "echo TOPSECRET"}}

    def fake_run(cfg, argv, *a, **k):
        k["progress"](json.dumps(tool) + "\n")
        return 0, "opened https://github.com/me/repo/pull/7"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--json", "--quiet"])
    result = json.loads(res.stdout)  # stdout is exactly one JSON object
    events = [json.loads(line) for line in res.stderr.splitlines() if line.startswith("{")]
    assert events == [
        {
            "event": "started",
            "job_id": result["job_id"],
            "command": "build",
            "status_command": f"franky job status {result['job_id']} --json",
        }
    ]
    record = jobs.read_record(result["job_id"], env)
    assert record["pid"] > 0 and record["host"]
    progress = jobs.read_progress(result["job_id"], env)
    assert (progress["phase"], progress["last_tool"], progress["output_lines"]) == (
        "agent",
        "bash",
        1,
    )
    assert "TOPSECRET" not in (tmp_path / "runs" / f"{result['job_id']}.progress.json").read_text()
    assert result["job_id"] not in cli._HEARTBEATS  # dropped at run end
