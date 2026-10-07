"""Tests for the workspace snapshot + restore module (issue #71).

Fully hermetic: no real docker, no network. The argv builders are pure; scrub/verify operate on
a real on-disk tree under `tmp_path`; the git-history verification and every orchestrator take an
injected `runner` so `git`/`docker` are never actually invoked. The load-bearing checks are the
secret-safety guarantees: cred files removed, git remote userinfo + helpers stripped, secret
values redacted, git object content decompress-scanned, and a fail-closed refusal on any hit.
"""

import io
import subprocess
import sys
import tarfile
import tracemalloc
from pathlib import Path

import pytest

from franky import snapshot


# ---------------------------------------------------------------------------
# Pure argv builders
# ---------------------------------------------------------------------------


def test_build_extract_argv():
    assert snapshot.build_extract_argv("task1", "/host/dst") == [
        "docker",
        "cp",
        "task1:/work/.",
        "/host/dst",
    ]


def test_build_untar_argv():
    # Extract as the default uid 1001 (which owns /work), reading the tar from STDIN (`-i` +
    # `-xzf -`) with --no-same-owner. WHY not -u 0: the task profile is --cap-drop=ALL, so
    # in-container root cannot write into the 1001-owned /work; WHY stdin not a cp'd file: the
    # resume container is --read-only and `docker cp` INTO it is refused even for a tmpfs target.
    assert snapshot.build_untar_argv("task1") == [
        "docker",
        "exec",
        "-i",
        "task1",
        "tar",
        "--no-same-owner",
        "-xzf",
        "-",
        "-C",
        "/work",
    ]


def test_build_marker_argv():
    assert snapshot.build_marker_argv("task1") == [
        "docker",
        "exec",
        "task1",
        "touch",
        "/work/.franky-resume-ready",
    ]


# ---------------------------------------------------------------------------
# scrub_workspace
# ---------------------------------------------------------------------------

SECRET = "ghp_supersecrettoken1234567890abcd"


