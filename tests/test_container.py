import subprocess

import pytest

from franky.config import Config
from franky.container import (
    build_docker_argv,
    build_network_argv,
    build_network_connect_argv,
    build_proxy_argv,
    ensure_image,
    proxy_url,
    run_in_container,
)
from franky.engine import PiEngine

SECRET = "sk-or-very-secret-9999"
PR_URL = "https://github.com/me/repo/pull/3"

NOOP_SLEEP = lambda *_a, **_k: None  # noqa: E731 - tiny test helper


def _cfg():
    return Config(
        engine=PiEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake", "OPENROUTER_API_KEY": SECRET},
    )


def _orchestration_runner(task_proc_fn, *, fail_step=None, reap_fn=None):
    """Build a fake runner that satisfies the egress orchestration (network create -> proxy
    run -> connect -> inspect[healthy] -> task run) and delegates the TASK `docker run` to
    `task_proc_fn(argv, **kwargs)`. Reaps (`docker rm`/`network rm`) succeed. Records the
    ordered sequence of docker subcommands in the returned `calls` list.

    Failure injection (so the error paths reuse this one helper instead of inlining):
    `fail_step` in {"network create", "network connect", "run -d"} makes that step return a
    non-zero rc; `reap_fn(argv, **kwargs)` overrides the `docker rm -f` response."""
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "rm", "-f"] and reap_fn is not None:
            return reap_fn(argv, **kwargs)
        if argv[:3] == ["docker", "inspect", "-f"] or (argv[:2] == ["docker", "inspect"]):
            return subprocess.CompletedProcess(argv, 0, stdout="healthy", stderr="")
        if argv[:3] == ["docker", "network", "create"]:
            rc = 1 if fail_step == "network create" else 0
            return subprocess.CompletedProcess(argv, rc, stdout="netid", stderr="")
        if argv[:3] == ["docker", "network", "connect"]:
            rc = 1 if fail_step == "network connect" else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        if argv[:3] == ["docker", "network", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["docker", "run", "-d"]:  # proxy (detached)
            rc = 1 if fail_step == "run -d" else 0
            return subprocess.CompletedProcess(argv, rc, stdout="proxyid", stderr="")
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["docker", "run"]:  # the task container
            return task_proc_fn(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return runner, calls


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

    def task(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout=f"done {PR_URL}", stderr="")

    runner, _calls = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi", "-p", "go"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code == 0
    assert PR_URL in out
    # never ran real docker; child env carries the passthrough values
    assert captured["argv"][0] == "docker"
    assert captured["env"]["OPENROUTER_API_KEY"] == SECRET


def test_run_in_container_redacts_secrets_in_output():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"leaked {SECRET}", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert SECRET not in out
    assert "***REDACTED***" in out


def test_run_in_container_timeout_returns_nonzero():
    def task(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    runner, calls = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi"], timeout=5, runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code != 0
    assert "timed out" in out
    # reaper fired (task + proxy reaped, net removed)
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls)


def test_run_in_container_oserror_returns_nonzero():
    def task(argv, **kwargs):
        raise OSError("docker not found")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code != 0
    assert "could not launch docker" in out


def test_run_in_container_warns_on_reap_failure():
    # If `docker rm -f` fails on the TASK container, it may survive holding the injected
    # tokens; that must be surfaced at full severity (not raised, not silent). Reuses the
    # shared orchestration runner with a reap override that fails the task reap only.
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    def reap(argv, **kwargs):
        rc = 1 if any("franky-run-" in a for a in argv) else 0  # task reap fails, proxy/net ok
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="rm failed")

    runner, _ = _orchestration_runner(task, reap_fn=reap)
    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code == 0
    assert "may not have been removed" in out


def test_run_in_container_connect_failure_reaps_and_no_task():
    # If attaching the proxy to the internal net fails, the task must NEVER run (fail-closed)
    # and the proxy + net must be torn down. The teardown WARNING (if any) must still surface.
    ran_task = []

    def task(argv, **kwargs):
        ran_task.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="should-not-run", stderr="")

    runner, calls = _orchestration_runner(task, fail_step="network connect")
    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code != 0
    assert "could not attach egress proxy" in out
    assert ran_task == []  # task image never launched
    # proxy and network were reaped despite the early abort
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls)
    assert any(a[:3] == ["docker", "network", "rm"] for a in calls)


def test_build_network_argv_internal():
    argv = build_network_argv("franky-net-abc")
    assert argv == ["docker", "network", "create", "--internal", "--driver", "bridge", "franky-net-abc"]


def test_build_network_connect_argv():
    assert build_network_connect_argv("net1", "proxy1") == [
        "docker", "network", "connect", "net1", "proxy1",
    ]


