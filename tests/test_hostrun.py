"""Host runtime (`FRANKY_RUNTIME=host`): selector, sandboxed argv, from-scratch env, the run loop
with a stub engine on PATH, job control, the Codex host auth store, and the guarantee that the
container path is unchanged. Hermetic: no Docker, no network, no real engine."""

import gzip
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from franky import cli, hostrun, jobs
from franky.config import RUNTIME_VAR, Config, load_config, resolve_runtime
from franky.engine import ClaudeEngine, CodexEngine, PiEngine
from franky.result import ConfigError
from franky.userconfig import SETTABLE_KEYS

ROOT = Path(__file__).resolve().parent.parent

CLAUDE_ENV = {
    "FRANKY_ALLOWED_REPOS": "me/repo",
    "GH_TOKEN": "ghp_hostsecret0001",
    "CLAUDE_CODE_OAUTH_TOKEN": "tok-hostsecret0002",
    "FRANKY_ENGINE": "claude",
}


def _cfg(engine=None, **kw):
    cfg = Config(
        engine=engine or ClaudeEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_hostsecret0001"},
        runtime="host",
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _stub_engine(tmp_path, body):
    """A fake `claude` on a private PATH: writes what it saw, runs `body`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C"}


# ---------------------------------------------------------------- selector


def test_runtime_defaults_to_container_and_validates():
    assert resolve_runtime({}) == "container"
    assert resolve_runtime({RUNTIME_VAR: ""}) == "container"
    assert resolve_runtime({RUNTIME_VAR: "host"}) == "host"
    with pytest.raises(ConfigError, match="FRANKY_RUNTIME must be one of container, host"):
        resolve_runtime({RUNTIME_VAR: "podman"})


def test_load_config_carries_runtime_and_refuses_engines_without_an_os_sandbox():
    assert load_config(None, CLAUDE_ENV).runtime == "container"
    assert load_config(None, {**CLAUDE_ENV, RUNTIME_VAR: "host"}).runtime == "host"
    with pytest.raises(ConfigError, match="FRANKY_RUNTIME"):
        load_config(None, {**CLAUDE_ENV, RUNTIME_VAR: "nope"})
    pi_env = {"FRANKY_ALLOWED_REPOS": "me/repo", "GH_TOKEN": "g", "FRANKY_ENGINE": "pi",
              "ANTHROPIC_API_KEY": "k", RUNTIME_VAR: "host"}  # fmt: skip
    with pytest.raises(ConfigError, match="supports only the claude and codex engines"):
        load_config(None, pi_env)


def test_runtime_is_a_settable_non_secret_key():
    assert RUNTIME_VAR in SETTABLE_KEYS


def test_version_lists_runtimes(monkeypatch):
    monkeypatch.setattr(cli, "_engine_binary_version", lambda *a, **k: None)
    env = {"FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"]}
    monkeypatch.setattr(cli.os, "environ", env)
    res = CliRunner().invoke(cli.main, ["version", "--json"])
    assert json.loads(res.output)["runtimes"] == ["container", "host"]
    res = CliRunner().invoke(cli.main, ["version"])
    assert "  runtimes:   container,host" in res.output


# ---------------------------------------------------------------- argv and sandbox


def test_rewrite_text_maps_container_paths_to_the_run_directory(tmp_path):
    home, work, tmp = tmp_path / "h", tmp_path / "w", tmp_path / "t"
    text = (
        "read /home/franky/profile/a.md, cd /work/repo, not /workspace/x or a/work or /work-x; "
        "prompt /tmp/franky-private-prompt"
    )
    out = hostrun.rewrite_text(text, home, work, tmp)
    assert f"{home}/profile/a.md" in out
    assert f"cd {work}/repo" in out
    assert "/workspace/x" in out and "a/work" in out and "/work-x" in out
    assert f"{tmp}/franky-private-prompt" in out


def test_engine_argv_swaps_the_container_flags_for_the_os_sandbox(tmp_path):
    control = tmp_path / "s.json"
    claude = ClaudeEngine().inner_argv("p", model=None)
    out = hostrun.engine_argv(claude, "claude", control)
    assert out[:3] == ["claude", "--settings", str(control)]
    assert "--dangerously-skip-permissions" in out  # headless approval; the sandbox bounds it
    codex = CodexEngine().inner_argv("p", model=None)
    out = hostrun.engine_argv(codex, "codex", control)
    assert out[:4] == [hostrun.SANDBOX_EXEC, "-f", str(control), "codex"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in out
    # Codex's own sandbox cannot nest inside the Seatbelt profile; the profile is the sandbox.
    assert out[out.index("--sandbox") + 1] == "danger-full-access"
    with pytest.raises(ValueError):
        hostrun.engine_argv(["codex", "exec", "p"], "codex", control)
    with pytest.raises(ValueError):
        hostrun.engine_argv(PiEngine().inner_argv("p", model=None), "pi", control)


def _fake_home(tmp_path):
    home = tmp_path / "real-home"
    (home / ".ssh").mkdir(parents=True)
    (home / "notes.txt").write_text("x")
    run = home / ".franky" / "runs" / "r.host"
    (run / "work").mkdir(parents=True)
    (home / ".franky" / "runs" / "other.host").mkdir()
    (home / ".franky" / "config").write_text("x")
    return home, run


def test_claude_settings_are_allow_only_and_keep_the_settings_out_of_reach(tmp_path):
    home, run = _fake_home(tmp_path)
    runs = run.parent
    ctl = runs / "r.ctl"
    s = hostrun.claude_settings(run, home, runs, (ctl,))
    sandbox = s["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["allowWrite"] == [f"{run}/home", f"{run}/work", f"{run}/tmp"]
    assert sandbox["filesystem"]["denyRead"] == [str(home)]
    assert sandbox["filesystem"]["allowRead"] == [str(run)]
    deny = s["permissions"]["deny"]
    for tool in ("Read", "Edit", "Write", "Grep", "Glob"):
        assert f"{tool}(/{home}/.ssh/**)" in deny and f"{tool}(/{home}/notes.txt)" in deny
        assert (
            f"{tool}(/{runs}/other.host/**)" in deny and f"{tool}(/{home}/.franky/config)" in deny
        )
        assert f"{tool}(/{ctl}/**)" in deny
    # The run directory itself and the path down to it are never denied wholesale.
    assert not any(str(run) in r or r.startswith(f"Read(/{home}/.franky/**") for r in deny)
    assert f"Read(/{home}/.franky)" not in deny and f"Read(/{runs})" not in deny
    # A new file directly in the home or on the way down is denied too.
    assert f"Write(/{home}/*)" in deny and f"Edit(/{runs}/*)" in deny
    assert "WebFetch" in deny and "WebSearch" in deny
    assert s["permissions"]["allow"] == [f"Read(/{run}/**)", f"Edit(/{run}/**)"]


def test_claude_settings_deny_the_whole_home_when_the_run_dir_is_elsewhere(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runs = tmp_path / "runs"
    run = runs / "r.host"
    (runs / "other.host").mkdir(parents=True)
    run.mkdir()
    s = hostrun.claude_settings(run, home, runs)
    deny = s["permissions"]["deny"]
    assert f"Read(/{home}/**)" in deny and f"Read(/{runs}/other.host/**)" in deny
    assert s["sandbox"]["filesystem"]["denyRead"] == [str(home), str(runs)]


def test_seatbelt_profile_denies_the_home_except_the_run_dir_and_codex_roots(tmp_path):
    home, run = _fake_home(tmp_path)
    runs = run.parent
    text = hostrun.seatbelt_profile(run, home, runs, [Path("/opt/x/codex")])
    lines = text.splitlines()
    assert "(allow default)" in lines and '(deny file-write* (subpath "/"))' in lines
    assert f'(deny file-read* file-write* (subpath "{home}"))' in lines
    assert '(allow file-read* (subpath "/opt/x/codex"))' in lines
    # Later rules win: the run directory allow is last, after every deny.
    assert lines[-1] == f'(allow file-read* file-write* (subpath "{run}"))'


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="needs macOS Seatbelt",
)
def test_seatbelt_profile_blocks_reads_and_writes_outside_the_run_dir(tmp_path):
    home, run = _fake_home(tmp_path)
    home = Path(os.path.realpath(home))
    run = home / ".franky" / "runs" / "r.host"
    (home / "dummy.txt").write_text("TOPSECRET")
    (run / "work" / "f.txt").write_text("inside")
    profile = tmp_path / "p.sb"
    profile.write_text(hostrun.seatbelt_profile(run, home, run.parent, []))
    script = (
        f"cat {home}/dummy.txt; echo r1=$?; cat {run}/work/f.txt; echo r2=$?; "
        f"echo x > {home}/out.txt; echo w1=$?; echo y > {run}/work/o.txt; echo w2=$?"
    )
    proc = subprocess.run(
        [hostrun.SANDBOX_EXEC, "-f", str(profile), "/bin/sh", "-c", script],
        capture_output=True,
        text=True,
    )
    assert "TOPSECRET" not in proc.stdout
    assert "r1=1" in proc.stdout and "r2=0" in proc.stdout and "inside" in proc.stdout
    assert "w1=1" in proc.stdout and "w2=0" in proc.stdout
    assert not (home / "out.txt").exists()


def test_control_files_live_outside_the_run_dir_read_only_and_are_removed(tmp_path):
    seen = {}

    def popen(argv, **kw):
        ctl = hostrun.ctl_dir("a1b2c3a1b2c3")
        base = hostrun.host_dir("a1b2c3a1b2c3")
        seen["files"] = sorted(p.name for p in ctl.iterdir())
        seen["modes"] = {stat.S_IMODE(os.lstat(p).st_mode) for p in ctl.iterdir()}
        seen["dir"] = stat.S_IMODE(os.lstat(ctl).st_mode)
        seen["outside"] = not str(ctl).startswith(str(base))
        s = json.loads((ctl / "claude-settings.json").read_text())
        seen["write"] = s["sandbox"]["filesystem"]["allowWrite"]
        seen["base"] = os.path.realpath(base)
        return subprocess.Popen(["/bin/sh", "-c", "exit 0"], **{**kw})

    code, _out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=10, env={"PATH": "/usr/bin:/bin"},
        popen=popen, run_id="a1b2c3a1b2c3",
    )  # fmt: skip
    assert code == 0
    assert seen["files"] == ["claude-settings.json"] and seen["modes"] == {0o400}
    assert seen["dir"] == 0o700 and seen["outside"]
    assert seen["write"] == [f"{seen['base']}/home", f"{seen['base']}/work", f"{seen['base']}/tmp"]
    assert not hostrun.ctl_dir("a1b2c3a1b2c3").exists()
    assert not hostrun.host_dir("a1b2c3a1b2c3").exists()


def test_preflight_refuses_without_the_engine_or_a_sandbox(monkeypatch):
    cfg = _cfg()
    with pytest.raises(ConfigError, match="needs `claude` on PATH"):
        hostrun.preflight(cfg, which=lambda b: None, platform="darwin")
    if Path("/usr/bin/sandbox-exec").exists():
        hostrun.preflight(cfg, which=lambda b: "/bin/" + b, platform="darwin")
    with pytest.raises(ConfigError, match="Seatbelt to confine reads"):
        hostrun.preflight(_cfg(CodexEngine()), which=lambda b: "/bin/" + b, platform="linux")
    with pytest.raises(ConfigError, match="bubblewrap"):
        hostrun.preflight(cfg, which=lambda b: None, platform="linux")
    with pytest.raises(ConfigError, match="not supported on win32"):
        hostrun.preflight(cfg, which=lambda b: "x", platform="win32")


def test_child_env_is_built_from_scratch():
    cfg = _cfg()
    env = hostrun.child_env(
        cfg,
        Path("/r/home"),
        Path("/r/tmp"),
        {"PATH": "/p", "AWS_SECRET_ACCESS_KEY": "nope", "SSH_AUTH_SOCK": "/s", "LANG": "C"},
    )
    assert set(env) == {
        "PATH", "LANG", "TERM", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "GIT_TERMINAL_PROMPT", "GH_TOKEN",
    }  # fmt: skip
    assert env["HOME"] == "/r/home" and env["TMPDIR"] == "/r/tmp"


def test_repo_setup_script_ships_in_the_package_beside_the_mapper():
    assert hostrun.REPO_SETUP_SCRIPT.read_text() == (ROOT / "franky-repo-setup.py").read_text()


# ---------------------------------------------------------------- the run


def test_run_on_host_runs_in_a_private_dir_with_a_scratch_env_and_cleans_up(tmp_path):
    env = _stub_engine(
        tmp_path,
        'echo "cwd=$PWD home=$HOME tmp=$TMPDIR"\n'
        'echo "token=$GH_TOKEN leak=${AWS_SECRET:-unset}"\n'
        'echo "args=$*"\n'
        'cat "$2" > /dev/null\n',
    )
    env["AWS_SECRET"] = "must-not-pass"
    cfg = _cfg()
    run_id = "abc123abc123"
    jobs.write_record({"job_id": run_id, "status": "running"}, os.environ)
    argv = ClaudeEngine().inner_argv("see /home/franky/x and /work/repo", model=None)
    code, out = hostrun.run_on_host(cfg, argv, timeout=30, env=env, run_id=run_id)
    text = "".join(out.chunks())
    base = hostrun.host_dir(run_id)
    assert code == 0, text
    assert f"cwd={os.path.realpath(base)}/work" in text
    assert "leak=unset" in text
    assert "ghp_hostsecret0001" not in text and "token=***REDACTED***" in text
    assert f"{os.path.realpath(base)}/home/x" in text and "/work/repo" not in text.replace(
        str(os.path.realpath(base)) + "/work/repo", ""
    )
    assert not base.exists()  # per-run dir removed
    record = jobs.read_record(run_id, os.environ)
    assert record["runtime"] == "host" and isinstance(record["pgid"], int)
    assert record["host_dir"] == os.path.realpath(base)


def test_run_on_host_extracts_the_profile_bundle_as_regular_files_only(tmp_path):
    env = _stub_engine(tmp_path, 'cat "$HOME/skills/a.md"; ls "$HOME/evil" 2>&1; echo done\n')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"skill text\n"
        info = tarfile.TarInfo("skills/a.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tar.addfile(link)
    code, out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=30, env=env,
        profile_bundle=buf.getvalue(), run_id="def456def456",
    )  # fmt: skip
    text = "".join(out.chunks())
    assert code == 0 and "skill text" in text and "No such file" in text


def test_run_on_host_timeout_kills_the_whole_group_and_returns_124(tmp_path):
    marker = tmp_path / "child-pid"
    env = _stub_engine(tmp_path, f"sleep 300 &\necho $! > {marker}\nwait\n")
    t0 = time.monotonic()
    code, out = hostrun.run_on_host(
        _cfg(),
        ClaudeEngine().inner_argv("p", model=None),
        timeout=1,
        env=env,
        run_id="aaa111aaa111",
    )
    assert code == 124
    assert "host run timed out after 1s" in "".join(out.chunks())
    assert time.monotonic() - t0 < 30
    time.sleep(0.2)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_run_on_host_refuses_resume_and_export(tmp_path):
    with pytest.raises(ConfigError, match="does not support resume_workspace"):
        hostrun.run_on_host(_cfg(), ["claude"], timeout=1, resume_workspace="/x")
    with pytest.raises(ConfigError, match="does not support workspace_sink"):
        hostrun.run_on_host(_cfg(), ["claude"], timeout=1, workspace_sink={"dest": "/x"})


def test_run_on_host_refuses_an_argv_it_cannot_sandbox(tmp_path):
    env = _stub_engine(tmp_path, "echo ran\n")
    with pytest.raises(ConfigError, match="refuses"):
        hostrun.run_on_host(
            _cfg(), ["claude", "-p", "x"], timeout=5, env=env, run_id="bbb222bbb222"
        )
    assert not hostrun.host_dir("bbb222bbb222").exists()


def test_run_on_host_repo_setup_failure_is_78_and_never_starts_the_engine(tmp_path):
    env = _stub_engine(tmp_path, "echo engine-started\n")

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "franky-repo-setup: nope\n")

    code, out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=5, env=env, runner=runner,
        run_id="ccc333ccc333", repo_setup={"repo": "me/repo", "ref": "a" * 40, "engine": "claude"},
    )  # fmt: skip
    text = "".join(out.chunks())
    assert code == 78 and "repository setup failed" in text and "engine-started" not in text


def test_session_copy_out_maps_the_host_slug_back_to_the_container_slug(tmp_path):
    work = tmp_path / "run" / "work"
    slug = hostrun.work_slug(work)
    sid = "11111111-1111-1111-1111-111111111111"
    home = tmp_path / "home"
    session = home / ".claude" / "projects" / slug
    session.mkdir(parents=True)
    (session / f"{sid}.jsonl").write_text("{}\n")
    (session / sid).mkdir()
    (session / sid / "x.txt").write_text("side")
    (session / sid / "link").symlink_to("/etc/passwd")
    dest = tmp_path / "dest"
    paths = [f".claude/projects/-work/{sid}.jsonl", f".claude/projects/-work/{sid}"]
    assert hostrun.copy_session(home, paths, dest, slug, max_bytes=1000) == "ok"
    assert (dest / paths[0]).is_file() and (dest / paths[1] / "x.txt").read_text() == "side"
    assert not (dest / paths[1] / "link").exists()
    assert hostrun.copy_session(home, paths, tmp_path / "d2", slug, max_bytes=3) == "too_large"
    assert hostrun.copy_session(home, paths, tmp_path / "d3", "other", max_bytes=1000) == "failed"


def test_session_tar_lands_under_the_run_slug(tmp_path):
    env = _stub_engine(tmp_path, 'ls "$HOME/.claude/projects"\n')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"{}"
        info = tarfile.TarInfo(".claude/projects/-work/s.jsonl")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_path = tmp_path / "s.tar.gz"
    tar_path.write_bytes(buf.getvalue())
    code, out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=30, env=env,
        session_tar=str(tar_path), run_id="ddd444ddd444",
    )  # fmt: skip
    text = "".join(out.chunks()).strip()
    assert code == 0 and text.startswith("-") and text != "-work"


# ---------------------------------------------------------------- job control


def _record(pgid, marker="Tue Nov  4 12:00:00 2026"):
    return {"runtime": "host", "pgid": pgid, "pgid_started_at": marker}


def test_group_state_requires_a_matching_start_marker():
    ok = lambda pgid, sig: None  # noqa: E731
    gone = lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError())  # noqa: E731
    denied = lambda pgid, sig: (_ for _ in ()).throw(PermissionError())  # noqa: E731
    same = lambda pid: "Tue Nov  4 12:00:00 2026"  # noqa: E731
    assert hostrun.group_state(_record(4242), killpg=ok, marker_of=same) is True
    assert hostrun.group_state(_record(4242), killpg=gone, marker_of=same) is False
    assert hostrun.group_state(_record(4242), killpg=denied, marker_of=same) is None
    assert hostrun.group_state(_record(4242), killpg=ok, marker_of=lambda p: "other") is None
    assert hostrun.group_state(_record(4242), killpg=ok, marker_of=lambda p: None) is None
    assert hostrun.group_state({"runtime": "host", "pgid": 4242}, killpg=ok) is None
    assert hostrun.group_state(_record(1), killpg=ok, marker_of=same) is None


def test_kill_run_never_signals_an_unverified_group():
    sent = []

    def killpg(pgid, sig):
        sent.append((pgid, sig))

    # The group exists (signal 0 probe) but its leader's start marker differs: a reused pgid.
    assert hostrun.kill_run(_record(4242), killpg=killpg, marker_of=lambda p: "other") is False
    assert sent == [(4242, 0)]


def test_kill_run_terms_then_kills_a_stubborn_verified_group():
    sent = []

    def killpg(pgid, sig):
        sent.append(sig)

    rec = _record(4242, marker="M")
    assert hostrun.kill_run(rec, killpg=killpg, sleeper=lambda s: None, marker_of=lambda p: "M")
    assert sent.count(signal.SIGTERM) == 1 and sent[-1] == signal.SIGKILL


def test_job_status_reports_a_host_run_by_its_process_group(monkeypatch):
    rec = jobs.new_record(
        job_id="eee555eee555", command="build", repo="me/repo", engine="claude", task="t",
        container="c", network="n", proxy="p", branch=None, started_at=jobs.now_iso(),
    )  # fmt: skip
    rec.update(_record(4242))
    jobs.write_record(rec, os.environ)
    monkeypatch.setattr(hostrun, "group_state", lambda record: True)
    res = CliRunner().invoke(cli.main, ["job", "status", "eee555eee555", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["container_running"] is True and data["runtime"] == "host"
    monkeypatch.setattr(hostrun, "group_state", lambda record: None)
    res = CliRunner().invoke(cli.main, ["job", "status", "eee555eee555", "--json"])
    assert json.loads(res.output)["state"] == "unknown"


def test_job_attach_delivers_to_the_host_steer_file(monkeypatch):
    rec = jobs.new_record(
        job_id="fff666fff666", command="build", repo="me/repo", engine="claude", task="t",
        container="c", network="n", proxy="p", branch=None, started_at=jobs.now_iso(),
    )  # fmt: skip
    rec.update(_record(4242))
    jobs.write_record(rec, os.environ)
    home = hostrun.host_dir("fff666fff666") / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(hostrun, "group_state", lambda record: True)
    res = CliRunner().invoke(cli.main, ["job", "attach", "fff666fff666", "-m", "use tabs"])
    assert res.exit_code == 0, res.output
    assert "use tabs" in (home / ".franky-steer.md").read_text()
    assert stat.S_IMODE(os.lstat(home / ".franky-steer.md").st_mode) == 0o600


# ---------------------------------------------------------------- codex host auth store


def test_codex_host_auth_store_login_status_logout(tmp_path):
    assert hostrun.codex_auth_ready() is False

    def runner(argv, env=None, **kw):
        assert env["CODEX_HOME"] == env["HOME"]
        (Path(env["CODEX_HOME"]) / "auth.json").write_text('{"tokens": {"access": "abcdefgh1234"}}')
        return subprocess.CompletedProcess(argv, 0)

    assert hostrun.codex_auth_login(runner=runner) is True
    store = hostrun.auth_dir()
    assert stat.S_IMODE(os.lstat(store).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(store / "auth.json").st_mode) == 0o600
    assert [p.name for p in store.iterdir()] == ["auth.json"]  # scratch login home removed
    assert hostrun.codex_auth_ready() is True
    assert hostrun.codex_auth_secrets() == ["abcdefgh1234"]
    assert hostrun.codex_auth_logout() is True
    assert hostrun.codex_auth_ready() is False


def test_codex_run_uses_the_host_store_and_writes_a_refreshed_token_back(tmp_path, monkeypatch):
    store = hostrun.auth_dir()
    store.mkdir(mode=0o700, parents=True)
    (store / "auth.json").write_text('{"tokens": {"access": "oldtoken1234"}}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "home=$CODEX_HOME"; cat "$CODEX_HOME/auth.json"; echo\n'
        'echo \'{"tokens": {"access": "newtoken5678"}}\' > "$CODEX_HOME/auth.json"\n'
    )
    stub.chmod(0o755)
    shim = bin_dir / "sandbox-exec"  # hermetic: drop `-f <profile>` and run the rest
    shim.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setattr(hostrun, "SANDBOX_EXEC", str(shim))
    cfg = Config(engine=CodexEngine(), allowed_repos=["me/repo"], runtime="host",
                 auth_volume="franky-codex-auth")  # fmt: skip
    code, out = hostrun.run_on_host(
        cfg, CodexEngine().inner_argv("p", model=None), timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, run_id="123abc123abc",
    )  # fmt: skip
    text = "".join(out.chunks())
    assert code == 0 and "oldtoken1234" not in text and "***REDACTED***" in text
    assert json.loads((store / "auth.json").read_text())["tokens"]["access"] == "newtoken5678"


# ---------------------------------------------------------------- cli wiring


def _host_env(monkeypatch):
    env = {
        **CLAUDE_ENV,
        RUNTIME_VAR: "host",
        "FRANKY_CONFIG_FILE": os.environ["FRANKY_CONFIG_FILE"],
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(hostrun, "preflight", lambda cfg: None)
    return env


def test_build_no_publish_is_refused_in_host_mode(monkeypatch):
    _host_env(monkeypatch)
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo", "--no-publish"])
    assert res.exit_code == 3, res.output
    assert "build --no-publish is not supported with FRANKY_RUNTIME=host" in res.output


def test_bad_runtime_value_is_exit_3(monkeypatch):
    env = _host_env(monkeypatch)
    env[RUNTIME_VAR] = "kubernetes"
    res = CliRunner().invoke(cli.main, ["build", "do it", "--repo", "me/repo"])
    assert res.exit_code == 3 and "FRANKY_RUNTIME must be one of" in res.output


def test_ensure_runtime_never_touches_docker_in_host_mode(monkeypatch):
    monkeypatch.setattr(hostrun, "preflight", lambda cfg: None)

    def boom(*a, **k):
        raise AssertionError("docker must not be used in host mode")

    monkeypatch.setattr(cli, "_ensure_images", boom)
    assert cli._ensure_runtime(_cfg()) == ("", "")


def test_run_pass_routes_a_host_config_to_the_host_runner(monkeypatch):
    seen = {}

    def fake_host(cfg, inner_argv, **kwargs):
        seen["argv"], seen["kwargs"] = inner_argv, kwargs
        return 0, "ok"

    def no_container(*a, **k):
        raise AssertionError("container runner must not run")

    monkeypatch.setattr(cli.hostrun, "run_on_host", fake_host)
    monkeypatch.setattr(cli, "run_in_container", no_container)
    code, out, _ = cli._run_pass(_cfg(), "do it", "", "", timeout=77, docker_start="deferred")
    assert code == 0 and seen["kwargs"]["timeout"] == 77
    assert "without Docker" in seen["argv"][2]  # the prompt tells the agent
    assert "docker_start" not in seen["kwargs"]


def test_container_runner_args_are_identical_with_the_runtime_unset(monkeypatch):
    """The container path must not change: same positional argv and keywords as before the host
    runtime existed, for a plain pass and a deferred-docker pass."""
    seen = []

    def fake_run(cfg, inner_argv, **kwargs):
        seen.append((inner_argv, kwargs))
        return 0, "ok"

    monkeypatch.setattr(cli, "run_in_container", fake_run)
    monkeypatch.setattr(
        cli.hostrun, "run_on_host", lambda *a, **k: (_ for _ in ()).throw(AssertionError("host"))
    )
    cfg = Config(engine=ClaudeEngine(), allowed_repos=["me/repo"])
    assert cfg.runtime == "container"
    cli._run_pass(cfg, "do it", "img", "proxy", run_id="abc", timeout=9)
    cli._run_pass(cfg, "do it", "img", "proxy", docker_start="deferred")
    (argv1, kw1), (argv2, kw2) = seen
    assert argv1 == ClaudeEngine().inner_argv("do it", model=None)
    assert kw1 == {
        "image": "img", "proxy_image": "proxy", "profile_bundle": None, "progress": None,
        "run_id": "abc", "diagnostics_sink": None, "snapshot_sink": None,
        "resume_workspace": None, "timeout": 9,
    }  # fmt: skip
    assert kw2["docker_start"] == "deferred" and "franky-docker-start" in argv2[2]
    assert "without Docker" not in argv2[2]


def test_auth_codex_commands_use_the_host_store_in_host_mode(monkeypatch):
    _host_env(monkeypatch)
    calls = []
    monkeypatch.setattr(hostrun, "codex_auth_login", lambda: calls.append("login") or True)
    monkeypatch.setattr(cli, "_codex_auth_image", lambda: (_ for _ in ()).throw(AssertionError))
    res = CliRunner().invoke(cli.main, ["auth", "login", "codex"])
    assert res.exit_code == 0 and calls == ["login"], res.output
    monkeypatch.setattr(hostrun, "codex_auth_ready", lambda env=None: True)
    res = CliRunner().invoke(cli.main, ["auth", "status", "codex"])
    assert res.exit_code == 0 and "ready" in res.output
    monkeypatch.setattr(hostrun, "codex_auth_logout", lambda env=None: True)
    res = CliRunner().invoke(cli.main, ["auth", "logout", "codex"])
    assert res.exit_code == 0 and "removed" in res.output


def test_python_is_the_hosts_interpreter_for_the_private_prompt_and_repo_setup(tmp_path):
    """A private-prompt pass with repository setup nests the in-image helpers; on the host they
    become this interpreter and the packaged mapper, never `python3` or the image path."""
    env = _stub_engine(tmp_path, 'echo "$@" | cut -c1-80\n')
    seen = {}

    def popen(argv, **kw):
        seen["argv"] = argv
        return subprocess.Popen(["/bin/sh", "-c", "exit 0"], **{**kw, "env": kw["env"]})

    def runner(argv, **kw):
        if argv[0] == sys.executable:
            seen["setup"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    inner = [
        "python3", "-c", "x", "/tmp/franky-private-prompt", "__FRANKY_PRIVATE_PROMPT__",
        "/usr/local/bin/franky-repo-setup", "exec", "--marker", "M", "--",
        *ClaudeEngine().inner_argv("__FRANKY_PRIVATE_PROMPT__", model=None),
    ]  # fmt: skip
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"prompt at /work and /home/franky"
        info = tarfile.TarInfo("franky-private-prompt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    code, out = hostrun.run_on_host(
        _cfg(), inner, timeout=10, env=env, popen=popen, runner=runner,
        private_prompt_tar=buf.getvalue(), run_id="999aaa999aaa",
        repo_setup={"repo": "me/repo", "ref": "a" * 40, "engine": "claude"},
    )  # fmt: skip
    argv = seen["argv"]
    assert (
        argv[0] == sys.executable and argv[argv.index(str(hostrun.REPO_SETUP_SCRIPT)) + 1] == "exec"
    )
    assert "/usr/local/bin/franky-repo-setup" not in argv and "python3" not in argv
    assert seen["setup"][:3] == [sys.executable, str(hostrun.REPO_SETUP_SCRIPT), "map"]
    assert gzip is not None  # imported for the bundle helpers above
