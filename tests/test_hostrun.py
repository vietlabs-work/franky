"""Host runtime (`FRANKY_RUNTIME=host`): selector, sandboxed argv, from-scratch env, the run loop
with a stub engine on PATH, job control, the Codex host auth store, and the guarantee that the
container path is unchanged. Hermetic: no Docker, no network, no real engine."""

import gzip
import io
import json
import os
import shutil
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
REAL_SANDBOX_EXEC = hostrun.SANDBOX_EXEC
DARWIN = sys.platform == "darwin" and Path(REAL_SANDBOX_EXEC).exists()


@pytest.fixture(autouse=True)
def _hermetic_host_root(tmp_path, monkeypatch):
    """A private runs root per test (never /Users/Shared) and a `sandbox-exec` shim that drops
    `-f <profile>` and runs the rest, so the run-loop tests are hermetic on every platform. The
    real profile is exercised by the darwin-only tests, which use REAL_SANDBOX_EXEC."""
    monkeypatch.setenv(hostrun.RUNS_ROOT_VAR, str(tmp_path / "hostroot" / "franky-host"))
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sandbox-exec"
    shim.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setattr(hostrun, "SANDBOX_EXEC", str(shim))
    # Hermetic run-loop tests use a stub engine that is not under any profile, so the kernel's
    # sandbox evidence cannot see it; they use the fallback identity. The tests that exercise
    # the kernel mode say so.
    monkeypatch.setattr(hostrun, "EVIDENCE_MODE", "fallback")


CLAUDE_ENV = {
    "FRANKY_ALLOWED_REPOS": "me/repo",
    "GH_TOKEN": "ghp_hostsecret0001",
    "CLAUDE_CODE_OAUTH_TOKEN": "tok-hostsecret0002",
    "FRANKY_ENGINE": "claude",
}


PYTHON = next(
    (p for p in ("/opt/homebrew/bin/python3", "/usr/bin/python3") if Path(p).exists()), ""
)

# A launchd job submission through liblaunch, from inside the sandbox. A process outside any
# profile that runs this starts a job that touches OUTFILE outside the sandbox.
LAUNCHD_SUBMIT = """
import ctypes, ctypes.util, sys
lib = ctypes.CDLL(ctypes.util.find_library("System"))
for name in ("launch_data_alloc", "launch_data_new_string", "launch_data_new_bool",
             "launch_data_dict_insert", "launch_data_array_set_index", "launch_msg",
             "launch_data_get_type", "launch_data_get_errno"):
    getattr(lib, name).restype = ctypes.c_void_p if name.startswith("launch_data_new") or name in (
        "launch_data_alloc", "launch_msg") else ctypes.c_int
lib.launch_data_alloc.argtypes = [ctypes.c_int]
lib.launch_data_new_string.argtypes = [ctypes.c_char_p]
lib.launch_data_new_bool.argtypes = [ctypes.c_bool]
lib.launch_data_dict_insert.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
lib.launch_data_array_set_index.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
lib.launch_msg.argtypes = [ctypes.c_void_p]
lib.launch_data_get_type.argtypes = [ctypes.c_void_p]
lib.launch_data_get_errno.argtypes = [ctypes.c_void_p]
job = lib.launch_data_alloc(1)
lib.launch_data_dict_insert(job, lib.launch_data_new_string(b"com.franky.test.submit"), b"Label")
args = lib.launch_data_alloc(2)
lib.launch_data_array_set_index(args, lib.launch_data_new_string(b"/usr/bin/touch"), 0)
lib.launch_data_array_set_index(args, lib.launch_data_new_string(b"OUTFILE"), 1)
lib.launch_data_dict_insert(job, args, b"ProgramArguments")
lib.launch_data_dict_insert(job, lib.launch_data_new_bool(True), b"RunAtLoad")
msg = lib.launch_data_alloc(1)
lib.launch_data_dict_insert(msg, job, b"SubmitJob")
resp = lib.launch_msg(msg)
sys.exit(lib.launch_data_get_errno(resp) if resp and lib.launch_data_get_type(resp) == 9 else 2)
"""


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


def test_engine_argv_wraps_the_engine_in_the_outer_profile(tmp_path):
    profile, settings = tmp_path / "p.sb", tmp_path / "s.json"
    claude_bin, codex_bin = Path("/opt/x/claude-real"), Path("/opt/x/codex-real")
    out = hostrun.engine_argv(
        ClaudeEngine().inner_argv("p", model=None),
        "claude",
        profile=profile,
        engine_bin=claude_bin,
        settings=settings,
    )
    assert out[:4] == [hostrun.SANDBOX_EXEC, "-f", str(profile), str(claude_bin)]
    assert out[4:8] == ["--setting-sources", "", "--settings", str(settings)]
    assert "--dangerously-skip-permissions" in out  # headless approval; the profile bounds it
    out = hostrun.engine_argv(
        CodexEngine().inner_argv("p", model=None), "codex", profile=profile, engine_bin=codex_bin
    )
    assert out[:4] == [hostrun.SANDBOX_EXEC, "-f", str(profile), str(codex_bin)]
    assert "--dangerously-bypass-approvals-and-sandbox" not in out
    # Codex's own sandbox cannot nest inside the profile; the profile is the sandbox.
    assert out[out.index("--sandbox") + 1] == "danger-full-access"
    with pytest.raises(ValueError):
        hostrun.engine_argv(["codex", "exec", "p"], "codex", profile=profile, engine_bin=codex_bin)
    with pytest.raises(ValueError):
        hostrun.engine_argv(
            PiEngine().inner_argv("p", model=None), "pi", profile=profile, engine_bin=codex_bin
        )