def _seed_workspace(root):
    """Build a representative workspace tree and return its interesting paths."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "objects" / "ab").mkdir(parents=True)
    git_config = root / ".git" / "config"
    git_config.write_text(
        '[remote "origin"]\n'
        f"\turl = https://x-access-token:{SECRET}@github.com/o/r\n"
        "[credential]\n"
        "\thelper = store\n",
        encoding="utf-8",
    )
    # A fake compressed git object: its BYTES must be left untouched by scrub.
    git_object = root / ".git" / "objects" / "ab" / "cdef"
    git_object.write_bytes(b"\x78\x9c fake zlib blob bytes")
    # A cred file the agent could have copied into /work.
    (root / ".git-credentials").write_text(
        f"https://x-access-token:{SECRET}@github.com\n", encoding="utf-8"
    )
    (root / ".netrc").write_text("machine github.com login x password y\n", encoding="utf-8")
    (root / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    # A source file that happens to contain a secret value.
    src = root / "src" / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text(f"TOKEN = '{SECRET}'\n", encoding="utf-8")
    return git_config, git_object, src


def test_scrub_workspace_removes_creds_strips_config_redacts_source(tmp_path):
    git_config, git_object, src = _seed_workspace(tmp_path)
    object_bytes_before = git_object.read_bytes()

    snapshot.scrub_workspace(tmp_path, [SECRET])

    # Cred files gone.
    assert not (tmp_path / ".git-credentials").exists()
    assert not (tmp_path / ".netrc").exists()
    assert not (tmp_path / "key.pem").exists()

    # .git/config: userinfo stripped, helper line dropped.
    cfg_text = git_config.read_text(encoding="utf-8")
    assert SECRET not in cfg_text
    assert "x-access-token" not in cfg_text
    assert "https://github.com/o/r" in cfg_text
    assert "helper" not in cfg_text

    # Source file: secret value redacted.
    src_text = src.read_text(encoding="utf-8")
    assert SECRET not in src_text
    assert "[REDACTED]" in src_text

    # .git object bytes UNCHANGED (a naive replace would corrupt compressed objects).
    assert git_object.read_bytes() == object_bytes_before


def test_scrub_workspace_deletes_resume_marker(tmp_path):
    marker = tmp_path / snapshot.SNAPSHOT_MARKER
    marker.write_text("", encoding="utf-8")
    snapshot.scrub_workspace(tmp_path, [])
    assert not marker.exists()


def test_scrub_workspace_never_raises_on_empty_tree(tmp_path):
    # No files, no secrets - must be a clean no-op.
    snapshot.scrub_workspace(tmp_path, [])


def test_scrub_preserves_invalid_utf8_in_git_config(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    config = git_dir / "config"
    contents = b"# original bytes: \xff\xfe\n[core]\n\tbare = false\n"
    config.write_bytes(contents)
    snapshot.scrub_workspace(tmp_path, [])
    assert config.read_bytes() == contents


def test_scrub_clean_files_avoids_temporary_copies(tmp_path, monkeypatch):
    path = tmp_path / "clean.bin"
    contents = b"\xff\x00" * 65536
    path.write_bytes(contents)

    def refuse_tempfile(*args, **kwargs):
        pytest.fail("clean files must not create temporary copies")

    monkeypatch.setattr(snapshot.tempfile, "TemporaryFile", refuse_tempfile)
    snapshot.scrub_workspace(tmp_path, [SECRET, "another-secret"])
    assert path.read_bytes() == contents


# ---------------------------------------------------------------------------
# verify_no_secrets (fail-closed)
# ---------------------------------------------------------------------------


def _clean_git_runner(stdout=b""):
    """A runner that satisfies the `git cat-file` verification with the given (secret-free) stdout."""

    def runner(argv, **kwargs):
        if "stdout" in kwargs:
            kwargs["stdout"].write(stdout)
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    return runner


def test_verify_clean_tree_returns_empty(tmp_path):
    (tmp_path / "readme.txt").write_text("nothing secret here\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=_clean_git_runner())
    assert findings == []


def test_verify_flags_surviving_value_in_non_git_file(tmp_path):
    (tmp_path / "leak.txt").write_bytes(f"here: {SECRET}".encode("utf-8"))
    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=_clean_git_runner())
    assert findings
    assert all(SECRET not in f for f in findings)  # labels never leak the value


def test_verify_flags_fresh_token_via_scan_for_secrets(tmp_path):
    # A freshly minted token NOT in `secrets` is still caught by the pattern scan.
    fresh = "ghp_" + "a" * 36
    (tmp_path / "note.txt").write_text(f"minted {fresh}\n", encoding="utf-8")
    findings = snapshot.verify_no_secrets(tmp_path, [], runner=_clean_git_runner())
    assert findings


@pytest.mark.parametrize("directory", [".git", ".git/modules/libfoo"])
def test_verify_refuses_fresh_token_in_git_config(tmp_path, directory):
    git_dir = tmp_path / directory
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        "[http]\n\textraHeader = Authorization: Bearer ghp_" + "a" * 36 + "\n"
    )
    findings = snapshot.verify_no_secrets(tmp_path, [], runner=_clean_git_runner())
    assert any("credential pattern" in finding for finding in findings)


def test_verify_flags_known_value_in_git_object_content(tmp_path):
    (tmp_path / ".git").mkdir()
    # git cat-file decompresses object content; a known secret value in that stream is a finding.
    runner = _clean_git_runner(stdout=f"blob content {SECRET}".encode("utf-8"))
    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=runner)
    assert findings


def test_verify_flags_known_value_in_plaintext_git_file(tmp_path):
    # A known secret value in a PLAINTEXT .git file (e.g. .git/config extraHeader, .git/logs/*)
    # must be caught by the raw-byte value scan - the earlier .git-skip was the fail-closed gap.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        f"[http]\n\textraHeader = Authorization: Bearer {SECRET}\n", encoding="utf-8"
    )
    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=_clean_git_runner())
    assert findings
    assert all(SECRET not in f for f in findings)


def test_verify_scans_submodule_object_store(tmp_path):
    # A submodule's objects live under .git/modules/<name>/ (not a dir named .git), so they must be
    # cat-file-scanned via their own store. A known value in that store's decompressed content is a
    # finding.
    sub_store = tmp_path / ".git" / "modules" / "libfoo"
    (sub_store / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects").mkdir(parents=True)

    def runner(argv, **kwargs):
        # Surface the secret ONLY for the submodule store, to prove it is actually scanned.
        if "--git-dir" in argv:
            gd = argv[argv.index("--git-dir") + 1]
            if "modules" in gd:
                if "stdout" in kwargs:
                    kwargs["stdout"].write(f"blob {SECRET}".encode("utf-8"))
                    return subprocess.CompletedProcess(argv, 0)
                return subprocess.CompletedProcess(
                    argv, 0, stdout=f"blob {SECRET}".encode("utf-8"), stderr=b""
                )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=runner)
    assert any("modules/libfoo" in f for f in findings)


def test_verify_git_call_error_is_a_finding(tmp_path):
    (tmp_path / ".git").mkdir()

    def failing_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"fatal")

    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=failing_runner)
    assert findings  # fail-closed: an unverifiable history is refused


def test_verify_git_call_raise_is_a_finding(tmp_path):
    (tmp_path / ".git").mkdir()

    def raising_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=20.0)

    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=raising_runner)
    assert findings


# ---------------------------------------------------------------------------
# finalize_snapshot (fail-closed + clean pack)
# ---------------------------------------------------------------------------


def test_finalize_snapshot_refuses_when_verify_finds_something(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    # A raw secret survives (no scrub can remove a value that is also freshly-token-shaped and not
    # in secrets, but here we keep it in `secrets` so verify's value scan catches what scrub
    # missed under .git). Simulate by putting the value under a .git dir so scrub skips it and
    # then a git runner surfaces it.
    (src / ".git").mkdir()
    dest = tmp_path / "out" / "snap.tar.gz"
    runner = _clean_git_runner(stdout=SECRET.encode("utf-8"))
    result = snapshot.finalize_snapshot(src, dest, [SECRET], runner=runner)
    assert result is None
    assert not dest.exists()  # no tar written on a fail-closed refusal
    assert not src.exists()  # src always cleaned up


def test_finalize_snapshot_clean_writes_secret_free_tar(tmp_path):
    src = tmp_path / "src"
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.py").write_text(f"TOKEN='{SECRET}'\n", encoding="utf-8")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text(
        f"[remote]\n\turl = https://x-access-token:{SECRET}@github.com/o/r\n", encoding="utf-8"
    )
    dest = tmp_path / "out" / "snap.tar.gz"

    result = snapshot.finalize_snapshot(src, dest, [SECRET], runner=_clean_git_runner())
    assert result == str(dest)
    assert dest.exists()
    # 0600 file.
    assert (dest.stat().st_mode & 0o777) == 0o600

    with tarfile.open(dest, "r:gz") as tar:
        members = tar.getnames()
        assert "src/app.py" in members
        # No secret bytes anywhere in the packed tar (scrub redacted the source + config).
        for m in tar.getmembers():
            if m.isfile():
                data = tar.extractfile(m).read()
                assert SECRET.encode("utf-8") not in data
                # Deterministic host-free metadata.
                assert m.mode == 0o600 and m.mtime == 0 and m.uid == 0 and m.uname == ""
    assert not src.exists()  # cleaned up


def test_large_snapshot_has_bounded_memory_and_redacts_chunk_boundaries(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    path = src / "large.bin"
    block = b"x" * (64 * 1024 - 1) + b"\n"
    with path.open("wb") as stream:
        for _ in range(256):
            stream.write(block)
        stream.write(b"\xff" + b"a" * 65530 + b"long-secret-value\nshort-secret\n")
    dest = tmp_path / "snapshot.tar.gz"
    tracemalloc.start()
    try:
        result = snapshot.finalize_snapshot(
            src, dest, ["long-secret", "long-secret-value", "short-secret"]
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == str(dest)
    assert peak < 8 * 1024 * 1024
    with tarfile.open(dest) as tar:
        with tar.extractfile("large.bin") as stream:
            stream.seek(-100, 2)
            assert stream.read().endswith(b"[REDACTED]\n[REDACTED]\n")


def test_verify_git_output_streams_to_private_file_and_checks_chunk_boundaries(tmp_path):
    (tmp_path / ".git").mkdir()
    outputs = []

    def runner(argv, **kwargs):
        output = kwargs.get("stdout")
        assert output is not None
        assert kwargs["timeout"] == 20.0
        outputs.append(output)
        output.write(b"a" * 65530 + SECRET.encode())
        return subprocess.CompletedProcess(argv, 0)

    findings = snapshot.verify_no_secrets(tmp_path, [SECRET], runner=runner)
    assert any("secret value in git object content" in finding for finding in findings)
    assert outputs and all(output.closed for output in outputs)


def test_verify_git_launcher_sets_file_limit_before_exec(tmp_path, monkeypatch):
    import os
    import resource

    (tmp_path / ".git").mkdir()
    actions = []

    def runner(argv, **kwargs):
        assert argv[:2] == [sys.executable, "-c"]
        assert "preexec_fn" not in kwargs
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", ["-c", *argv[3:]])
            patch.setattr(resource, "setrlimit", lambda *args: actions.append(("limit", args)))
            patch.setattr(os, "execvp", lambda *args: actions.append(("exec", args)))
            exec(argv[2])
        return subprocess.CompletedProcess(argv, 0)

    assert snapshot.verify_no_secrets(tmp_path, [], runner=runner) == []
    assert actions[0] == (
        "limit",
        (resource.RLIMIT_FSIZE, (snapshot._MAX_TREE_BYTES, snapshot._MAX_TREE_BYTES)),
    )
    assert actions[1] == (
        "exec",
        (
            "git",
            [
                "git",
                "--git-dir",
                str(tmp_path / ".git"),
                "cat-file",
                "--batch-all-objects",
                "--unordered",
                "--batch",
            ],
        ),
    )


@pytest.mark.parametrize(
    "contents",
    [b"GH_TOKEN\n \n=\n \nvalue\n", b"z" * (4 * 1024 * 1024 + 1)],
    ids=["multiline-assignment", "oversized-line"],
)
def test_verify_refuses_multiline_credentials_or_unbounded_pattern_lines(tmp_path, contents):
    (tmp_path / "input").write_bytes(contents)
    assert snapshot.verify_no_secrets(tmp_path, [])


def test_verify_unreadable_file_refuses_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "input"
    path.write_bytes(b"contents")
    original = type(path).open

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", unreadable)
    assert snapshot.verify_no_secrets(tmp_path, [])


@pytest.mark.parametrize("limit", ["_MAX_FILE_BYTES", "_MAX_TREE_BYTES", "_MAX_ENTRIES"])
def test_snapshot_refuses_oversized_workspace_and_cleans_up(tmp_path, monkeypatch, limit):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_bytes(b"abcd\n")
    (src / "b").write_bytes(b"abcd\n")
    monkeypatch.setattr(snapshot, limit, 1)
    dest = tmp_path / "snapshot.tar.gz"
    assert snapshot.finalize_snapshot(src, dest, []) is None
    assert not src.exists() and not dest.exists()


def test_snapshot_never_follows_directory_or_file_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret"
    secret_file.write_text(SECRET)
    src = tmp_path / "src"
    src.mkdir()
    (src / "linked-dir").symlink_to(outside, target_is_directory=True)
    (src / "linked-file").symlink_to(secret_file)
    (src / "clean").write_text("safe\n")
    dest = tmp_path / "snapshot.tar.gz"
    assert snapshot.finalize_snapshot(src, dest, [SECRET]) == str(dest)
    with tarfile.open(dest) as tar:
        assert tar.getnames() == ["clean"]
    assert secret_file.read_text() == SECRET


def test_verify_large_git_output_uses_bounded_memory(tmp_path):
    (tmp_path / ".git").mkdir()

    def runner(argv, **kwargs):
        block = b"x" * 65536
        for _ in range(512):
            kwargs["stdout"].write(block)
        return subprocess.CompletedProcess(argv, 0)

    tracemalloc.start()
    try:
        assert snapshot.verify_no_secrets(tmp_path, [SECRET], runner=runner) == []
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# extract_workspace / restore_into_container orchestrators
# ---------------------------------------------------------------------------

NOOP_SLEEP = lambda *_a, **_k: None  # noqa: E731 - tiny test helper


def test_extract_workspace_rc0_true(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert snapshot.extract_workspace("task1", str(tmp_path), runner) is True


def test_extract_workspace_rc_nonzero_false(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such container")

    assert snapshot.extract_workspace("task1", str(tmp_path), runner) is False


def test_extract_workspace_oserror_false(tmp_path):
    def runner(argv, **kwargs):
        raise OSError("docker gone")

    assert snapshot.extract_workspace("task1", str(tmp_path), runner) is False


def _restore_kind(argv):
    """Classify a restore-orchestration docker call by its operation (or None for polls)."""
    for verb in ("tar", "touch"):
        if verb in argv:
            return verb
    return None


def _snap_file(tmp_path):
    """A real (tiny) on-disk snapshot file - restore now opens it to stream over exec stdin."""
    p = tmp_path / "snap.tar.gz"
    p.write_bytes(b"fake-tar-bytes")
    return str(p)


def test_restore_into_container_ordered_calls_and_true(tmp_path):
    calls = []
    saw_untar_stdin = False

    def runner(argv, **kwargs):
        nonlocal saw_untar_stdin
        calls.append(argv)
        if "tar" in argv and "-xzf" in argv:
            stdin = kwargs.get("stdin")
            # The tar must be fed as a BINARY file object over stdin (never a cp'd file, never text).
            saw_untar_stdin = stdin is not None and getattr(stdin, "mode", "") == "rb"
        if argv[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok = snapshot.restore_into_container("task1", _snap_file(tmp_path), runner, sleeper=NOOP_SLEEP)
    assert ok is True
    # untar-from-stdin (as uid 1001) -> marker, in that exact order. No cp, no chown.
    kinds = [k for k in (_restore_kind(c) for c in calls) if k is not None]
    assert kinds == ["tar", "touch"]
    # The untar reads from stdin (-i + -xzf -), runs as the default uid (no -u 0 - root cannot
    # write the 1001-owned /work under --cap-drop=ALL), and does not preserve archive owner.
    untar = next(c for c in calls if "tar" in c and "-xzf" in c)
    assert untar[:4] == ["docker", "exec", "-i", "task1"]
    assert "-u" not in untar
    assert "--no-same-owner" in untar
    assert untar[untar.index("-xzf") + 1] == "-"
    assert saw_untar_stdin  # the snapshot bytes are fed via stdin, never a cp'd file


def test_restore_into_container_returns_false_when_snapshot_unreadable(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    # A missing snapshot file must fail closed: no untar/chown/marker, and never raise.
    missing = str(tmp_path / "nope.tar.gz")
    ok = snapshot.restore_into_container("task1", missing, runner, sleeper=NOOP_SLEEP)
    assert ok is False
    assert not any(_restore_kind(c) is not None for c in calls)


def test_restore_into_container_does_not_touch_marker_on_untar_failure(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if "tar" in argv and "-xzf" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="tar failed")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok = snapshot.restore_into_container("task1", _snap_file(tmp_path), runner, sleeper=NOOP_SLEEP)
    assert ok is False
    # The marker must NOT fire after a failed untar.
    assert not any(_restore_kind(c) == "touch" for c in calls)


def test_restore_into_container_never_raises_on_runner_oserror(tmp_path):
    def runner(argv, **kwargs):
        raise OSError("docker gone")

    ok = snapshot.restore_into_container(
        "task1", _snap_file(tmp_path), runner, ready_polls=1, sleeper=NOOP_SLEEP
    )
    assert ok is False


def test_snapshot_path_for_rejects_bad_id():
    import pytest

    with pytest.raises(ValueError):
        snapshot.snapshot_path_for("../escape")


def test_snapshot_path_for_uses_runs_dir(tmp_path):
    env = {"FRANKY_RUNS_DIR": str(tmp_path / "runs")}
    path = snapshot.snapshot_path_for("abc123", env)
    assert path == tmp_path / "runs" / "abc123.snapshot.tar.gz"


def test_build_home_extract_argv_streams_one_home_path_as_a_tar():
    assert snapshot.build_home_extract_argv("t1", ".claude/projects/-work/u.jsonl") == [
        "docker",
        "cp",
        "t1:/home/franky/.claude/projects/-work/u.jsonl",
        "-",
    ]


class _TarPopen:
    """Fake `docker cp ... -`: stdout serves the given bytes; records kills."""

    def __init__(self, data, returncode=0):
        self.stdout = io.BytesIO(data)
        self.returncode = returncode
        self.killed = False
        self.done = False
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        return self

    def kill(self):
        self.killed = self.done = True

    def poll(self):
        return self.returncode if self.done else None

    def wait(self, timeout=None):
        self.done = True
        return self.returncode


def _tar_bytes(add):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        add(tar)
    return buffer.getvalue()


def _member(tar, name, data=b"", kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def test_copy_home_path_extracts_only_plain_files_and_dirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    def add(tar):
        _member(tar, "u", kind=tarfile.DIRTYPE)
        _member(tar, "u/a.jsonl", b"{}")
        _member(tar, "u/link", kind=tarfile.SYMTYPE, linkname=str(outside))
        _member(tar, "u/hard", kind=tarfile.LNKTYPE, linkname="u/a.jsonl")
        _member(tar, "u/dev", kind=tarfile.CHRTYPE)
        _member(tar, "u/fifo", kind=tarfile.FIFOTYPE)
        _member(tar, "../escape", b"x")
        _member(tar, "/abs", b"x")

    popen = _TarPopen(_tar_bytes(add))
    dest = tmp_path / "dest"
    dest.mkdir()
    status, used = snapshot.copy_home_path("t1", "u", dest, max_bytes=10**6, popen=popen)
    assert status == "ok" and used > 0
    assert popen.argv == ["docker", "cp", "t1:/home/franky/u", "-"]
    names = sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))
    assert names == ["u", "u/a.jsonl"]
    assert not (tmp_path / "escape").exists() and not list(outside.iterdir())
    assert oct((dest / "u" / "a.jsonl").stat().st_mode & 0o777) == oct(0o600)


def test_copy_home_path_kills_an_over_cap_stream(tmp_path):
    popen = _TarPopen(_tar_bytes(lambda tar: _member(tar, "big.jsonl", b"x" * 200_000)))
    status, used = snapshot.copy_home_path(
        "t1", "big.jsonl", tmp_path, max_bytes=100_000, popen=popen
    )
    assert status == "too_large" and used > 100_000
    assert popen.killed and not list(tmp_path.iterdir())


def test_copy_home_path_reports_a_docker_failure(tmp_path):
    popen = _TarPopen(b"", returncode=1)
    assert snapshot.copy_home_path("t1", "u", tmp_path, max_bytes=10, popen=popen)[0] == "failed"


# ---------------------------------------------------------------------------
# Session sidecar (`build --thread` timeout / `job kill` capture)
# ---------------------------------------------------------------------------

_SID_PATHS = [".claude/projects/-work/u.jsonl", ".claude/projects/-work/u"]


def _session_popen(data):
    """`docker cp` fake: the session file serves `data`; the optional side dir is absent."""

    def popen(argv, **kwargs):
        if argv[2].endswith("u.jsonl"):
            return _TarPopen(_tar_bytes(lambda tar: _member(tar, "u.jsonl", data)))(argv)
        return _TarPopen(b"", returncode=1)(argv)

    return popen


def test_session_path_for_sits_beside_the_record_and_validates_the_id(tmp_path):
    env = {"FRANKY_RUNS_DIR": str(tmp_path)}
    assert snapshot.session_path_for("abc123", env) == tmp_path / "abc123.session.tar.gz"
    with pytest.raises(ValueError):
        snapshot.session_path_for("../x", env)


def test_copy_session_lands_the_session_file_and_shares_one_budget(tmp_path):
    status = snapshot.copy_session(
        "t1", _SID_PATHS, tmp_path, max_bytes=10**6, popen=_session_popen(b"{}\n")
    )
    assert status == "ok"
    assert (tmp_path / _SID_PATHS[0]).read_bytes() == b"{}\n"


def test_copy_session_fails_without_paths_or_without_a_regular_session_file(tmp_path):
    assert snapshot.copy_session("t1", [], tmp_path, max_bytes=10) == "failed"

    def dir_only(argv, **kwargs):  # the session "file" arrives as a directory
        data = _tar_bytes(lambda tar: _member(tar, "u.jsonl", kind=tarfile.DIRTYPE))
        return _TarPopen(data)(argv)

    assert snapshot.copy_session("t1", _SID_PATHS, tmp_path, max_bytes=10**6, popen=dir_only) == (
        "failed"
    )


def test_finalize_snapshot_scrubs_and_refuses_a_session_like_a_workspace(tmp_path):
    src = tmp_path / "src" / ".claude/projects/-work"
    src.mkdir(parents=True)
    (src / "u.jsonl").write_text('{"m": "sekrit-value"}\n')
    dest = tmp_path / "j1.session.tar.gz"
    assert snapshot.finalize_snapshot(tmp_path / "src", dest, ["sekrit-value"]) == str(dest)
    assert oct(dest.stat().st_mode & 0o777) == oct(0o600)
    with tarfile.open(dest) as tar:
        assert tar.getnames() == [".claude/projects/-work/u.jsonl"]
        assert b"sekrit-value" not in tar.extractfile(tar.getmembers()[0]).read()
    src.mkdir(parents=True)
    (src / "u.jsonl").write_text("ghp_" + "A" * 36)
    other = tmp_path / "j2.session.tar.gz"
    assert snapshot.finalize_snapshot(tmp_path / "src", other, []) is None
    assert not other.exists()


def test_finalize_snapshot_never_leaves_a_partial_tar(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("x")
    dest = tmp_path / "out" / "j1.snapshot.tar.gz"

    def broken_pack(src_dir, dest_tar):
        Path(dest_tar).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(snapshot, "_pack_dir", broken_pack)
    assert snapshot.finalize_snapshot(tmp_path / "src", dest, []) is None
    assert list(dest.parent.iterdir()) == []


def test_extract_plain_refuses_more_than_max_bytes(tmp_path):
    data = _tar_bytes(lambda tar: (_member(tar, "a", b"x" * 60), _member(tar, "b", b"x" * 60)))
    with pytest.raises(ValueError):
        snapshot._extract_plain(io.BytesIO(data), tmp_path, max_bytes=100)
    snapshot._extract_plain(io.BytesIO(data), tmp_path / "ok", max_bytes=120)
    assert (tmp_path / "ok" / "b").read_bytes() == b"x" * 60


# ---------------------------------------------------------------------------
# Bundle header (`build --no-publish`)
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _bundle(tmp_path, header: str, body: bytes = b"PACK\x00\x00") -> Path:
    path = tmp_path / "x.bundle"
    path.write_bytes(header.encode() + b"\n" + body)
    return path


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_parse_bundle_header_returns_the_head_sha(tmp_path, version):
    lines = [f"# {version} git bundle"]
    if version == "v3":
        lines.append("@object-format=sha1")
    lines += [f"-{_SHA_A} base commit", f"{_SHA_B} refs/heads/franky/fix-a"]
    path = _bundle(tmp_path, "\n".join(lines) + "\n")
    assert snapshot.parse_bundle_header(path, "franky/fix-a") == _SHA_B


@pytest.mark.parametrize(
    "header",
    [
        # wrong ref
        f"# v2 git bundle\n{_SHA_B} refs/heads/franky/other\n",
        # a tag or HEAD is not a branch
        f"# v2 git bundle\n{_SHA_B} refs/tags/franky/fix-a\n",
        f"# v2 git bundle\n{_SHA_B} HEAD\n",
        # two refs, even when one is right
        f"# v2 git bundle\n{_SHA_B} refs/heads/franky/fix-a\n{_SHA_A} refs/heads/main\n",
        # no ref at all
        f"# v2 git bundle\n-{_SHA_A} base\n",
        # malformed: bad version, short sha, upper-case sha, missing space, unknown capability
        f"# v1 git bundle\n{_SHA_B} refs/heads/franky/fix-a\n",
        "# v2 git bundle\nabc refs/heads/franky/fix-a\n",
        f"# v2 git bundle\n{'B' * 40} refs/heads/franky/fix-a\n",
        f"# v2 git bundle\n{_SHA_B}refs/heads/franky/fix-a\n",
        f"# v3 git bundle\n@object-format=sha256\n{'b' * 64} refs/heads/franky/fix-a\n",
        f"# v3 git bundle\n@filter=blob:none\n{_SHA_B} refs/heads/franky/fix-a\n",
        f"# v2 git bundle\n-xyz base\n{_SHA_B} refs/heads/franky/fix-a\n",
        "",
        "not a bundle\n",
    ],
)
def test_parse_bundle_header_refuses_anything_else(tmp_path, header):
    with pytest.raises(ValueError):
        snapshot.parse_bundle_header(_bundle(tmp_path, header), "franky/fix-a")


def test_parse_bundle_header_is_bounded(tmp_path):
    # A header with no terminating blank line inside 64 KiB is refused, not read to the end.
    path = tmp_path / "big.bundle"
    path.write_bytes(b"# v2 git bundle\n" + (f"-{_SHA_A} c\n".encode() * 5000))
    with pytest.raises(ValueError):
        snapshot.parse_bundle_header(path, "franky/fix-a")


def test_parse_bundle_header_missing_file(tmp_path):
    with pytest.raises(ValueError):
        snapshot.parse_bundle_header(tmp_path / "gone.bundle", "franky/fix-a")


def test_values_in_a_stream_are_found_across_chunk_edges():
    secret = "ghp_" + "x" * 60
    data = b"a" * (snapshot._CHUNK_BYTES - 10) + secret.encode() + b"b" * 100
    assert snapshot.stream_contains_values(io.BytesIO(data), [secret]) is True
    assert snapshot.stream_contains_values(io.BytesIO(b"clean " * 50), [secret]) is False
    assert snapshot.stream_contains_values(io.BytesIO(data), []) is False
