"""Tests for the review thread store (franky/threads.py). Filesystem only, no Docker or network."""

import io
import json
import os
import stat
import subprocess
import tarfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from franky import threads
from franky.result import EXIT_TASK_REJECTED, TaskRejected

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
TOKEN = "ghp_" + "A" * 36
SECRET = "s3cr3t-value-xyz"


@pytest.fixture
def env(tmp_path):
    return {"FRANKY_THREADS_DIR": str(tmp_path / "threads")}


def _open(env, repo="me/repo", pr=7, role="reviewer"):
    return threads.open_thread(repo, pr, role, env)


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _record(**overrides):
    record = {
        "schema": 1,
        "repo": "me/repo",
        "pr": 7,
        "role": "reviewer",
        "engine": "claude",
        "model": None,
        "rubric_version": "",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "session_ok": True,
        "last_sha": "a" * 40,
        "last_job_id": "abc",
        "handoff": {"schema": 1, "sha": "a" * 40, "summary": "s", "findings": []},
        "created_at": NOW.isoformat(),
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
    }
    record.update(overrides)
    return record


def _shaped(findings=None):
    return {"summary": "ok", "findings": findings or [], "checks": [], "has_blocking": False}


# --- ids and refs ------------------------------------------------------------------------------


def test_thread_id_shape():
    assert threads.thread_id("me/repo.js", 12, "author") == "me__repo.js__12__author"


@pytest.mark.parametrize(
    "repo,pr,role",
    [
        ("me/../etc", 1, "reviewer"),
        ("../repo", 1, "reviewer"),
        ("me/..", 1, "reviewer"),
        ("me/.", 1, "reviewer"),
        ("me/re po", 1, "reviewer"),
        ("me/repo/x", 1, "reviewer"),
        ("me/repo\n", 1, "reviewer"),
        ("me/repo", 0, "reviewer"),
        ("me/repo", -3, "reviewer"),
        ("me/repo", True, "reviewer"),
        ("me/repo", "7", "reviewer"),
        ("me/repo", 7, "admin"),
    ],
)
def test_thread_id_rejects_unsafe_values(repo, pr, role):
    with pytest.raises(ValueError):
        threads.thread_id(repo, pr, role)


def test_parse_ref():
    assert threads.parse_ref("me/repo#42") == ("me/repo", 42)
    for bad in ("me/repo", "me/repo#0", "../x#1", "me/..#1", "me/repo#1/..", "me/repo#1\n", ""):
        with pytest.raises(ValueError):
            threads.parse_ref(bad)


# --- locking and recovery ----------------------------------------------------------------------


def test_open_thread_creates_0700_dir_and_refuses_a_second_holder(env):
    first = _open(env)
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o700
    with pytest.raises(TaskRejected) as info:
        _open(env)
    assert info.value.kind == "thread_busy"
    assert info.value.code == EXIT_TASK_REJECTED
    first.close()
    second = _open(env)  # released on close
    second.close()


def test_open_thread_restores_session_after_a_half_done_swap(env):
    thread = _open(env)
    thread.close()
    _write(thread.path / "session.old" / "a.jsonl", "old")
    _write(thread.path / "session.new" / "a.jsonl", "new")
    _write(thread.path / ".tmp-incoming-x" / "b", "junk")
    reopened = _open(env)
    assert (reopened.session_dir / "a.jsonl").read_text() == "old"
    assert not (reopened.path / "session.new").exists()
    assert not (reopened.path / "session.old").exists()
    assert not list(reopened.path.glob(".tmp-*"))
    reopened.close()


def test_open_thread_drops_old_session_after_a_completed_swap(env):
    thread = _open(env)
    thread.close()
    _write(thread.session_dir / "a.jsonl", "new")
    _write(thread.path / "session.old" / "a.jsonl", "old")
    reopened = _open(env)
    assert (reopened.session_dir / "a.jsonl").read_text() == "new"
    assert not (reopened.path / "session.old").exists()
    reopened.close()


# --- plan --------------------------------------------------------------------------------------


def _plan(record, **overrides):
    args = dict(engine="claude", native=True, model=None, rubric="", session_bytes=100, now=NOW)
    args.update(overrides)
    return threads.plan_run(record, **args)