def test_claude_control_settings_turn_hooks_and_its_own_sandbox_off():
    s = hostrun.claude_control_settings()
    assert s["disableAllHooks"] is True and s["sandbox"] == {"enabled": False}
    assert s["permissions"]["deny"] == ["WebFetch", "WebSearch"]


def _profile(tmp_path, roots=(), control=None):
    home = tmp_path / "real-home"
    root = tmp_path / "root"
    run = root / ("a" * 32)
    for d in (home, run / "home", run / "work", run / "tmp"):
        d.mkdir(parents=True)
    return home, root, run, hostrun.seatbelt_profile(run, home, root, list(roots), control)


def test_seatbelt_profile_is_deny_default_with_narrow_allows(tmp_path):
    ctl = tmp_path / "root" / ("a" * 32 + ".ctl") / "s.json"
    home, root, run, text = _profile(tmp_path, [Path("/opt/x/codex")], ctl)
    lines = text.splitlines()
    assert lines[:2] == ["(version 1)", "(deny default)"]
    assert "(allow network-outbound (remote tcp))" in lines
    assert '(allow network-outbound (literal "/private/var/run/mDNSResponder"))' in lines
    deny_read = next(ln for ln in lines if ln.startswith("(deny file-read* file-write*"))
    for path in ("/Users", home, root, "/private/var/folders", "/private/tmp"):
        assert f'(subpath "{path}")' in deny_read
    write = next(ln for ln in lines if ln.startswith("(allow file-write* "))
    assert write.count("subpath") == 3  # home, work, tmp only, never the run dir itself
    assert f'(subpath "{run}")' not in write and f'(subpath "{run}/home")' in write
    assert '(allow file-read* (subpath "/opt/x/codex"))' in lines
    assert f'(allow file-read* (literal "{ctl}"))' in lines
    assert any(
        "com.apple.SecurityServer" in ln and ln.startswith("(deny mach-lookup") for ln in lines
    )
    allow_exec = next(ln for ln in lines if ln.startswith("(allow process-exec"))
    for r in ("/usr", "/bin", "/opt/homebrew", "/opt/x/codex"):
        assert f'(subpath "{r}")' in allow_exec
    # Exec is never allowed from the run directory or the home.
    assert str(run) not in allow_exec and str(home) not in allow_exec
    deny_exec = next(ln for ln in lines if ln.startswith("(deny process-exec"))
    assert "/bin/launchctl" in deny_exec and "/usr/bin/security" in deny_exec
    assert f'(subpath "{root}")' in deny_exec  # nothing in the runs root is ever executable
    # Later rules win: the denies for exec and Mach services close the profile.
    assert lines.index(deny_exec) > lines.index(write)


def test_track_descendants_follows_parent_links_and_ignores_a_reused_pid():
    rows = [
        (10, 1, 10, "t10", 1.0),
        (11, 10, 10, "t11", 1.0),
        (12, 11, 12, "t12", 1.0),  # grandchild that called setsid: own group
        (20, 1, 20, "t20", 1.0),  # unrelated
    ]
    seen = {}
    hostrun.track_descendants(10, seen, rows)
    assert seen == {11: "t11", 12: "t12"}
    # A later walk keeps what is still the same process and adds its new children.
    hostrun.track_descendants(
        10, seen, [(11, 10, 10, "t11", 1.0), (12, 11, 12, "t12", 1.0), (13, 12, 12, "t13", 1.0)]
    )
    assert seen == {11: "t11", 12: "t12", 13: "t13"}


def test_a_seen_parent_whose_pid_was_reused_is_dropped_and_not_walked():
    seen = {11: "t11"}
    rows = [
        (10, 1, 10, "t10", 1.0),
        (11, 1, 11, "REUSED", 9.0),  # pid 11 now belongs to an unrelated process
        (30, 11, 11, "t30", 9.0),  # whose child must not be adopted
    ]
    hostrun.track_descendants(10, seen, rows)
    assert seen == {}
    seen = {11: "t11"}
    hostrun.track_descendants(10, seen, [(10, 1, 10, "t10", 1.0)])  # pid gone
    assert seen == {}


def test_engine_install_roots_refuse_a_root_that_contains_the_home_or_the_runs_root(tmp_path):
    home = tmp_path / "h"
    root = tmp_path / "h" / ".runs"
    pkg = home / ".local" / "share" / "tool"
    (pkg / "bin").mkdir(parents=True)
    exe = pkg / "bin" / "engine"
    exe.write_text("x")
    assert hostrun.engine_install_roots(exe, home, root) == [pkg / "bin"]
    # A launcher whose directory contains the home or the runs root yields no root at all.
    (tmp_path / "engine").write_text("x")
    assert hostrun.engine_install_roots(tmp_path / "engine", home, root) == []
    assert hostrun.engine_install_roots(tmp_path / "engine", tmp_path / "other", root) == []


