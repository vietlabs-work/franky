"""Tests for the workspace snapshot + restore module (issue #71).

Fully hermetic: no real docker, no network. The argv builders are pure; scrub/verify operate on
a real on-disk tree under `tmp_path`; the git-history verification and every orchestrator take an
injected `runner` so `git`/`docker` are never actually invoked. The load-bearing checks are the
secret-safety guarantees: cred files removed, git remote userinfo + helpers stripped, secret
values redacted, git object content decompress-scanned, and a fail-closed refusal on any hit.
"""

import subprocess
import tarfile

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


# ---------------------------------------------------------------------------
# verify_no_secrets (fail-closed)
# ---------------------------------------------------------------------------


def _clean_git_runner(stdout=b""):
    """A runner that satisfies the `git cat-file` verification with the given (secret-free) stdout."""

    def runner(argv, **kwargs):
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