@pytest.mark.parametrize(
    "record_overrides,plan_overrides,expected",
    [
        ({}, {}, ("resumed", "")),
        ({"engine": "pi"}, {}, ("seeded", "engine_changed:pi->claude")),
        ({"model": "opus"}, {}, ("seeded", "model_changed")),
        ({"rubric_version": "v1"}, {"rubric": "v2"}, ("seeded", "rubric_changed")),
        ({}, {"native": False}, ("seeded", "no_native_resume")),
        ({"session_ok": False}, {}, ("seeded", "session_not_ok")),
        ({"session_id": None}, {}, ("seeded", "session_not_ok")),
        ({}, {"session_bytes": 0}, ("seeded", "no_session")),
        ({}, {"session_bytes": threads.MAX_SESSION_BYTES + 1}, ("seeded", "too_large")),
        ({"updated_at": (NOW - timedelta(days=14)).isoformat()}, {}, ("seeded", "stale")),
        ({"updated_at": "garbage"}, {}, ("seeded", "stale")),
        ({"session_ok": False, "handoff": None}, {}, ("fresh", "session_not_ok")),
    ],
)
def test_plan_run_matrix(record_overrides, plan_overrides, expected):
    assert _plan(_record(**record_overrides), **plan_overrides) == expected


def test_plan_run_without_record_is_fresh():
    assert _plan(None) == ("fresh", "new_thread")


# --- begin / session tar -----------------------------------------------------------------------

SID = "11111111-2222-3333-4444-555555555555"
SFILE = f".claude/projects/-work/{SID}.jsonl"


def _begin(thread, record, mode, native=True):
    record, written = threads.begin_run(
        thread,
        record,
        repo="me/repo",
        pr=7,
        role="reviewer",
        engine="claude",
        native=native,
        model=None,
        rubric="",
        mode=mode,
        job_id="job1",
        now=NOW,
    )
    assert written
    return record


def test_begin_run_writes_a_new_session_id_before_launch_and_drops_old_session(env):
    thread = _open(env)
    _write(thread.session_dir / "old.jsonl")
    prior = _record()
    record = _begin(thread, prior, "seeded")
    on_disk = threads.read_record(thread.path)
    assert on_disk == record
    assert record["session_id"] != prior["session_id"]
    assert record["session_ok"] is False
    assert record["handoff"] == prior["handoff"] and record["last_sha"] == prior["last_sha"]
    assert record["updated_at"] == prior["updated_at"]  # only a success moves it
    assert not thread.session_dir.exists()
    assert stat.S_IMODE((thread.path / "record.json").stat().st_mode) == 0o600
    thread.close()


def test_begin_run_resumed_keeps_id_and_session(env):
    thread = _open(env)
    _write(thread.session_dir / SFILE)
    record = _begin(thread, _record(), "resumed")
    assert record["session_id"] == SID and record["session_ok"] is True
    assert (thread.session_dir / SFILE).exists()
    thread.close()


def test_begin_run_without_native_resume_has_no_session_id(env):
    thread = _open(env)
    assert _begin(thread, None, "fresh", native=False)["session_id"] is None
    thread.close()


def test_begin_run_reports_a_failed_record_write(env, monkeypatch):
    thread = _open(env)
    monkeypatch.setattr(threads.jobs, "_atomic_write", lambda *a, **k: False)
    _record_out, written = threads.begin_run(
        thread,
        None,
        repo="me/repo",
        pr=7,
        role="reviewer",
        engine="claude",
        native=True,
        model=None,
        rubric="",
        mode="fresh",
        job_id="j",
        now=NOW,
    )
    assert written is False
    thread.close()


def test_begin_run_stores_the_lowercase_repo(env):
    thread = threads.open_thread("Me/Repo", 7, "reviewer", env)
    assert thread.id == "me__repo__7__reviewer"
    record, _ = threads.begin_run(
        thread,
        None,
        repo="Me/Repo",
        pr=7,
        role="reviewer",
        engine="claude",
        native=True,
        model=None,
        rubric="",
        mode="fresh",
        job_id="j",
        now=NOW,
    )
    assert record["repo"] == "me/repo"
    thread.close()


def test_session_paths_are_the_file_and_side_dir_only():
    assert threads.session_paths("claude", SID) == [SFILE, f".claude/projects/-work/{SID}"]
    assert threads.session_paths("pi", SID) == []
    assert threads.session_paths("claude", None) == []