@pytest.mark.skipif(not DARWIN, reason="needs macOS Seatbelt")
def test_seatbelt_profile_probes_under_the_real_sandbox(tmp_path):
    """Live probes under the generated profile: late files, reads of the home, writes to the
    system, launchd, the keychain tool, other processes' environments, and the run dir."""
    home = Path(os.path.realpath(Path.home()))
    root = hostrun.ensure_runs_root()
    token = "b" * 32
    run = root / token
    for d in (run, run / "home", run / "work", run / "tmp"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    (run / "work" / "f.txt").write_text("inside")
    (run / "work" / "ls.py").write_text(
        LAUNCHD_SUBMIT.replace("OUTFILE", str(run / "work" / "submitted.txt"))
    )
    profile = tmp_path / "p.sb"
    profile.write_text(hostrun.seatbelt_profile(run, home, root, []))
    # Control: the same submission from OUTSIDE any profile works, so the probe is meaningful.
    control = tmp_path / "control-submitted.txt"
    script = tmp_path / "ls.py"
    script.write_text(LAUNCHD_SUBMIT.replace("OUTFILE", str(control)))
    ran = subprocess.run([PYTHON, str(script)], capture_output=True).returncode == 0
    time.sleep(1.5)
    subprocess.run(["/bin/launchctl", "remove", "com.franky.test.submit"], capture_output=True)
    if not (ran and control.exists()):
        pytest.skip("launchd job submission is not available outside the sandbox here")
    late = home / f".franky-probe-late-{os.getpid()}"
    other = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"], env={"PROBE_SECRET_ENV": "leaky", "PATH": "/bin"}
    )
    script = (
        f"echo x > {late} 2>/dev/null; echo late_write=$?; "
        f"echo x > /opt/homebrew/bin/zz-probe 2>/dev/null; echo sys_write=$?; "
        f"ls {home} >/dev/null 2>&1; echo home_list=$?; "
        f"ls {root} >/dev/null 2>&1; echo root_list=$?; "
        "ls /Users/Shared >/dev/null 2>&1; echo shared_list=$?; "
        f"cat {run}/work/f.txt; echo inside_read=$?; echo y > {run}/work/o.txt; echo inside_write=$?; "
        f"echo y > {run}/escape.txt 2>/dev/null; echo rundir_write=$?; "
        "/bin/launchctl print gui/$(id -u) >/dev/null 2>&1; echo launchctl=$?; "
        "/usr/bin/security find-generic-password -s gh:github.com >/dev/null 2>&1; "
        "echo security=$?; "
        f"cp /bin/launchctl {run}/tmp/lc 2>/dev/null; {run}/tmp/lc print gui/$(id -u) "
        ">/dev/null 2>&1; echo copied_launchctl=$?; "
        f"cp /bin/sh {run}/tmp/sh2; {run}/tmp/sh2 -c true >/dev/null 2>&1; echo copied_sh=$?; "
        f"cp /bin/sh {run}/work/sh3; {run}/work/sh3 -c true >/dev/null 2>&1; echo copied_sh_work=$?; "
        f"{PYTHON} {run}/work/ls.py >/dev/null 2>&1; echo python_submit=$?; "
        f"/bin/ps eww -p {other.pid} 2>&1 | grep -c PROBE_SECRET_ENV; "
        "/usr/bin/curl -s -o /dev/null -w 'tcp=%{http_code}\\n' https://api.github.com"
    )
    try:
        late.write_text("created after launch, before the probe")  # after the profile exists
        proc = subprocess.run(
            [REAL_SANDBOX_EXEC, "-f", str(profile), "/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            cwd=run / "work",
            timeout=60,
        )
        out = proc.stdout
        assert "late_write=1" in out and "sys_write=1" in out
        assert "home_list=1" in out and "root_list=1" in out and "shared_list=1" in out
        assert "inside_read=0" in out and "inside" in out and "inside_write=0" in out
        assert "rundir_write=1" in out
        assert "launchctl=0" not in out and "launchctl=" in out
        assert "security=0" not in out and "security=" in out
        assert "copied_launchctl=0" not in out and "copied_launchctl=" in out
        # Nothing a run drops in its own directories can be executed.
        assert "copied_sh=0" not in out and "copied_sh_work=0" not in out
        assert "copied_sh=" in out and "copied_sh_work=" in out
        assert "python_submit=0" not in out and "python_submit=" in out
        assert not (run / "work" / "submitted.txt").exists()
        assert "PROBE_SECRET_ENV" not in out and "\n0\n" in out  # no env of other processes
        # A file that exists in the home is unreadable too (not only the ones made later).
        read = subprocess.run(
            [REAL_SANDBOX_EXEC, "-f", str(profile), "/bin/cat", str(late)],
            capture_output=True,
            text=True,
        )
        assert read.returncode != 0 and "created after" not in read.stdout
    finally:
        other.kill()
        other.wait()
        late.unlink(missing_ok=True)
        snapshot_rmtree = hostrun.snapshot._rmtree
        snapshot_rmtree(run)
        os.chmod(root, 0o700)
        snapshot_rmtree(root)


def test_runs_root_is_created_0300_and_refuses_the_home_and_wrong_modes(tmp_path, monkeypatch):
    root = hostrun.ensure_runs_root()
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o300
    os.chmod(root, 0o700)
    with pytest.raises(ConfigError, match="mode 0300"):
        hostrun.ensure_runs_root()
    os.chmod(root, 0o300)
    monkeypatch.setenv(hostrun.RUNS_ROOT_VAR, str(Path.home() / ".franky-hostroot-test"))
    with pytest.raises(ConfigError, match="outside your home"):
        hostrun.ensure_runs_root()


def test_host_dir_of_accepts_only_a_random_directory_directly_under_the_root():
    root = hostrun.ensure_runs_root()
    good = root / ("c" * 32)
    good.mkdir(mode=0o700)
    assert hostrun.host_dir_of({"host_dir": str(good)}) == good
    assert hostrun.host_dir_of({"host_dir": str(root / "short")}) is None
    assert hostrun.host_dir_of({"host_dir": str(root.parent / ("c" * 32))}) is None
    assert hostrun.host_dir_of({"host_dir": str(root / "x" / ("c" * 32))}) is None
    assert hostrun.host_dir_of({}) is None
    link = root / ("d" * 32)
    link.symlink_to(good)
    assert hostrun.host_dir_of({"host_dir": str(link)}) is None


def test_preflight_refuses_without_the_engine_or_a_sandbox():
    cfg = _cfg()
    with pytest.raises(ConfigError, match="needs macOS Seatbelt"):
        hostrun.preflight(cfg, which=lambda b: "/bin/" + b, platform="linux")
    with pytest.raises(ConfigError, match="needs macOS Seatbelt"):
        hostrun.preflight(_cfg(CodexEngine()), which=lambda b: "/bin/" + b, platform="win32")
    if not Path(REAL_SANDBOX_EXEC).exists():
        return
    with pytest.raises(ConfigError, match="needs `claude` on PATH"):
        hostrun.preflight(cfg, which=lambda b: None, platform="darwin")


def test_preflight_refuses_when_sandbox_exec_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(hostrun, "SANDBOX_EXEC", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="needs .*nope"):
        hostrun.preflight(_cfg(), which=lambda b: "/bin/" + b, platform="darwin")


def test_child_env_is_built_from_scratch():
    cfg = _cfg()
    env = hostrun.child_env(
        cfg,
        Path("/r/home"),
        Path("/r/tmp"),
        {"PATH": "/p", "AWS_SECRET_ACCESS_KEY": "nope", "SSH_AUTH_SOCK": "/s", "LANG": "C"},
    )
    env.pop("SSL_CERT_FILE", None)  # present only where the system CA bundle exists
    assert set(env) == {
        "PATH", "LANG", "TERM", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "GIT_TERMINAL_PROMPT", "GH_TOKEN",
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GH_CONFIG_DIR", "CLAUDE_CODE_TMPDIR",
    }  # fmt: skip
    assert env["HOME"] == "/r/home" and env["TMPDIR"] == "/r/tmp"
    # The operator's git and gh configuration (osxkeychain helper, signing) never applies.
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/r/home/.gitconfig"
    assert env["GH_CONFIG_DIR"] == "/r/home/.config/gh"


def test_repo_setup_script_ships_in_the_package_beside_the_mapper():
    assert hostrun.REPO_SETUP_SCRIPT.read_text() == (ROOT / "franky-repo-setup.py").read_text()


# ---------------------------------------------------------------- the run


def test_run_on_host_runs_in_a_private_dir_with_a_scratch_env_and_cleans_up(tmp_path):
    env = _stub_engine(
        tmp_path,
        'echo "cwd=$PWD home=$HOME tmp=$TMPDIR"\n'
        'echo "token=$GH_TOKEN leak=${AWS_SECRET:-unset}"\n'
        'echo "args=$*"\n'
        'cat "$4" > /dev/null\n',
    )
    env["AWS_SECRET"] = "must-not-pass"
    cfg = _cfg()
    run_id = "abc123abc123"
    jobs.write_record({"job_id": run_id, "status": "running"}, os.environ)
    argv = ClaudeEngine().inner_argv("see /home/franky/x and /work/repo", model=None)
    code, out = hostrun.run_on_host(cfg, argv, timeout=30, env=env, run_id=run_id)
    text = "".join(out.chunks())
    record = jobs.read_record(run_id, os.environ)
    base = Path(record["host_dir"])
    assert code == 0, text
    assert f"cwd={base}/work" in text
    assert "leak=unset" in text
    assert "ghp_hostsecret0001" not in text and "token=***REDACTED***" in text
    assert f"{base}/home/x" in text and "/work/repo" not in text.replace(f"{base}/work/repo", "")
    assert not base.exists() and not Path(f"{base}.ctl").exists()  # run and control dirs removed
    assert record["runtime"] == "host" and isinstance(record["pgid"], int)
    assert base.parent == hostrun.ensure_runs_root() and hostrun.RUN_TOKEN_RE.match(base.name)
    assert isinstance(record["pgid_started_epoch"], float) and record["window_secs"] > 30


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


def _session_run(tmp_path, slug):
    base = tmp_path / "run"
    sid = "11111111-1111-1111-1111-111111111111"
    session = base / "home" / ".claude" / "projects" / slug
    session.mkdir(parents=True)
    (session / f"{sid}.jsonl").write_text("{}\n")
    (session / sid).mkdir()
    (session / sid / "x.txt").write_text("side")
    return base, sid, session


def test_session_copy_out_maps_the_host_slug_back_to_the_container_slug(tmp_path):
    slug = hostrun.work_slug(tmp_path / "run" / "work")
    base, sid, session = _session_run(tmp_path, slug)
    (session / sid / "link").symlink_to("/etc/passwd")
    dest = tmp_path / "dest"
    paths = [f".claude/projects/-work/{sid}.jsonl", f".claude/projects/-work/{sid}"]
    assert hostrun.copy_session(base, paths, dest, slug, max_bytes=1000) == "ok"
    assert (dest / paths[0]).is_file() and (dest / paths[1] / "x.txt").read_text() == "side"
    assert not (dest / paths[1] / "link").exists()
    assert hostrun.copy_session(base, paths, tmp_path / "d2", slug, max_bytes=3) == "too_large"
    assert hostrun.copy_session(base, paths, tmp_path / "d3", "other", max_bytes=1000) == "failed"


def test_session_copy_out_never_follows_a_planted_link(tmp_path):
    slug = hostrun.work_slug(tmp_path / "run" / "work")
    base, sid, session = _session_run(tmp_path, slug)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{sid}.jsonl").write_text("STOLEN")
    # The projects dir is swapped for a link to a directory outside the run.
    projects = base / "home" / ".claude" / "projects"
    stash = tmp_path / "stash"
    projects.rename(stash)
    projects.symlink_to(outside)
    paths = [f".claude/projects/-work/{sid}.jsonl"]
    assert hostrun.copy_session(base, paths, tmp_path / "d", slug, max_bytes=1000) == "failed"
    assert not (tmp_path / "d").exists() or not any((tmp_path / "d").rglob("*.jsonl"))
    # A session file that is itself a link, and a file past the limit while reading.
    projects.unlink()
    stash.rename(projects)
    f = session / f"{sid}.jsonl"
    f.unlink()
    f.symlink_to(outside / f"{sid}.jsonl")
    assert hostrun.copy_session(base, paths, tmp_path / "d4", slug, max_bytes=1000) == "failed"


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


def _ident(pgid=4242, started=1_000_000.0, window=2000):
    return {"pgid": pgid, "pgid_started_epoch": started, "window_secs": window}


def _host_record(job_id, base=None, **extra):
    rec = jobs.new_record(
        job_id=job_id, command="build", repo="me/repo", engine="claude", task="t",
        container="c", network="n", proxy="p", branch=None, started_at=jobs.now_iso(),
    )  # fmt: skip
    rec.update({"runtime": "host", **_ident()})
    if base is not None:
        rec["host_dir"] = str(base)
    rec.update(extra)
    jobs.write_record(rec, os.environ)
    return rec


def _run_dir(token="e" * 32):
    root = hostrun.ensure_runs_root()
    base = root / token
    (base / "home").mkdir(parents=True, mode=0o700)
    return base


class FakeHost:
    """A process table and a `kill` for the teardown tests. `profiled` is the kernel's answer
    (which pids run under this run's profile); `dies_on` maps a signal to whether it removes the
    process; `on_term` can spawn a new profiled child when a TERM lands."""

    def __init__(self, procs, profiled, dies_on=(9,), on_term=None):
        self.procs = dict(procs)  # pid -> (start text, start epoch, pgid)
        self.profiled = set(profiled)
        self.dies_on = set(dies_on)
        self.on_term = on_term
        self.sent = []
        self.now = 0.0

    def table(self, runner=None):
        rows = [(pid, 1, g, text, e) for pid, (text, e, g) in self.procs.items()]
        return [(1, 0, 1, "init", 0.0), *rows]  # a real table is never empty

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        if pid in self.procs and sig in self.dies_on:
            del self.procs[pid]
        if sig == 15 and self.on_term:
            self.on_term(self)

    def check(self, pid, path):
        # home denied, the run's work allowed, the runs root denied: only for profiled pids.
        if pid not in self.profiled:
            return 0
        return 0 if str(path).endswith("/work") else 1

    def sleep(self, secs):
        self.now += secs

    def teardown(self, monkeypatch, **kw):
        monkeypatch.setattr(hostrun, "EVIDENCE_MODE", "kernel")
        monkeypatch.setattr(hostrun, "process_table", self.table)
        monkeypatch.setattr(hostrun.os, "kill", self.kill)
        root = Path("/runs")
        return hostrun.teardown(
            root / ("a" * 32), _ident(), home=Path("/home/x"), root=root, check=self.check,
            sleeper=self.sleep, clock=lambda: self.now, **kw,
        )  # fmt: skip


IN_WINDOW = 1_000_100.0


def test_an_unrelated_process_in_the_old_process_group_is_not_signalled(monkeypatch):
    """A reused pgid: a stranger sits in the recorded group but the kernel says it is not under
    this run's profile. No sandbox evidence, no signal."""
    host = FakeHost({4242: ("t", IN_WINDOW, 4242)}, profiled=[])
    assert host.teardown(monkeypatch) == hostrun.CLEAN
    assert host.sent == []


def test_a_profiled_process_is_killed_whatever_its_start_time(monkeypatch):
    """The kernel's answer is authoritative: a process under this run's profile that started far
    outside the window is the run's, and is stopped. It is never silently dropped (CLEAN)."""
    host = FakeHost({4242: ("t", 5_000_000.0, 4242)}, profiled=[4242])
    out = host.teardown(monkeypatch)
    assert out == hostrun.CLEAN and (4242, 15) in host.sent and (4242, 9) in host.sent
    assert not host.procs
    # And one that will not die is uncertain, never clean.
    stuck = FakeHost({4242: ("t", 5_000_000.0, 4242)}, profiled=[4242], dies_on=())
    assert stuck.teardown(monkeypatch, deadline=3.0) == hostrun.UNCERTAIN


def test_a_process_the_check_cannot_answer_for_is_uncertain_not_clean(monkeypatch):
    host = FakeHost({50: ("t50", IN_WINDOW, 50)}, profiled=[])

    def broken(pid, path):
        raise OSError("no answer")

    host.check = broken
    assert host.teardown(monkeypatch) == hostrun.UNCERTAIN
    base = _run_dir("7" * 32)
    rec = {"host_dir": str(base), **_ident()}
    monkeypatch.setattr(hostrun, "process_table", host.table)
    monkeypatch.setattr(hostrun, "_sandbox_check", lambda: broken)
    assert hostrun.run_state(rec) is None


def test_teardown_terms_then_kills_a_stubborn_proven_process_and_returns_clean(monkeypatch):
    host = FakeHost({50: ("t50", IN_WINDOW, 50)}, profiled=[50], dies_on=(9,))
    assert host.teardown(monkeypatch) == hostrun.CLEAN
    assert host.sent[0] == (50, 15) and host.sent[-1] == (50, 9)
    assert not host.procs


def test_a_child_forked_while_terminating_is_found_and_killed_in_a_later_pass(monkeypatch):
    def fork_on_term(host):
        host.procs[60] = ("t60", IN_WINDOW, 60)
        host.profiled.add(60)

    host = FakeHost(
        {50: ("t50", IN_WINDOW, 50)}, profiled=[50], dies_on=(15, 9), on_term=fork_on_term
    )
    assert host.teardown(monkeypatch) == hostrun.CLEAN
    assert (60, 9) in host.sent and not host.procs


def test_a_process_that_will_not_die_makes_teardown_uncertain(monkeypatch):
    host = FakeHost({50: ("t50", IN_WINDOW, 50)}, profiled=[50], dies_on=())
    assert host.teardown(monkeypatch, deadline=3.0) == hostrun.UNCERTAIN
    assert host.procs  # still there


def test_an_unreadable_process_table_is_uncertain_never_clean(monkeypatch):
    monkeypatch.setattr(hostrun, "process_table", lambda runner=None: None)
    monkeypatch.setattr(hostrun, "EVIDENCE_MODE", "kernel")
    root = Path("/runs")
    out = hostrun.teardown(root / ("a" * 32), _ident(), home=Path("/h"), root=root)
    assert out == hostrun.UNCERTAIN


def test_a_start_time_that_changed_before_the_signal_is_not_signalled(monkeypatch):
    host = FakeHost({50: ("t50", IN_WINDOW, 50)}, profiled=[50], dies_on=())
    real = host.table
    calls = {"n": 0}

    def table(runner=None):
        calls["n"] += 1
        rows = real(runner)
        if calls["n"] >= 2:  # the pid was reused between proof and signal
            return [(50, 1, 50, "REUSED", IN_WINDOW)]
        return rows

    host.table = table
    monkeypatch.setattr(hostrun, "EVIDENCE_MODE", "kernel")
    monkeypatch.setattr(hostrun, "process_table", table)
    monkeypatch.setattr(hostrun.os, "kill", host.kill)
    root = Path("/runs")
    hostrun.teardown(
        root / ("a" * 32), _ident(), home=Path("/h"), root=root, check=host.check,
        sleeper=host.sleep, clock=lambda: host.now, deadline=1.0,
    )  # fmt: skip
    assert host.sent == []


def test_track_descendants_follows_parent_links_and_drops_a_reused_pid():
    rows = [
        (10, 1, 10, "t10", 1.0),
        (11, 10, 10, "t11", 1.0),
        (12, 11, 12, "t12", 1.0),  # grandchild that called setsid: own group
        (20, 1, 20, "t20", 1.0),  # unrelated
    ]
    seen = {}
    hostrun.track_descendants(10, seen, rows)
    assert seen == {11: "t11", 12: "t12"}
    hostrun.track_descendants(
        10, seen, [(11, 10, 10, "t11", 1.0), (12, 11, 12, "t12", 1.0), (13, 12, 12, "t13", 1.0)]
    )
    assert seen == {11: "t11", 12: "t12", 13: "t13"}
    seen = {11: "t11"}
    hostrun.track_descendants(
        10, seen, [(10, 1, 10, "t10", 1.0), (11, 1, 11, "REUSED", 9.0), (30, 11, 11, "t30", 9.0)]
    )
    assert seen == {}


def test_run_state_is_none_when_the_table_or_the_record_cannot_say(monkeypatch):
    base = _run_dir("1" * 32)
    rec = {"host_dir": str(base), **_ident()}
    monkeypatch.setattr(hostrun, "process_table", lambda runner=None: None)
    assert hostrun.run_state(rec) is None
    assert hostrun.run_state({"host_dir": str(base)}) is None
    assert hostrun.run_state({}) is None


def test_job_status_reports_a_host_run_by_its_proven_processes(monkeypatch):
    _host_record("eee555eee555")
    monkeypatch.setattr(hostrun, "run_state", lambda record, **kw: True)
    res = CliRunner().invoke(cli.main, ["job", "status", "eee555eee555", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["container_running"] is True and data["runtime"] == "host"
    monkeypatch.setattr(hostrun, "run_state", lambda record, **kw: None)
    res = CliRunner().invoke(cli.main, ["job", "status", "eee555eee555", "--json"])
    assert json.loads(res.output)["state"] == "unknown"


def _orphan(job_id, token, **extra):
    base = _run_dir(token)
    _host_record(job_id, base, status="running", pid=2_000_000_000, **extra)
    return base


def test_orphan_status_with_a_failing_ps_keeps_the_directory_and_the_record(monkeypatch):
    base = _orphan("ab1000ab1000", "2" * 32)
    monkeypatch.setattr(hostrun, "process_table", lambda runner=None: None)
    res = CliRunner().invoke(cli.main, ["job", "status", "ab1000ab1000", "--json"])
    assert res.exit_code == 0, res.output
    assert base.exists() and jobs.read_record("ab1000ab1000", os.environ)["status"] == "running"


def test_orphan_status_keeps_everything_when_the_teardown_is_uncertain(monkeypatch):
    base = _orphan("ab2000ab2000", "3" * 32)
    monkeypatch.setattr(hostrun, "run_state", lambda record, **kw: True)
    monkeypatch.setattr(hostrun, "teardown_record", lambda record, **kw: hostrun.UNCERTAIN)
    res = CliRunner().invoke(cli.main, ["job", "status", "ab2000ab2000", "--json"])
    assert json.loads(res.output)["state"] == "unknown"
    assert base.exists() and jobs.read_record("ab2000ab2000", os.environ)["status"] == "running"


def test_orphan_status_removes_the_directory_only_after_a_clean_teardown(monkeypatch):
    base = _orphan("ab3000ab3000", "4" * 32)
    monkeypatch.setattr(hostrun, "run_state", lambda record, **kw: True)
    monkeypatch.setattr(hostrun, "teardown_record", lambda record, **kw: hostrun.CLEAN)
    CliRunner().invoke(cli.main, ["job", "status", "ab3000ab3000", "--json"])
    assert not base.exists()
    assert jobs.read_record("ab3000ab3000", os.environ)["status"] == "killed"


def test_job_kill_keeps_the_run_when_the_teardown_is_uncertain(monkeypatch):
    base = _orphan("ab4000ab4000", "5" * 32)
    monkeypatch.setattr(hostrun, "teardown_record", lambda record, **kw: hostrun.UNCERTAIN)
    res = CliRunner().invoke(cli.main, ["job", "kill", "ab4000ab4000", "--json"])
    assert res.exit_code != 0 and "cannot be verified stopped" in res.output
    assert base.exists() and jobs.read_record("ab4000ab4000", os.environ)["status"] == "running"


def test_job_kill_stops_then_removes_the_directory_after_a_clean_teardown(monkeypatch):
    base = _orphan("ab5000ab5000", "6" * 32)
    monkeypatch.setattr(hostrun, "teardown_record", lambda record, **kw: hostrun.CLEAN)
    res = CliRunner().invoke(cli.main, ["job", "kill", "ab5000ab5000", "--json"])
    assert res.exit_code == 0, res.output
    assert not base.exists() and jobs.read_record("ab5000ab5000", os.environ)["status"] == "killed"


def test_job_attach_delivers_to_the_host_steer_file_without_following_links(monkeypatch):
    base = _run_dir()
    _host_record("fff666fff666", base)
    monkeypatch.setattr(hostrun, "run_state", lambda record, **kw: True)
    res = CliRunner().invoke(cli.main, ["job", "attach", "fff666fff666", "-m", "use tabs"])
    assert res.exit_code == 0, res.output
    steer = base / "home" / ".franky-steer.md"
    assert "use tabs" in steer.read_text()
    assert stat.S_IMODE(os.lstat(steer).st_mode) == 0o600
    # A planted link at the steer path is refused, and a record naming a stray dir is ignored.
    steer.unlink()
    (base / "elsewhere").write_text("x")
    steer.symlink_to(base / "elsewhere")
    assert hostrun.deliver_steer({"host_dir": str(base)}, "x") is False
    assert hostrun.deliver_steer({"host_dir": str(base.parent.parent)}, "x") is False


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_a_setsid_child_holding_the_pipe_cannot_hang_the_reader_and_is_killed(tmp_path):
    marker = tmp_path / "escaped-pid"
    env = _stub_engine(
        tmp_path,
        'perl -e \'use POSIX; POSIX::setsid(); exec "sleep", "300"\' &\n'
        f"echo $! > {marker}\necho leader-done\n",
    )
    t0 = time.monotonic()
    code, out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=60, env=env,
        run_id="ccd999ccd999",
    )  # fmt: skip
    assert code == 0 and "leader-done" in "".join(out.chunks())
    assert time.monotonic() - t0 < 30  # the drain is bounded, not the child's 300 s
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # found by its working directory and stopped before the dir went


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_an_interrupt_during_the_spawn_still_tears_the_run_down(tmp_path):
    marker = tmp_path / "pid"
    env = _stub_engine(tmp_path, f"echo $$ > {marker}\nsleep 300\n")
    seen = {}

    def popen(argv, **kw):
        subprocess.Popen(argv, **kw)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["base"] = kw["cwd"]
        raise KeyboardInterrupt  # right after the process exists, before anything stored its pid

    with pytest.raises(KeyboardInterrupt):
        hostrun.run_on_host(
            _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=60, env=env, popen=popen,
            run_id="ee1111ee1111",
        )  # fmt: skip
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not Path(seen["base"]).parent.exists()


def test_an_uncertain_teardown_keeps_the_run_directory_and_says_so(tmp_path, monkeypatch):
    env = _stub_engine(tmp_path, "echo hi\n")
    monkeypatch.setattr(hostrun, "teardown", lambda *a, **k: hostrun.UNCERTAIN)
    jobs.write_record({"job_id": "ff2222ff2222", "status": "running"}, os.environ)
    code, out = hostrun.run_on_host(
        _cfg(), ClaudeEngine().inner_argv("p", model=None), timeout=30, env=env,
        run_id="ff2222ff2222",
    )  # fmt: skip
    record = jobs.read_record("ff2222ff2222", os.environ)
    assert Path(record["host_dir"]).exists() and record["host_unverified"] is True
    assert "could not be stopped or verified" in "".join(out.chunks())


@pytest.mark.skipif(not DARWIN, reason="needs macOS Seatbelt")
def test_a_process_that_forks_when_terminated_is_killed_under_the_real_profile(tmp_path):
    """Live: a process under the generated profile forks a child on SIGTERM and detaches it with
    setsid and a chdir; the teardown loop finds the child in a later pass by the kernel's
    evidence and kills it."""
    home = Path(os.path.realpath(Path.home()))
    root = hostrun.ensure_runs_root()
    run = root / ("d" * 32)
    for d in (run, run / "home", run / "work", run / "tmp"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = tmp_path / "p.sb"
    profile.write_text(hostrun.seatbelt_profile(run, home, root, []))
    script = (
        'use POSIX; $SIG{TERM} = sub { if (fork() == 0) { POSIX::setsid(); chdir "/"; '
        "sleep 120; exit 0 } exit 0 }; sleep 120"
    )
    proc = subprocess.Popen(
        [REAL_SANDBOX_EXEC, "-f", str(profile), "/usr/bin/perl", "-e", script],
        start_new_session=True,
    )
    ident = hostrun.new_identity(60, proc.pid)
    ident["pgid_started_epoch"] -= 10
    time.sleep(1.5)
    try:
        monkey_mode = hostrun.EVIDENCE_MODE
        hostrun.EVIDENCE_MODE = "kernel"
        out = hostrun.teardown(run, ident, home=home, root=root, deadline=20.0)
        assert out == hostrun.CLEAN
        time.sleep(0.5)
        rows = hostrun.process_table()
        assert not hostrun.prove(run, home, root, ident, rows)
    finally:
        hostrun.EVIDENCE_MODE = monkey_mode
        proc.kill()
        os.chmod(root, 0o700)
        hostrun.snapshot._rmtree(root)


def test_the_runs_root_refuses_exec_roots_the_home_and_their_parents(monkeypatch):
    for bad in ("/usr/local/franky", "/opt/homebrew", "/System/x", "/", str(Path.home() / "r")):
        monkeypatch.setenv(hostrun.RUNS_ROOT_VAR, bad)
        with pytest.raises(ConfigError):
            hostrun.ensure_runs_root()


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


def test_codex_run_copies_the_host_login_in_and_never_persists_what_the_run_wrote(tmp_path):
    store = hostrun.auth_dir()
    store.mkdir(mode=0o700, parents=True)
    original = '{"tokens": {"access_token": "old-access-1234", "account_id": "acct-1"}}'
    (store / "auth.json").write_text(original)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "home=$CODEX_HOME"; cat "$CODEX_HOME/auth.json"; echo\n'
        'echo \'{"tokens": {"access_token": "forged-9999", "account_id": "acct-1"}}\' '
        '> "$CODEX_HOME/auth.json"\n'
    )
    stub.chmod(0o755)
    cfg = Config(
        engine=CodexEngine(),
        allowed_repos=["me/repo"],
        runtime="host",
        auth_volume="franky-codex-auth",
    )
    code, out = hostrun.run_on_host(
        cfg,
        CodexEngine().inner_argv("p", model=None),
        timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        run_id="123abc123abc",
    )
    text = "".join(out.chunks())
    assert code == 0 and "old-access-1234" not in text and "***REDACTED***" in text
    # The run could have written anything (an id token it forged, a stolen login): the host
    # store is only ever written by `franky auth login codex`.
    assert (store / "auth.json").read_text() == original


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