def test_build_proxy_argv_shape_and_hardening():
    argv = build_proxy_argv("franky-proxy", "proxy-x", ["github.com", "api.anthropic.com"])
    assert argv[:3] == ["docker", "run", "-d"]
    # proxy hardening flags present
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--rm" in argv
    assert "--memory=1g" in argv
    # squid's writable tmpfs dirs, pinned to squid's `proxy` uid/gid (13) so it can write
    # them under --read-only (a bare --tmpfs mounts root-owned; squid crashes otherwise).
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert "/run:exec,uid=13,gid=13" in tmpfs_specs
    assert "/var/log/squid:uid=13,gid=13" in tmpfs_specs
    assert "/var/spool/squid:uid=13,gid=13" in tmpfs_specs
    # allowlist passed BY VALUE (joined domains appear in the argv), image last
    assert "FRANKY_ALLOWED_DOMAINS=github.com,api.anthropic.com" in argv
    assert argv[-1] == "franky-proxy"


def test_build_docker_argv_with_network_and_proxy():
    argv = build_docker_argv(
        "franky", {"GH_TOKEN": "x"}, ["pi"],
        name="task1", network="net1", proxy_url="http://proxy1:3128",
    )
    # network joined
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "net1"
    # proxy env by VALUE (inline KEY=VALUE)
    assert "HTTP_PROXY=http://proxy1:3128" in argv
    assert "HTTPS_PROXY=http://proxy1:3128" in argv
    assert "http_proxy=http://proxy1:3128" in argv
    assert "no_proxy=localhost,127.0.0.1" in argv
    # DNS killed so the agent cannot resolve off-allowlist hosts directly
    assert "--dns" in argv
    assert argv[argv.index("--dns") + 1] == "127.0.0.1"


def test_build_docker_argv_defaults_unchanged_without_network_or_proxy():
    # With no network/proxy_url, the argv must be exactly as before (no leakage of the new
    # flags into the existing non-egress call shape).
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    assert "--network" not in argv
    assert "--dns" not in argv
    assert not any(a.startswith("HTTP_PROXY=") for a in argv)
    assert not any(a.startswith("http_proxy=") for a in argv)


def test_run_in_container_orchestration_order():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"done {PR_URL}", stderr="")

    runner, calls = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code == 0

    def first_index(pred):
        return next(i for i, a in enumerate(calls) if pred(a))

    i_net_create = first_index(lambda a: a[:3] == ["docker", "network", "create"])
    i_proxy_run = first_index(lambda a: a[:3] == ["docker", "run", "-d"])
    i_connect = first_index(lambda a: a[:3] == ["docker", "network", "connect"])
    i_inspect = first_index(lambda a: a[:2] == ["docker", "inspect"])
    i_task_run = first_index(lambda a: a[:2] == ["docker", "run"] and a[2] != "-d")
    i_rm_task = first_index(lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-run-" in x for x in a))
    i_rm_proxy = first_index(lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-proxy-" in x for x in a))
    i_net_rm = first_index(lambda a: a[:3] == ["docker", "network", "rm"])

    # network-create -> proxy run -d -> connect -> inspect -> task run -> rm task -> rm proxy -> net rm
    assert i_net_create < i_proxy_run < i_connect < i_inspect < i_task_run
    assert i_task_run < i_rm_task < i_rm_proxy < i_net_rm


def test_run_in_container_fail_closed_when_proxy_never_ready():
    # Proxy never becomes healthy: the task `docker run` must NEVER fire, and proxy+net are
    # torn down.
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="starting", stderr="")
        if argv[:3] == ["docker", "network", "create"]:
            return subprocess.CompletedProcess(argv, 0, stdout="netid", stderr="")
        if argv[:3] == ["docker", "network", "connect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["docker", "network", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["docker", "run", "-d"]:
            return subprocess.CompletedProcess(argv, 0, stdout="proxyid", stderr="")
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code != 0
    assert "did not become ready" in out
    # the task image was NEVER run (no `docker run` other than the proxy's `-d`)
    task_runs = [a for a in calls if a[:2] == ["docker", "run"] and (len(a) < 3 or a[2] != "-d")]
    assert task_runs == []
    # proxy + net torn down
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls)
    assert any(a[:3] == ["docker", "network", "rm"] for a in calls)


def test_run_in_container_timeout_reaps_task_proxy_net():
    def task(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    runner, calls = _orchestration_runner(task)
    code, out = run_in_container(_cfg(), ["pi"], timeout=5, runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code != 0
    # finally guarantees all three are reaped
    assert any(a[:3] == ["docker", "rm", "-f"] and any("franky-run-" in x for x in a) for a in calls)
    assert any(a[:3] == ["docker", "rm", "-f"] and any("franky-proxy-" in x for x in a) for a in calls)
    assert any(a[:3] == ["docker", "network", "rm"] for a in calls)


def test_run_in_container_secrets_absent_from_task_argv():
    # The injected secret VALUE must never appear on the task container's argv (name-only -e).
    captured = {}

    def task(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert SECRET not in captured["argv"]
    assert "OPENROUTER_API_KEY" in captured["argv"]  # name only


def test_ensure_image_true_when_present():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    assert ensure_image("franky", runner=fake_runner) is True


def test_ensure_image_false_when_absent():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")

    assert ensure_image("franky", runner=fake_runner) is False