def test_session_tar_carries_only_the_session_file_and_side_dir(env, tmp_path):
    thread = _open(env)
    paths = threads.session_paths("claude", SID)
    assert threads.session_tar(thread, paths) is None  # nothing stored
    _write(thread.session_dir / SFILE, "{}")
    _write(thread.session_dir / f".claude/projects/-work/{SID}/sub/a.jsonl", "{}")
    _write(thread.session_dir / ".claude/projects/-work/memory/MEMORY.md", "planted")
    _write(thread.session_dir / ".claude/projects/-work/other.jsonl", "{}")
    (thread.session_dir / f".claude/projects/-work/{SID}/link").symlink_to(tmp_path)
    tar = threads.session_tar(thread, paths)
    assert stat.S_IMODE(tar.stat().st_mode) == 0o600
    with tarfile.open(tar) as archive:
        assert archive.getnames() == [SFILE, f".claude/projects/-work/{SID}/sub/a.jsonl"]
    tar.unlink()
    thread.close()


# --- commit ------------------------------------------------------------------------------------


def _incoming(thread, files):
    incoming = threads.new_incoming(thread)
    for rel, text in files.items():
        _write(incoming / rel, text)
    return incoming


def _no_git(*a, **k):
    raise AssertionError("no git store expected in a session tree")


def test_commit_session_swaps_in_the_new_tree(env):
    thread = _open(env)
    _write(thread.session_dir / "old.jsonl", "old")
    incoming = _incoming(thread, {SFILE: "new"})
    assert threads.commit_session(thread, incoming, [SECRET], _no_git) == (True, "")
    stored = thread.session_dir / SFILE
    assert stored.read_text() == "new"
    assert not (thread.session_dir / "old.jsonl").exists()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    assert not incoming.exists()
    assert not (thread.path / "session.new").exists() and not (thread.path / "session.old").exists()
    thread.close()


def test_commit_session_drops_links_and_special_files_without_following(env, tmp_path):
    outside = _write(tmp_path / "outside" / "keep.txt", "untouched")
    thread = _open(env)
    incoming = _incoming(thread, {"a/u.jsonl": "ok"})
    (incoming / "a" / "file-link").symlink_to(outside)
    (incoming / "dir-link").symlink_to(outside.parent, target_is_directory=True)
    os.mkfifo(incoming / "a" / "fifo")
    assert threads.commit_session(thread, incoming, [], _no_git)[0] is True
    names = sorted(str(p.relative_to(thread.session_dir)) for p in thread.session_dir.rglob("*"))
    assert names == ["a", "a/u.jsonl"]
    assert outside.read_text() == "untouched" and outside.parent.is_dir()
    thread.close()


def test_commit_session_scrubs_known_secret_values(env):
    thread = _open(env)
    incoming = _incoming(thread, {"u.jsonl": f'{{"out": "{SECRET}"}}'})
    assert threads.commit_session(thread, incoming, [SECRET], _no_git)[0] is True
    assert SECRET not in (thread.session_dir / "u.jsonl").read_text()
    thread.close()


def test_commit_session_refuses_a_token_pattern_and_keeps_the_old_session(env):
    thread = _open(env)
    _write(thread.session_dir / "old.jsonl", "old")
    incoming = _incoming(thread, {"u.jsonl": f'{{"out": "{TOKEN}"}}'})
    assert threads.commit_session(thread, incoming, [], _no_git) == (False, "verify_failed")
    assert (thread.session_dir / "old.jsonl").read_text() == "old"
    assert not (thread.session_dir / "u.jsonl").exists()
    assert not incoming.exists()
    thread.close()


def test_commit_session_refuses_empty_and_oversized_trees(env, monkeypatch):
    thread = _open(env)
    assert threads.commit_session(thread, _incoming(thread, {}), [], _no_git) == (
        False,
        "no_session",
    )
    monkeypatch.setattr(threads, "MAX_SESSION_BYTES", 3)
    big = _incoming(thread, {"u.jsonl": "four"})
    assert threads.commit_session(thread, big, [], _no_git) == (False, "too_large")
    thread.close()


def test_open_thread_rederives_session_ok_after_a_crash_between_swap_and_record(env):
    thread = _open(env)
    record = _begin(thread, None, "fresh")  # session_ok False on disk
    incoming = _incoming(thread, {f".claude/projects/-work/{record['session_id']}.jsonl": "{}"})
    assert threads.commit_session(thread, incoming, [], _no_git)[0] is True
    thread.close()  # crash: finish_run never wrote the record
    reopened = _open(env)
    assert threads.read_record(reopened.path)["session_ok"] is True
    reopened.close()
    # The reverse: the record says ok but the session file is gone.
    (reopened.session_dir / f".claude/projects/-work/{record['session_id']}.jsonl").unlink()
    again = _open(env)
    assert threads.read_record(again.path)["session_ok"] is False
    again.close()


