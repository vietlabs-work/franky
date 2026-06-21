import subprocess

import pytest

from franky.config import Config
from franky.container import build_docker_argv, ensure_image, run_in_container
from franky.engine import PiEngine

SECRET = "sk-or-very-secret-9999"
PR_URL = "https://github.com/me/repo/pull/3"


def _cfg():
    return Config(
        engine=PiEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake", "OPENROUTER_API_KEY": SECRET},
    )


def test_build_docker_argv_hardening_flags():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi", "-p", "go"])
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--rm" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--pids-limit=512" in argv
    assert "--memory=4g" in argv
    # /work and HOME are both writable tmpfs, owned by the non-root run uid so git/gh work
    # under --read-only. (Verified against the real image: bare /work:exec is root-owned and
    # a non-root agent cannot write to it; uid= fixes that.)
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    work = next(s for s in tmpfs_specs if s.startswith("/work:"))
    home = next(s for s in tmpfs_specs if s.startswith("/home/franky:"))
    assert "exec" in work and "uid=1001" in work and "gid=1001" in work
    assert "exec" in home and "uid=1001" in home and "gid=1001" in home


def test_build_docker_argv_no_mounts_or_socket():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    joined = " ".join(argv)
    assert "-v" not in argv
    assert "--mount" not in argv
    assert "docker.sock" not in joined
    assert "/var/run/docker.sock" not in joined


def test_build_docker_argv_only_passthrough_env_as_e_flags():
    passthrough = {"GH_TOKEN": "x", "OPENROUTER_API_KEY": "y"}
    argv = build_docker_argv("franky", passthrough, ["pi"])
    e_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-e"]
    assert sorted(e_values) == ["GH_TOKEN", "OPENROUTER_API_KEY"]
    # only the var NAME appears, never the value
    assert "x" not in argv
    assert "y" not in argv


def test_build_docker_argv_cross_engine_env_isolation():
    # A pi run must never carry the claude token, and a claude run must never carry a pi
    # provider key. Only the selected engine's creds (+ GH_TOKEN) are passed.
    pi_argv = build_docker_argv("franky", {"GH_TOKEN": "x", "OPENROUTER_API_KEY": "y"}, ["pi"])
    pi_e = [pi_argv[i + 1] for i, t in enumerate(pi_argv) if t == "-e"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in pi_e

    cl_argv = build_docker_argv("franky", {"GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "z"}, ["claude"])
    cl_e = [cl_argv[i + 1] for i, t in enumerate(cl_argv) if t == "-e"]
    assert "OPENROUTER_API_KEY" not in cl_e
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cl_e


def test_build_docker_argv_image_and_inner_last():
    inner = ["pi", "-p", "go", "--mode", "json"]
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, inner)
    assert argv[-len(inner):] == inner
    assert argv[-len(inner) - 1] == "franky"


def test_run_in_container_fake_runner_returns_output():
    captured = {}

    def fake_runner(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:  # ignore the reaper's `docker rm -f` call
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout=f"done {PR_URL}", stderr="")

    code, out = run_in_container(_cfg(), ["pi", "-p", "go"], runner=fake_runner, env={})
    assert code == 0
    assert PR_URL in out
    # never ran real docker; child env carries the passthrough values
    assert captured["argv"][0] == "docker"
    assert captured["env"]["OPENROUTER_API_KEY"] == SECRET


def test_run_in_container_redacts_secrets_in_output():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"leaked {SECRET}", stderr="")

    code, out = run_in_container(_cfg(), ["pi"], runner=fake_runner, env={})
    assert SECRET not in out
    assert "***REDACTED***" in out


def test_run_in_container_timeout_returns_nonzero():
    calls = []

    def fake_runner(argv, **kwargs):
        # the reaper call is `docker rm -f`; only the main run raises TimeoutExpired
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    code, out = run_in_container(_cfg(), ["pi"], timeout=5, runner=fake_runner, env={})
    assert code != 0
    assert "timed out" in out
    # reaper fired
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls)


def test_run_in_container_oserror_returns_nonzero():
    def fake_runner(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            raise OSError("docker not found")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    code, out = run_in_container(_cfg(), ["pi"], runner=fake_runner, env={})
    assert code != 0
    assert "could not launch docker" in out


def test_run_in_container_warns_on_reap_failure():
    # If `docker rm -f` fails on a launched run, the container may survive holding the
    # injected tokens; that must be surfaced (not raised, not silent).
    def fake_runner(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="rm failed")  # reaper fails

    code, out = run_in_container(_cfg(), ["pi"], runner=fake_runner, env={})
    assert code == 0
    assert "may not have been removed" in out


def test_ensure_image_true_when_present():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    assert ensure_image("franky", runner=fake_runner) is True


def test_ensure_image_false_when_absent():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")

    assert ensure_image("franky", runner=fake_runner) is False