# --- handoff and finish ------------------------------------------------------------------------


def test_build_handoff_caps_redacts_and_drops_bodies_and_resolved():
    findings = [
        {
            "title": f"t{i} {SECRET}\nline2" + "x" * 300,
            "body": "long body",
            "severity": "normal",
            "file": "f.py",
            "line": i,
            "status": "resolved" if i < 5 else "open",
        }
        for i in range(50)
    ]
    handoff = threads.build_handoff(
        {"summary": "y" * 900, "findings": findings}, "b" * 40, [SECRET]
    )
    assert handoff["sha"] == "b" * 40 and len(handoff["summary"]) == 500
    assert len(handoff["findings"]) == threads.HANDOFF_MAX_FINDINGS
    first = handoff["findings"][0]
    assert first["line"] == 5 and first["status"] == "open"  # resolved ones dropped
    assert "body" not in first and len(first["title"]) == 200
    assert SECRET not in json.dumps(handoff) and "\n" not in first["title"]


def test_build_handoff_redacts_token_patterns_in_every_text_field():
    handoff = threads.build_handoff(
        {
            "summary": f"leaked {TOKEN}.",
            "findings": [{"title": f"token {TOKEN} in log", "file": f"{TOKEN}.txt"}],
        },
        "b" * 40,
        [],
    )
    text = json.dumps(handoff)
    assert TOKEN not in text and "ghp_" not in text
    assert handoff["findings"][0]["title"] == "token [redacted] in log"


def test_build_handoff_keeps_blocking_findings_first_under_the_cap():
    findings = [{"title": f"nit{i}", "severity": "nit"} for i in range(45)]
    findings += [{"title": "odd", "severity": "weird"}, {"title": "norm", "severity": "normal"}]
    findings += [{"title": f"block{i}", "severity": "blocking"} for i in range(2)]
    titles = [
        f["title"] for f in threads.build_handoff({"findings": findings}, "s", [])["findings"]
    ]
    assert titles[:3] == ["block0", "block1", "norm"]
    assert titles[3:5] == ["nit0", "nit1"] and len(titles) == 40


def _finish(thread, record, **overrides):
    args = dict(
        mode="resumed",
        code=0,
        shaped=_shaped([{"title": "bug", "severity": "blocking", "file": None, "line": None}]),
        sha="c" * 40,
        tail="",
        incoming=None,
        copy_status=None,
        secrets=[],
        now=NOW,
        runner=_no_git,
    )
    args.update(overrides)
    return threads.finish_run(thread, record, **args)


def test_finish_run_success_commits_session_and_handoff(env):
    thread = _open(env)
    record = _begin(thread, None, "fresh")
    incoming = _incoming(thread, {"u.jsonl": "{}"})
    record, reason = _finish(thread, record, mode="fresh", incoming=incoming, copy_status="ok")
    assert reason == "" and record["session_ok"] is True
    assert record["last_sha"] == "c" * 40 and record["updated_at"] == NOW.isoformat(
        timespec="seconds"
    )
    assert record["handoff"]["findings"][0]["title"] == "bug"
    assert threads.read_record(thread.path) == record
    assert (thread.session_dir / "u.jsonl").exists()
    thread.close()


def test_finish_run_failed_fresh_run_keeps_previous_handoff(env):
    thread = _open(env)
    prior = _record(session_ok=False)
    record = _begin(thread, prior, "seeded")
    incoming = _incoming(thread, {"u.jsonl": "{}"})
    record, _ = _finish(
        thread, record, mode="seeded", code=1, shaped=None, incoming=incoming, copy_status="ok"
    )
    assert record["handoff"] == prior["handoff"] and record["updated_at"] == prior["updated_at"]
    assert not incoming.exists()
    thread.close()


def test_finish_run_timed_out_resumed_run_keeps_its_session(env):
    thread = _open(env)
    _write(thread.session_dir / SFILE)
    record = _begin(thread, _record(), "resumed")
    record, reason = _finish(thread, record, code=124, shaped=None, tail="plain error")
    assert reason == "" and record["session_ok"] is True
    assert (thread.session_dir / SFILE).exists()
    thread.close()


@pytest.mark.parametrize(
    "tail,mode,reason",
    [
        ("Error: No conversation found with session ID: 1111", "resumed", "resume_failed"),
        ("error: unknown option '--session-id'", "fresh", "engine_flags_unsupported"),
        ("error: unknown option '--resume'", "resumed", "engine_flags_unsupported"),
        ("some other failure", "resumed", ""),  # any failed resume drops it; regex only labels
    ],
)
def test_finish_run_marks_an_unusable_session(env, tail, mode, reason):
    thread = _open(env)
    _write(thread.session_dir / SFILE)
    record = _begin(thread, _record(), mode)
    record, got = _finish(thread, record, mode=mode, code=1, shaped=None, tail=tail)
    assert got == reason and record["session_ok"] is False
    assert threads.read_record(thread.path)["session_ok"] is False
    assert not thread.session_dir.exists()
    thread.close()


def test_finish_run_ignores_the_resume_phrase_inside_json_events(env):
    thread = _open(env)
    _write(thread.session_dir / SFILE)
    record = _begin(thread, _record(), "resumed")
    event = json.dumps({"type": "tool_result", "content": "No conversation found with session ID"})
    record, got = _finish(thread, record, code=0, tail=event + "\n")
    assert got == "" and record["session_ok"] is True
    # Also on a failure, only non-JSON lines label the reason.
    record, got = _finish(thread, record, code=1, shaped=None, tail=event + "\n")
    assert got == ""
    thread.close()


@pytest.mark.parametrize(
    "incoming_text,copy_status,why",
    [(TOKEN, "ok", "verify_failed"), ("{}", "too_large", "too_large")],
)
def test_finish_run_drops_the_session_when_a_copy_cannot_be_stored(
    env, capsys, incoming_text, copy_status, why
):
    thread = _open(env)
    _write(thread.session_dir / SFILE, "old")
    record = _begin(thread, _record(), "resumed")
    incoming = _incoming(thread, {"u.jsonl": incoming_text})
    record, _ = _finish(thread, record, incoming=incoming, copy_status=copy_status)
    err = capsys.readouterr().err
    assert f"reason={why}" in err and TOKEN not in err
    assert record["session_ok"] is False and record["handoff"] is not None
    assert not thread.session_dir.exists()  # the next run seeds a compact session
    thread.close()


def test_finish_run_write_failure_prints_one_line_without_content(env, capsys, monkeypatch):
    thread = _open(env)
    record = _begin(thread, None, "fresh")
    monkeypatch.setattr(threads.jobs, "_atomic_write", lambda *a, **k: False)
    _finish(thread, record, mode="fresh")
    err = capsys.readouterr().err
    assert err == f"franky: thread {thread.id}: record not updated reason=write_failed\n"
    thread.close()


# --- list / prune / purge ----------------------------------------------------------------------


def _stored(env, pr, *, updated, size=0, repo="me/repo"):
    thread = _open(env, repo=repo, pr=pr)
    threads.write_record(thread, _record(repo=repo, pr=pr, updated_at=updated.isoformat()))
    if size:
        _write(thread.session_dir / "s.jsonl", "x" * size)
    thread.close()
    return thread.path


def _fake_gh(states=None, *, code=0, raises=None, calls=None):
    def gh(args, env, **kwargs):
        if calls is not None:
            calls.append(args)
        if raises:
            raise raises
        data = {f"t{i}": {"pullRequest": {"state": s}} for i, s in enumerate(states or [])}
        return code, json.dumps({"data": data}), ""

    return gh


def test_list_threads_reports_session_bytes(env):
    _stored(env, 1, updated=NOW, size=10)
    [entry] = threads.list_threads(env)
    assert entry["thread"] == "me__repo__1__reviewer" and entry["session_bytes"] == 10


def test_purge_dir_removes_everything_in_order(env):
    path = _stored(env, 1, updated=NOW, size=5)
    _write(path / "session.new" / "a")
    _write(path / ".tmp-x")
    assert threads._purge_dir(path) == 7  # session, staging and temp bytes
    assert not path.exists()


def test_prune_sweeps_old_orphans_only(env):
    root = threads.threads_dir(env)
    old = root / "me__repo__1__reviewer"
    young = root / "me__repo__2__reviewer"
    old.mkdir(parents=True)
    young.mkdir()
    stamp = (NOW - timedelta(hours=2)).timestamp()
    os.utime(old, (stamp, stamp))
    os.utime(young, (NOW.timestamp(), NOW.timestamp()))
    result = threads.prune(env, now=NOW)
    assert result["purged"] == [{"thread": old.name, "reason": "orphan", "bytes": 0}]
    assert not old.exists() and young.exists()


def test_prune_idle_threads(env):
    idle = _stored(env, 1, updated=NOW - timedelta(days=31))
    fresh = _stored(env, 2, updated=NOW - timedelta(days=1))
    result = threads.prune(env, older_than_days=30, now=NOW)
    assert [e["reason"] for e in result["purged"]] == ["idle"]
    assert not idle.exists() and fresh.exists() and result["kept"] == 1


def test_prune_disk_cap_drops_oldest_sessions_but_keeps_records(env):
    oldest = _stored(env, 1, updated=NOW - timedelta(days=3), size=100)
    middle = _stored(env, 2, updated=NOW - timedelta(days=2), size=100)
    newest = _stored(env, 3, updated=NOW - timedelta(days=1), size=100)
    result = threads.prune(env, max_bytes=150, now=NOW)
    assert result["purged"] == [
        {"thread": oldest.name, "reason": "disk", "bytes": 100},
        {"thread": middle.name, "reason": "disk", "bytes": 100},
    ]
    assert result["bytes"] == 100 and result["kept"] == 3
    assert threads.read_record(oldest) is not None and not (oldest / "session").exists()
    assert (newest / "session" / "s.jsonl").exists()


def test_prune_closed_purges_merged_and_closed_only(env):
    merged = _stored(env, 1, updated=NOW)
    opened = _stored(env, 2, updated=NOW)
    closed = _stored(env, 3, updated=NOW)
    calls = []
    result = threads.prune(
        env, closed=True, now=NOW, gh=_fake_gh(["MERGED", "OPEN", "CLOSED"], calls=calls)
    )
    assert sorted(e["thread"] for e in result["purged"]) == [merged.name, closed.name]
    assert opened.exists() and len(calls) == 1
    query = calls[0][-1]
    assert calls[0][:3] == ["api", "graphql", "-f"]
    assert 't0: repository(owner:"me",name:"repo"){pullRequest(number:1){state}}' in query


def test_prune_closed_batches_fifty_threads_per_query(env):
    for pr in range(1, 52):
        _stored(env, pr, updated=NOW)
    calls = []
    threads.prune(env, closed=True, now=NOW, gh=_fake_gh([], calls=calls))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "gh",
    [
        _fake_gh([], code=1),
        lambda *a, **k: (1, "not json", "boom"),
        lambda *a, **k: (0, json.dumps({"data": {"t0": None}}), ""),
    ],
)
def test_prune_closed_keeps_threads_on_errors_or_missing_aliases(env, gh):
    path = _stored(env, 1, updated=NOW)
    assert threads.prune(env, closed=True, now=NOW, gh=gh)["purged"] == []
    assert path.exists()


def test_prune_closed_without_gh_warns_and_skips_only_that_pass(env):
    _stored(env, 1, updated=NOW)
    idle = _stored(env, 2, updated=NOW - timedelta(days=90))
    warnings = []
    result = threads.prune(
        env, closed=True, now=NOW, gh=_fake_gh(raises=OSError("no gh")), warn=warnings.append
    )
    assert warnings and "skipping the --closed pass" in warnings[0]
    assert [e["thread"] for e in result["purged"]] == [idle.name]


def test_prune_and_purge_skip_a_busy_thread(env):
    path = _stored(env, 1, updated=NOW - timedelta(days=90), size=10)
    held = _open(env, pr=1)
    assert threads.prune(env, now=NOW, max_bytes=0)["purged"] == []
    assert threads.purge(env, ref="me/repo#1") == {"purged": [], "busy": [path.name]}
    assert threads.purge(env) == {"purged": [], "busy": [path.name]}
    held.close()
    assert threads.purge(env, ref="me/repo#1")["purged"][0]["thread"] == path.name


def test_purge_by_ref_and_role(env):
    reviewer = _stored(env, 1, updated=NOW)
    author = threads.open_thread("me/repo", 1, "author", env)
    author.close()
    other = _stored(env, 2, updated=NOW)
    result = threads.purge(env, ref="me/repo#1", role="reviewer")
    assert result == {
        "purged": [{"thread": reviewer.name, "reason": "manual", "bytes": 0}],
        "busy": [],
    }
    assert author.path.exists()
    threads.purge(env, ref="me/repo#1")
    assert not author.path.exists() and other.exists()
    threads.purge(env)
    assert not other.exists()


def test_gh_timeout_is_treated_as_unavailable(env):
    _stored(env, 1, updated=NOW)
    warnings = []
    threads.prune(
        env,
        closed=True,
        now=NOW,
        gh=_fake_gh(raises=subprocess.TimeoutExpired("gh", 60)),
        warn=warnings.append,
    )
    assert warnings


def test_prune_repo_filter_limits_every_pass_and_skips_disk(env):
    mine_closed = _stored(env, 1, updated=NOW, size=100)
    mine_idle = _stored(env, 2, updated=NOW - timedelta(days=90))
    other_closed = _stored(env, 1, updated=NOW, size=100, repo="me/other")
    other_idle = _stored(env, 2, updated=NOW - timedelta(days=90), repo="me/other")
    root = threads.threads_dir(env)
    stamp = (NOW - timedelta(hours=2)).timestamp()
    orphans = [root / "me__repo__9__reviewer", root / "me__other__9__reviewer"]
    for orphan in orphans:
        orphan.mkdir()
        os.utime(orphan, (stamp, stamp))
    calls = []
    result = threads.prune(
        env,
        closed=True,
        repo="me/repo",
        max_bytes=0,
        now=NOW,
        gh=_fake_gh(["MERGED"], calls=calls),
    )
    assert sorted((e["thread"], e["reason"]) for e in result["purged"]) == [
        (mine_closed.name, "closed"),
        (mine_idle.name, "idle"),
        (orphans[0].name, "orphan"),
    ]
    assert result["disk_skipped"] is True and result["kept"] == 0
    assert 'name:"repo"' in calls[0][-1] and 'name:"other"' not in calls[0][-1]
    assert other_closed.exists() and other_idle.exists() and orphans[1].exists()
    assert (other_closed / "session" / "s.jsonl").exists()  # disk cap not applied


def test_prune_repo_filter_rejects_invalid_repo(env):
    with pytest.raises(ValueError):
        threads.prune(env, repo="../etc", now=NOW)


def test_prune_sweeps_old_leftovers_and_counts_young_ones_in_the_disk_total(env):
    path = _stored(env, 1, updated=NOW, size=10)
    old_new = _write(path / "session.new" / "a", "x" * 5).parent
    old_tmp = _write(path / ".tmp-incoming-a" / "b", "x" * 5).parent
    young_tmp = _write(path / ".tmp-session-b.tar.gz", "x" * 7)
    stamp = (NOW - timedelta(hours=2)).timestamp()
    for leftover in (old_new, old_tmp):
        os.utime(leftover, (stamp, stamp))
    os.utime(young_tmp, (NOW.timestamp(), NOW.timestamp()))
    result = threads.prune(env, now=NOW)
    assert not old_new.exists() and not old_tmp.exists() and young_tmp.exists()
    assert result["bytes"] == 10 + 7
    # The young leftover pushes the total over the cap, so the session is dropped.
    assert threads.prune(env, now=NOW, max_bytes=12)["purged"] == [
        {"thread": path.name, "reason": "disk", "bytes": 10}
    ]


def test_prune_resolves_the_process_env_when_none_is_given(env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _stored(env, 1, updated=NOW)
    seen = []

    def gh(args, gh_env, **kwargs):
        seen.append(gh_env)
        return 0, json.dumps({"data": {"t0": {"pullRequest": {"state": "MERGED"}}}}), ""

    result = threads.prune(None, closed=True, now=NOW, gh=gh)
    assert isinstance(seen[0], Mapping) and result["purged"][0]["reason"] == "closed"


# --- author role -------------------------------------------------------------------------------


def _author(thread, record, mode, **extra):
    record, written = threads.begin_run(
        thread,
        record,
        repo="me/repo",
        pr=7,
        role="author",
        engine="claude",
        native=True,
        model=None,
        rubric="",
        mode=mode,
        job_id="job1",
        now=NOW,
        **extra,
    )
    assert written
    return record


@pytest.mark.parametrize(
    "resumes,role,expected",
    [
        (9, "author", ("resumed", "")),
        (10, "author", ("seeded", "resume_cap")),
        (10, "reviewer", ("resumed", "")),
    ],
)
def test_plan_run_caps_author_resumes_only(resumes, role, expected):
    assert _plan(_record(role=role, resumes=resumes), role=role) == expected


def test_author_resumes_ten_times_then_reseeds_and_counts_failed_runs(env):
    thread = _open(env, role="author")
    record = _author(thread, None, "fresh")
    assert record["resumes"] == 0
    for run in range(1, 12):
        record = {**record, "session_ok": True, "handoff": {"schema": 1, "sha": None}}
        mode, reason = _plan(record, role="author")
        if run <= 10:
            assert (mode, reason) == ("resumed", "")
        else:
            assert (mode, reason) == ("seeded", "resume_cap")
        # Counted in begin_run, before launch: a run that then fails or crashes still counts.
        record = _author(thread, record, mode)
        assert record["resumes"] == (run if run <= 10 else 0)
        assert threads.read_record(thread.path)["resumes"] == record["resumes"]
    thread.close()


def test_reviewer_records_never_get_a_resume_counter(env):
    thread = _open(env)
    assert "resumes" not in _begin(thread, _record(), "resumed")
    thread.close()


def test_begin_run_pins_a_given_session_id_for_a_bound_build(env):
    thread = _open(env, role="author")
    record = _author(thread, None, "fresh", session_id=SID)
    assert record["session_id"] == SID and record["session_ok"] is False
    thread.close()


def _sidecar(tmp_path, members):
    path = tmp_path / "side.session.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return path


def test_extract_session_keeps_only_the_session_file_and_side_dir(tmp_path):
    paths = threads.session_paths("claude", SID)
    side = f".claude/projects/-work/{SID}"
    archive = _sidecar(
        tmp_path,
        {
            SFILE: b"{}\n",
            f"{side}/sub.jsonl": b"{}\n",
            ".claude/projects/-work/memory/MEMORY.md": b"planted\n",
            f"{side}/link": None,
            "../escape": b"x",
        },
    )
    dest = tmp_path / "in"
    dest.mkdir()
    assert threads.extract_session(archive, dest, paths) is True
    found = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert found == [SFILE, f"{side}/sub.jsonl"]
    assert not (dest / side / "link").exists() and not (tmp_path / "escape").exists()


def test_extract_session_refuses_corrupt_or_sessionless_archives(tmp_path):
    paths = threads.session_paths("claude", SID)
    corrupt = tmp_path / "bad.tar.gz"
    corrupt.write_bytes(b"not a tar")
    (tmp_path / "a").mkdir()
    assert threads.extract_session(corrupt, tmp_path / "a", paths) is False
    other = _sidecar(tmp_path, {".claude/projects/-work/other.jsonl": b"{}"})
    (tmp_path / "b").mkdir()
    assert threads.extract_session(other, tmp_path / "b", paths) is False
    assert threads.extract_session(other, tmp_path / "b", []) is False


EVENT = '{"type":"system","subtype":"init"}'


@pytest.mark.parametrize(
    "tail,truncated,expected",
    [
        ("Error: No conversation found with session ID: x", False, True),
        ("error: unknown option '--resume'", False, True),
        ('{"type":"text","text":"No conversation found with session ID"}', False, False),
        ("tests failed after git push", False, False),
        # A tail cut mid-event: its first line is a fragment that must not pass as an error.
        ('n found with session ID: x"}\n{"type":"result"}', True, False),
        ('No conversation found with session ID: x"}\nexit 1', True, False),
        # The engine emitted events, so it ran: never a startup rejection.
        (f"{EVENT}\nError: No conversation found with session ID: x", False, False),
        ("{not json\nError: No conversation found with session ID: x", False, True),
    ],
)
def test_startup_rejected_matches_only_a_pure_engine_startup_error(tail, truncated, expected):
    assert threads.startup_rejected(tail, truncated=truncated) is expected


def test_extract_session_refuses_a_sidecar_over_the_session_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(threads, "MAX_SESSION_BYTES", 10)
    archive = _sidecar(tmp_path, {SFILE: b"x" * 11})
    (tmp_path / "in").mkdir()
    assert threads.extract_session(
        archive, tmp_path / "in", threads.session_paths("claude", SID)
    ) is (False)
