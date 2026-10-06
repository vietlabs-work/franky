import subprocess
from unittest import mock

import franky.container as container_mod
import pytest
from franky import franky_version, snapshot
from franky.config import Config, load_config
from franky.container import (
    CODEX_AUTH_HOME,
    FRANKY_PROXY_IMAGE_VAR,
    GHCR_REPO_VAR,
    PULL_TIMEOUT_SECS,
    STEER_FILE,
    build_docker_argv,
    build_codex_auth_scrub_argv,
    build_network_argv,
    build_network_connect_argv,
    build_proxy_argv,
    build_steer_argv,
    capture_diagnostics,
    deliver_steer,
    ensure_image_available,
    image_exists,
    resolve_image,
    run_in_container as _run_in_container,
    codex_auth_ready,
    codex_auth_status,
    codex_auth_login,
    codex_auth_logout,
)
from franky.engine import CODEX_AUTH_VOLUME, CodexEngine, OpenCodeEngine, PiEngine
from franky.profile import CONTAINER_HOME, PROFILE_WAIT_VAR

SECRET = "sk-or-very-secret-9999"
PR_URL = "https://github.com/me/repo/pull/3"

NOOP_SLEEP = lambda *_a, **_k: None  # noqa: E731 - tiny test helper


def run_in_container(*args, **kwargs):
    """Materialize small fixture transcripts for historical output assertions."""
    from franky.transcript import Transcript

    kwargs.setdefault("apparmor_selector", lambda _runner: None)
    code, output = _run_in_container(*args, **kwargs)
    if isinstance(output, Transcript):
        with output:
            output = "".join(output.chunks())
    return code, output


def _cfg():
    return Config(
        engine=PiEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake", "OPENROUTER_API_KEY": SECRET},
    )


def _orchestration_runner(task_proc_fn, *, fail_step=None, reap_fn=None):
    """Build a fake runner that satisfies the egress orchestration (network create -> proxy
    run -> connect -> denied-CONNECT probe -> task run) and delegates the TASK `docker run` to
    `task_proc_fn(argv, **kwargs)`. Reaps (`docker rm`/`network rm`) succeed. Records the
    ordered sequence of docker subcommands in the returned `calls` list.

    Failure injection (so the error paths reuse this one helper instead of inlining):
    `fail_step` in {"network create", "network connect", "run -d"} makes that step return a
    non-zero rc; `reap_fn(argv, **kwargs)` overrides the `docker rm -f` response."""
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if argv[:3] == ["docker", "rm", "-f"] and reap_fn is not None:
            return reap_fn(argv, **kwargs)
        if argv[:2] == ["docker", "exec"] and "curl" in argv:
            return subprocess.CompletedProcess(argv, 56, stdout="403", stderr="CONNECT refused")
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
    # Anonymous volumes keep build files off RAM and never share a bot's workspace.
    mounts = [argv[i + 1] for i, t in enumerate(argv) if t == "--mount"]
    assert mounts == [
        "type=volume,dst=/work",
        "type=volume,dst=/home/franky",
        "type=volume,dst=/tmp",
    ]


def test_build_docker_argv_dind_relaxations():
    """Always-on rootless DinD (issue #12) needs a MINIMAL, deliberate relaxation of the locked
    profile. These assertions pin exactly what changed so a regression (re-adding
    no-new-privileges, dropping a cap) is caught."""
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi", "-p", "go"])
    # no-new-privileges is DELIBERATELY GONE: it blocks rootlesskit's setuid uid-map helpers.
    assert "--security-opt=no-new-privileges" not in argv
    # cap-drop=ALL stays, but SETUID/SETGID are added back for newuidmap/newgidmap.
    assert "--cap-add=SETUID" in argv
    assert "--cap-add=SETGID" in argv
    # /proc unmasked so the nested runc can mount procfs for inner containers.
    assert "--security-opt=systempaths=unconfined" in argv
    # slirp4netns tap device for the nested daemon's network.
    assert "--device" in argv
    assert "/dev/net/tun" in argv
    # Two jobs fit within a 4.25 GiB container budget, including their proxies.
    assert "--pids-limit=2048" in argv
    assert "--memory=2048m" in argv
    assert "--memory-swap=2048m" in argv
    assert "--log-driver=none" in argv
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert any(s.startswith("/run/user/1001:") and "uid=1001" in s for s in tmpfs_specs)
    assert all("size=" in s for s in tmpfs_specs)
    assert "FRANKY_DISK_MB=8192" in argv


def test_apparmor_selector_failure_aborts_before_resource_creation():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fail(_runner):
        raise container_mod.SecurityPolicyError("could not inspect Docker security options")

    code, out = run_in_container(_cfg(), ["pi"], runner=runner, env={}, apparmor_selector=fail)

    assert code == 1
    assert out == "franky: could not inspect Docker security options - refusing to run"
    assert calls == []


def test_build_docker_argv_no_mounts_or_socket():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    joined = " ".join(argv)
    assert "-v" not in argv
    assert "type=bind" not in joined
    assert "docker.sock" not in joined
    assert "/var/run/docker.sock" not in joined


def test_codex_subscription_mounts_only_fixed_named_volume():
    argv = build_docker_argv(
        "franky", {"GH_TOKEN": "x"}, ["codex", "exec"], auth_volume=CODEX_AUTH_VOLUME
    )
    named_mounts = [
        argv[i + 1] for i, t in enumerate(argv) if t == "--mount" and "src=" in argv[i + 1]
    ]
    assert named_mounts == [f"type=volume,src={CODEX_AUTH_VOLUME},dst={CODEX_AUTH_HOME}"]
    assert "type=bind" not in " ".join(argv)
    assert "docker.sock" not in " ".join(argv)


def test_codex_auth_scrub_keeps_only_auth_json_and_has_no_network():
    argv = build_codex_auth_scrub_argv("franky", require_auth=True, auth_volume=CODEX_AUTH_VOLUME)
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "auth.json" in joined
    assert "config.toml" not in joined
    assert "! -L" in joined
    assert "65536" in joined
    assert CODEX_AUTH_VOLUME in joined
    assert "type=bind" not in joined


def test_codex_auth_ready_checks_volume_before_scrubbing():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such volume")
        raise AssertionError("missing volume must fail before docker run")

    assert not codex_auth_ready("franky", runner=runner, auth_volume=CODEX_AUTH_VOLUME)
    assert len(calls) == 1


def test_codex_auth_ready_rejects_malformed_json():
    def runner(argv, **kwargs):
        if argv[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "cat":
            return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert not codex_auth_ready("franky", runner=runner, auth_volume=CODEX_AUTH_VOLUME)


def test_codex_auth_ready_rejects_oversized_json():
    def runner(argv, **kwargs):
        if argv[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "cat":
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"token":"' + "x" * 65536, stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert not codex_auth_ready("franky", runner=runner, auth_volume=CODEX_AUTH_VOLUME)


def test_codex_auth_status_uses_read_only_volume_and_no_network():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "cat":
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"access_token":"subscription-token"}', stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert codex_auth_status("franky", runner=runner, auth_volume=CODEX_AUTH_VOLUME)
    status_argv = calls[-1]
    assert status_argv[-2:] == ["login", "status"]
    assert "--network" in status_argv and "none" in status_argv
    assert ",readonly" in status_argv[status_argv.index("--mount") + 1]


def test_codex_auth_login_is_device_flow_and_logout_removes_fixed_volume():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "cat":
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"access_token":"subscription-token"}', stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert codex_auth_login("franky", runner=runner, auth_volume=CODEX_AUTH_VOLUME)
    login_argv = next(argv for argv in calls if "--device-auth" in argv)
    assert 'cli_auth_credentials_store="file"' in login_argv
    assert "--mount" in login_argv and CODEX_AUTH_VOLUME in " ".join(login_argv)
    assert "--network" not in login_argv  # trusted operator flow needs OpenAI egress
    calls.clear()
    assert codex_auth_logout(runner=runner, auth_volume=CODEX_AUTH_VOLUME)
    assert calls == [["docker", "volume", "rm", CODEX_AUTH_VOLUME]]


def test_codex_auth_login_status_logout_honor_custom_auth_volume():
    # FRANKY_CODEX_AUTH_VOLUME (resolved by the caller) must reach every docker call, not just
    # the default - two Franky instances must never touch each other's volume.
    custom = "franky-team-codex-auth"
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "cat":
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"access_token":"subscription-token"}', stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert codex_auth_login("franky", runner=runner, auth_volume=custom)
    assert calls[0] == ["docker", "volume", "create", custom]
    login_argv = next(argv for argv in calls if "--device-auth" in argv)
    assert custom in " ".join(login_argv)
    assert CODEX_AUTH_VOLUME not in " ".join(login_argv)

    calls.clear()
    assert codex_auth_status("franky", runner=runner, auth_volume=custom)
    assert custom in " ".join(calls[-1])

    calls.clear()
    assert codex_auth_logout(runner=runner, auth_volume=custom)
    assert calls == [["docker", "volume", "rm", custom]]


def test_run_in_container_codex_auth_gate_uses_configured_auth_volume():
    # The pre-run scrub/read gate must key off cfg.auth_volume (resolved from
    # FRANKY_CODEX_AUTH_VOLUME), not the hardcoded CODEX_AUTH_VOLUME default.
    custom = "franky-team-codex-auth"
    cfg = Config(
        engine=CodexEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake"},
        auth_volume=custom,
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such volume")
        raise AssertionError(argv)

    code, out = run_in_container(cfg, ["codex", "exec"], runner=runner, env={})
    assert code != 0
    assert calls == [["docker", "volume", "inspect", custom]]


def test_run_in_container_fails_closed_when_codex_auth_scrub_fails():
    calls = []
    cfg = Config(
        engine=CodexEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake"},
        auth_volume=CODEX_AUTH_VOLUME,
    )

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if argv[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        raise AssertionError(argv)

    code, out = run_in_container(cfg, ["codex", "exec"], runner=runner, env={})
    assert code != 0
    assert "subscription login" in out
    assert not any(argv[:3] == ["docker", "network", "create"] for argv in calls)


def test_run_in_container_redacts_codex_subscription_tokens(monkeypatch):
    token = "subscription-token-that-must-never-reach-logs"
    cfg = Config(
        engine=CodexEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake"},
        auth_volume=CODEX_AUTH_VOLUME,
    )
    monkeypatch.setattr(container_mod, "_codex_auth_state", lambda *a, **k: [token])

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"leaked {token}", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(cfg, ["codex", "exec"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code == 0
    assert token not in out
    assert "***REDACTED***" in out


def test_run_in_container_stream_redacts_codex_subscription_tokens(monkeypatch):
    token = "subscription-token-that-must-never-reach-progress"
    cfg = Config(
        engine=CodexEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={"GH_TOKEN": "ghp_fake"},
        auth_volume=CODEX_AUTH_VOLUME,
    )
    monkeypatch.setattr(container_mod, "_codex_auth_state", lambda *a, **k: [token])
    runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    )
    seen = []
    code, out = run_in_container(
        cfg,
        ["codex", "exec"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=seen.append,
        popen=_fake_popen_factory([f"leaked {token}\n"]),
    )
    assert code == 0
    assert all(token not in line for line in seen)
    assert token not in out


def test_build_docker_argv_only_passthrough_env_as_e_flags():
    passthrough = {"GH_TOKEN": "x", "OPENROUTER_API_KEY": "y"}
    argv = build_docker_argv("franky", passthrough, ["pi"])
    e_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-e"]
    assert sorted(e_values) == ["FRANKY_DISK_MB=8192", "GH_TOKEN", "OPENROUTER_API_KEY"]
    # only the var NAME appears, never the value
    assert "x" not in argv
    assert "y" not in argv


def test_build_docker_argv_cross_engine_env_isolation():
    # A pi run must never carry the claude token, and a claude run must never carry a pi
    # provider key. Only the selected engine's creds (+ GH_TOKEN) are passed.
    pi_argv = build_docker_argv("franky", {"GH_TOKEN": "x", "OPENROUTER_API_KEY": "y"}, ["pi"])
    pi_e = [pi_argv[i + 1] for i, t in enumerate(pi_argv) if t == "-e"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in pi_e

    cl_argv = build_docker_argv(
        "franky", {"GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "z"}, ["claude"]
    )
    cl_e = [cl_argv[i + 1] for i, t in enumerate(cl_argv) if t == "-e"]
    assert "OPENROUTER_API_KEY" not in cl_e
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cl_e

    # A codex run carries only its own key (+ GH_TOKEN), never the other engines' creds.
    cx_argv = build_docker_argv(
        "franky", {"GH_TOKEN": "x", "CODEX_API_KEY": "c"}, ["codex", "exec"]
    )
    cx_e = [cx_argv[i + 1] for i, t in enumerate(cx_argv) if t == "-e"]
    assert "CODEX_API_KEY" in cx_e
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cx_e
    assert "OPENROUTER_API_KEY" not in cx_e


def test_opencode_config_emits_only_name_only_selected_credentials():
    secret_values = {
        "GH_TOKEN": "gh-secret",
        "OPENROUTER_API_KEY": "or-secret",
        "OPENAI_API_KEY": "oa-secret",
        "ANTHROPIC_API_KEY": "an-secret",
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "FRANKY_MODEL": "openrouter/anthropic/claude-x",
    }
    cfg = load_config("opencode", secret_values)
    argv = build_docker_argv("franky", cfg.passthrough_env, ["opencode", "run"])
    e_values = [argv[i + 1] for i, token in enumerate(argv) if token == "-e"]
    assert sorted(e_values) == ["FRANKY_DISK_MB=8192", "GH_TOKEN", "OPENROUTER_API_KEY"]
    assert all(value not in argv for value in secret_values.values())


def test_opencode_profile_credential_does_not_widen_provider_egress():
    cfg = Config(
        engine=OpenCodeEngine(),
        allowed_repos=["me/repo"],
        passthrough_env={
            "GH_TOKEN": "ghp_fake",
            "MOONSHOT_API_KEY": "moon",
            "OPENROUTER_API_KEY": "profile-added",
        },
        model="moonshotai/kimi-k3",
    )
    runner, calls = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    )

    code, _ = run_in_container(cfg, ["opencode", "run"], runner=runner, env={}, sleeper=NOOP_SLEEP)

    assert code == 0
    proxy_argv = next(argv for argv in calls if argv[:3] == ["docker", "run", "-d"])
    allowed_arg = next(arg for arg in proxy_argv if arg.startswith("FRANKY_ALLOWED_DOMAINS="))
    assert "api.moonshot.ai" in allowed_arg
    assert "openrouter.ai" not in allowed_arg


def test_build_docker_argv_image_and_inner_last():
    inner = ["pi", "-p", "go", "--mode", "json"]
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, inner)
    assert argv[-len(inner) :] == inner
    assert argv[-len(inner) - 1] == "franky"


def test_run_in_container_fake_runner_returns_output():
    captured = {}

    def task(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout=f"done {PR_URL}", stderr="")

    runner, _calls = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(), ["pi", "-p", "go"], runner=runner, env={}, sleeper=NOOP_SLEEP
    )
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
    code, out = run_in_container(
        _cfg(), ["pi"], timeout=5, runner=runner, env={}, sleeper=NOOP_SLEEP
    )
    # 124 is the Franky-set timeout sentinel (GNU `timeout(1)` convention), still nonzero.
    assert code == 124
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
    assert argv == [
        "docker",
        "network",
        "create",
        "--internal",
        "--driver",
        "bridge",
        "franky-net-abc",
    ]


def test_build_network_connect_argv():
    assert build_network_connect_argv("net1", "proxy1") == [
        "docker",
        "network",
        "connect",
        "net1",
        "proxy1",
    ]


def test_build_proxy_argv_shape_and_hardening():
    argv = build_proxy_argv("franky-proxy", "proxy-x", ["github.com", "api.anthropic.com"])
    assert argv[:3] == ["docker", "run", "-d"]
    # proxy hardening flags present
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--rm" in argv
    assert "--memory=128m" in argv
    assert "--memory-swap=128m" in argv
    assert "--ulimit=nofile=4096:4096" in argv
    # squid's writable tmpfs dirs, pinned to squid's `proxy` uid/gid (13) so it can write
    # them under --read-only (a bare --tmpfs mounts root-owned; squid crashes otherwise).
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert "/run:exec,uid=13,gid=13,size=16m" in tmpfs_specs
    assert "/var/log/squid:uid=13,gid=13,size=1m" in tmpfs_specs
    assert "/var/spool/squid:uid=13,gid=13,size=1m" in tmpfs_specs
    # allowlist passed BY VALUE (joined domains appear in the argv), image last
    assert "FRANKY_ALLOWED_DOMAINS=github.com,api.anthropic.com" in argv
    assert argv[-1] == "franky-proxy"


def test_build_docker_argv_with_network_and_proxy():
    argv = build_docker_argv(
        "franky",
        {"GH_TOKEN": "x"},
        ["pi"],
        name="task1",
        network="net1",
        proxy_url="http://proxy1:3128",
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
    i_probe = first_index(lambda a: a[:2] == ["docker", "exec"] and "curl" in a)
    i_task_run = first_index(lambda a: a[:2] == ["docker", "run"] and a[2] != "-d")
    i_rm_task = first_index(
        lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-run-" in x for x in a)
    )
    i_rm_proxy = first_index(
        lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-proxy-" in x for x in a)
    )
    i_net_rm = first_index(lambda a: a[:3] == ["docker", "network", "rm"])

    # network-create -> proxy run -d -> connect -> denial probe -> task run -> teardown
    assert i_net_create < i_proxy_run < i_connect < i_probe < i_task_run
    assert i_task_run < i_rm_task < i_rm_proxy < i_net_rm


def test_run_in_container_fail_closed_when_proxy_never_ready():
    # Proxy never proves default-deny: the task `docker run` must NEVER fire, and proxy+net are
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
    code, out = run_in_container(
        _cfg(), ["pi"], timeout=5, runner=runner, env={}, sleeper=NOOP_SLEEP
    )
    assert code == 124  # Franky-set timeout sentinel
    # finally guarantees all three are reaped
    assert any(
        a[:3] == ["docker", "rm", "-f"] and any("franky-run-" in x for x in a) for a in calls
    )
    assert any(
        a[:3] == ["docker", "rm", "-f"] and any("franky-proxy-" in x for x in a) for a in calls
    )
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


def test_image_exists_true_when_present():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    assert image_exists("franky", runner=fake_runner) is True


def test_image_exists_false_when_absent():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")

    assert image_exists("franky", runner=fake_runner) is False


def test_resolve_image_env_override_wins():
    assert resolve_image({"FRANKY_IMAGE": "my-local"}) == "my-local"


def test_resolve_image_default_is_versioned_ghcr():
    ref = resolve_image({})
    assert ref.startswith("ghcr.io/vietlabs-work/franky:")
    assert not ref.endswith(":latest")


@pytest.mark.parametrize("engine", ["pi", "claude", "codex", "opencode"])
def test_engine_image_tag(engine):
    assert resolve_image({}, engine=engine).endswith(f":{franky_version()}-{engine}")
    assert resolve_image({"FRANKY_IMAGE": "local:test"}, engine=engine) == "local:test"


def test_resolve_image_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unknown image engine"):
        resolve_image({}, engine="bogus")


def test_resolve_image_proxy_uses_proxy_var():
    ref = resolve_image({}, FRANKY_PROXY_IMAGE_VAR, "franky-proxy")
    assert ref.startswith("ghcr.io/vietlabs-work/franky-proxy:")


def test_resolve_image_ghcr_repo_override():
    # FRANKY_GHCR_REPO retargets the namespace (publishing org can move with no code change),
    # while the per-image FRANKY_IMAGE override still wins outright.
    ref = resolve_image({GHCR_REPO_VAR: "ghcr.io/acme"})
    assert ref.startswith("ghcr.io/acme/franky:")
    assert resolve_image({GHCR_REPO_VAR: "ghcr.io/acme"}, engine="codex").startswith(
        "ghcr.io/acme/franky:"
    )
    assert resolve_image({GHCR_REPO_VAR: "ghcr.io/acme", "FRANKY_IMAGE": "local"}) == "local"


def test_ensure_image_available_present_no_pull():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, reason = ensure_image_available("franky", runner=fake_runner)
    assert ok is True
    assert reason == ""
    assert not any(a[:2] == ["docker", "pull"] for a in calls)


def test_ensure_image_available_absent_pull_ok():
    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")
        if argv[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Pulled", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, reason = ensure_image_available("franky:0.1.0", runner=fake_runner)
    assert ok is True
    assert reason == ""


def test_ensure_image_available_absent_auth_error():
    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")
        if argv[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unauthorized: access denied"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, reason = ensure_image_available("ghcr.io/vietlabs-work/franky:0.1.0", runner=fake_runner)
    assert ok is False
    assert reason == "auth"


def test_ensure_image_available_absent_pull_failed():
    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")
        if argv[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="network timeout")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, reason = ensure_image_available("ghcr.io/vietlabs-work/franky:0.1.0", runner=fake_runner)
    assert ok is False
    assert reason == "pull-failed"


def test_ensure_image_available_pull_timeout():
    seen = {}

    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    ok, reason = ensure_image_available("franky:0.1.0", runner=fake_runner)
    assert ok is False
    assert reason == "pull-timeout"
    assert seen["timeout"] == PULL_TIMEOUT_SECS


def test_ensure_image_available_oserror():
    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such image")
        raise OSError("docker not found")

    ok, reason = ensure_image_available("franky:0.1.0", runner=fake_runner)
    assert ok is False
    assert reason == "no-docker"


def test_run_in_container_threads_proxy_image():
    proxy_run_calls = []

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    original_runner, calls = _orchestration_runner(task)

    def capturing_runner(argv, **kwargs):
        if argv[:3] == ["docker", "run", "-d"]:
            proxy_run_calls.append(argv)
        return original_runner(argv, **kwargs)

    run_in_container(
        _cfg(),
        ["pi"],
        runner=capturing_runner,
        env={},
        sleeper=NOOP_SLEEP,
        proxy_image="my-proxy:1.0",
    )
    assert len(proxy_run_calls) == 1
    assert "my-proxy:1.0" in proxy_run_calls[0]


# ---------------------------------------------------------------------------
# profile bundle injection
# ---------------------------------------------------------------------------


def test_build_docker_argv_profile_wait_flag_set():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"], profile_wait=True)
    assert f"{PROFILE_WAIT_VAR}=1" in argv


def test_build_docker_argv_no_profile_wait_by_default():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    assert PROFILE_WAIT_VAR not in " ".join(argv)


def test_build_docker_argv_never_carries_bundle_content():
    """The bundle must NEVER ride the argv - only the wait FLAG does.

    Load-bearing: a swept operator setup runs to ~1 MB, past Linux's 128 KB per-argument
    ceiling, and an argv is visible in `ps`. build_docker_argv has no bundle parameter at all
    now, so there is nothing to regress; this asserts the flag is the whole footprint."""
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"], profile_wait=True)
    assert [a for a in argv if PROFILE_WAIT_VAR in a] == [f"{PROFILE_WAIT_VAR}=1"]
    assert max(len(a) for a in argv) < 512


def test_run_in_container_streams_bundle_in_after_launch():
    """A bundle forces the streaming path, then untars over exec stdin and touches the marker.

    Same channel as resume: no `docker cp` INTO the read-only container, extraction as the
    image's own uid into HOME, marker LAST."""
    calls = []
    base_runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    )
    stdin_seen = {}

    def full_runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "inspect", "-f"] and "{{.State.Running}}" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if argv[:2] == ["docker", "exec"] and "curl" in argv:
            return base_runner(argv, **kwargs)
        if argv[:2] == ["docker", "exec"]:
            if "tar" in argv:
                stdin_seen["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return base_runner(argv, **kwargs)

    code, _out = run_in_container(
        _cfg(),
        ["pi"],
        runner=full_runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=_fake_popen_factory(["event\n"]),
        run_id="beef00beef00",
        profile_bundle=b"fake-gzip-tar",
    )
    untar = [c for c in calls if "tar" in c and "-xzf" in c]
    assert untar, "bundle was never streamed in"
    assert CONTAINER_HOME in untar[0], "bundle must extract into HOME, not /work"
    assert stdin_seen["input"] == b"fake-gzip-tar"
    assert not any(c[:2] == ["docker", "cp"] and "franky-run-beef00beef00:" in c[3] for c in calls)
    assert any("touch" in c for c in calls)


def test_run_in_container_bundle_failure_does_not_change_result():
    """A failed untar must not alter (code, output) - the entrypoint refuses on its own."""
    base_runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    )
    touched = []

    def runner(argv, **kwargs):
        if argv[:3] == ["docker", "inspect", "-f"] and "{{.State.Running}}" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if "tar" in argv and "-xzf" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="untar boom")
        if "touch" in argv:
            touched.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return base_runner(argv, **kwargs)

    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=_fake_popen_factory(["streamed line\n"]),
        profile_bundle=b"fake-gzip-tar",
    )
    assert code == 0
    assert "streamed line" in out
    # The marker is touched only after a CLEAN untar, so the entrypoint exits nonzero instead
    # of running the engine on a half-unpacked profile.
    assert not touched


def test_run_in_container_no_profile_wait_by_default():
    """Without a bundle, the wait flag must not appear in the task argv."""
    captured = {}

    def task(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert PROFILE_WAIT_VAR not in " ".join(captured["argv"])


# ---------------------------------------------------------------------------
# Streaming path (progress + popen)
# ---------------------------------------------------------------------------


class _FakePopen:
    """Fake subprocess.Popen for streaming tests: stdout yields the provided lines."""

    def __init__(self, lines, returncode=0):
        self.returncode = returncode
        self._lines = list(lines)
        self.stdout = self

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _fake_popen_factory(lines, returncode=0):
    """Return a popen callable that produces a _FakePopen for the task container."""

    def fake_popen(argv, **kwargs):
        return _FakePopen(lines=lines, returncode=returncode)

    return fake_popen


MISSING_APPARMOR = 'docker: Error response from daemon: apparmor profile "franky-task" not found.\n'
APPARMOR_INSTALL = "franky apparmor-profile > franky-task.apparmor"


def test_missing_apparmor_profile_has_install_command_in_blocking_output():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 125, stdout="", stderr=MISSING_APPARMOR)

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        apparmor_selector=lambda _runner: "franky-task",
    )

    assert code == 125
    assert APPARMOR_INSTALL in out


def test_missing_apparmor_profile_has_install_command_in_streaming_output():
    runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    )
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _line: None,
        popen=_fake_popen_factory([MISSING_APPARMOR], returncode=125),
        apparmor_selector=lambda _runner: "franky-task",
    )

    assert code == 125
    assert APPARMOR_INSTALL in out


def test_run_in_container_streaming_calls_progress_for_each_line():
    """When progress is given, it is called once per line of output."""
    lines = ["line one\n", "line two\n"]
    seen = []

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=seen.append,
        popen=_fake_popen_factory(lines),
    )
    assert seen == lines


def test_run_in_container_streaming_accumulates_output():
    """The returned output is the full accumulated transcript (for log + economics)."""
    lines = ["event one\n", "event two\n"]

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _: None,
        popen=_fake_popen_factory(lines),
    )
    assert code == 0
    assert out == "event one\nevent two\n"


def test_quiet_run_spools_secret_across_byte_chunks_before_progress(tmp_path):
    import io
    from franky.transcript import Transcript

    secret = "secret\nacross-chunks"
    cfg = _cfg()
    cfg.passthrough_env["OPENROUTER_API_KEY"] = secret
    runner, _ = _orchestration_runner(lambda argv, **kwargs: None)
    proc = _FakePopen([])
    proc.stdout = io.BytesIO(("before " + secret + " after\n").encode())
    code, output = _run_in_container(
        cfg, ["pi"], runner=runner, popen=lambda *a, **k: proc, sleeper=NOOP_SLEEP, env={}
    )
    assert code == 0
    assert isinstance(output, Transcript)
    with output:
        output.persist(tmp_path / "transcript")
        assert "".join(output.chunks()) == "before ***REDACTED*** after\n"


def test_stream_timeout_keeps_redacted_partial_output():
    runner, _ = _orchestration_runner(lambda argv, **kwargs: None)
    code, output = _run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        popen=_fake_popen_factory([f"partial {SECRET}\n"]),
        timeout=0,
        sleeper=NOOP_SLEEP,
        env={},
    )
    with output:
        text = "".join(output.chunks())
    assert code == 124
    assert "partial ***REDACTED***" in text
    assert "container timed out" in text
    assert SECRET not in text


def test_run_in_container_streaming_redacts_before_progress():
    """The progress callback must receive per-line redacted output (never the raw secret)."""
    lines = [f"token {SECRET}\n"]
    seen = []

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=seen.append,
        popen=_fake_popen_factory(lines),
    )
    assert len(seen) == 1
    assert SECRET not in seen[0]
    assert "***REDACTED***" in seen[0]


def test_run_in_container_streaming_full_output_redacted():
    """The returned output (for log / PR-URL parsing) is also fully redacted."""
    lines = [f"leaked {SECRET}\n"]

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _: None,
        popen=_fake_popen_factory(lines),
    )
    assert SECRET not in out
    assert "***REDACTED***" in out


def test_run_in_container_streaming_oserror_returns_nonzero():
    """OSError from popen (docker not found) is handled the same as the blocking path."""

    def failing_popen(argv, **kwargs):
        raise OSError("docker not found")

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _: None,
        popen=failing_popen,
    )
    assert code != 0
    assert "could not launch docker" in out


def test_run_in_container_streaming_nonzero_returncode():
    """If the container exits non-zero, run_in_container surfaces that return code."""

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _: None,
        popen=_fake_popen_factory(["done\n"], returncode=1),
    )
    assert code == 1


def test_run_in_container_blocking_path_unchanged_when_no_progress():
    """Without a progress callback the original blocking subprocess.run path is used."""
    captured = {}

    def task(argv, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    # capture_output=True is the blocking-path signature (popen path uses stdout=PIPE)
    assert captured["kwargs"].get("capture_output") is True


# ---------------------------------------------------------------------------
# capture_diagnostics (issue #69): best-effort runtime signals captured host-side just
# before the task/proxy containers are reaped. Never raises, never touches docker mechanics
# beyond `docker inspect`/`docker exec`.
# ---------------------------------------------------------------------------

_SQUID_DENIED_LOG = (
    "1700000000.000    0 172.0.0.2 TCP_DENIED/403 3822 CONNECT evil.example.com:443 - "
    "HIER_NONE/- text/html\n"
    "1700000001.000    0 172.0.0.2 TCP_DENIED/403 3822 CONNECT evil.example.com:443 - "
    "HIER_NONE/- text/html\n"
    "1700000002.000    0 172.0.0.2 TCP_DENIED/403 3822 CONNECT other.example.com:443 - "
    "HIER_NONE/- text/html\n"
    "1700000003.000    0 172.0.0.2 TCP_MISS/200 1234 CONNECT allowed.example.com:443 - "
    "HIER_DIRECT/1.2.3.4 -\n"
)


def test_capture_diagnostics_parses_task_inspect():
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="137|true|exited\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=True, proxy_launched=False, secrets=[]
    )
    assert diag["task_exit_code"] == 137
    assert diag["oom_killed"] is True
    assert diag["task_state"] == "exited"


def test_capture_diagnostics_skips_task_fields_when_not_launched():
    def runner(argv, **kwargs):
        raise AssertionError("must not call docker when task_launched=False")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=False, proxy_launched=False, secrets=[]
    )
    assert "task_exit_code" not in diag


def test_capture_diagnostics_parses_squid_denied_hosts():
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_SQUID_DENIED_LOG, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=False, proxy_launched=True, secrets=[]
    )
    assert diag["proxy_denied_count"] == 3
    assert diag["egress_denied"] == [
        {"host": "evil.example.com", "count": 2},
        {"host": "other.example.com", "count": 1},
    ]


def test_capture_diagnostics_bounds_proxy_read_before_host_capture():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=_SQUID_DENIED_LOG, stderr="")

    diag = capture_diagnostics(
        "t", "p", "", runner, task_launched=False, proxy_launched=True, secrets=[]
    )
    assert calls[0][0] == ["docker", "exec", "p", "tail", "-c", "65537", "/run/squid-access.log"]
    assert calls[0][1]["errors"] == "replace"
    assert diag["proxy_log_truncated"] is False
    assert diag["proxy_denied_count"] == 3


def test_capture_diagnostics_discards_truncated_first_line():
    fragment = "TCP_DENIED/403 CONNECT partial.example.com:443"
    suffix = "\n" + _SQUID_DENIED_LOG
    output = fragment + " " * (65537 - len(fragment) - len(suffix)) + suffix

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    diag = capture_diagnostics(
        "t", "p", "", runner, task_launched=False, proxy_launched=True, secrets=[]
    )
    assert diag["proxy_log_truncated"] is True
    assert diag["proxy_denied_count"] == 3
    assert {item["host"] for item in diag["egress_denied"]} == {
        "evil.example.com",
        "other.example.com",
    }


def test_capture_diagnostics_ignores_and_marks_incomplete_last_record():
    output = _SQUID_DENIED_LOG + "TCP_DENIED/403 CONNECT partial.example.com:443"

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    diag = capture_diagnostics(
        "t", "p", "", runner, task_launched=False, proxy_launched=True, secrets=[]
    )
    assert diag["proxy_denied_count"] == 3
    assert diag["proxy_log_truncated"] is True


def test_capture_diagnostics_omits_failed_proxy_read_but_keeps_transcript_signals():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=_SQUID_DENIED_LOG, stderr="gone")

    diag = capture_diagnostics(
        "t",
        "p",
        "rootless dockerd ready",
        runner,
        task_launched=False,
        proxy_launched=True,
        secrets=[],
    )
    assert "proxy_denied_count" not in diag
    assert "egress_denied" not in diag
    assert "proxy_log_truncated" not in diag
    assert diag["dind_ready"] is True


def test_capture_diagnostics_dind_ready_markers():
    failure_diag = capture_diagnostics(
        "t",
        "p",
        "franky: WARNING rootless dockerd did not become ready in 30s",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert failure_diag["dind_ready"] is False

    ready_diag = capture_diagnostics(
        "t",
        "p",
        "franky: rootless dockerd ready",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert ready_diag["dind_ready"] is True

    neither_diag = capture_diagnostics(
        "t",
        "p",
        "no marker here at all",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert neither_diag["dind_ready"] is None


def test_capture_diagnostics_tmpfs_full_heuristic():
    no_space = capture_diagnostics(
        "t",
        "p",
        "write failed: No space left on device",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert no_space["tmpfs_full"] is True

    enospc = capture_diagnostics(
        "t",
        "p",
        "OSError: [Errno 28] ENOSPC",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert enospc["tmpfs_full"] is True

    clean = capture_diagnostics(
        "t",
        "p",
        "all good, nothing to see",
        lambda *a, **k: None,
        task_launched=False,
        proxy_launched=False,
        secrets=[],
    )
    assert clean["tmpfs_full"] is False


def test_capture_diagnostics_malformed_inspect_output():
    # Wrong token count AND a non-int exit code: the un-parseable fields are omitted, no raise.
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="not|pipes\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=True, proxy_launched=False, secrets=[]
    )
    # Only 2 tokens (!= 3), so NONE of the task fields are set - and no exception escaped.
    assert "task_exit_code" not in diag
    assert "oom_killed" not in diag
    assert "task_state" not in diag


def test_capture_diagnostics_non_int_exit_code_omits_only_that_field():
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="notanint|false|running\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=True, proxy_launched=False, secrets=[]
    )
    # 3 tokens, but the exit code is not an int -> that ONE field is omitted, the others land.
    assert "task_exit_code" not in diag
    assert diag["oom_killed"] is False
    assert diag["task_state"] == "running"


def test_capture_diagnostics_partial_when_proxy_exec_raises():
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="0|false|exited\n", stderr="")
        if argv[:2] == ["docker", "exec"]:
            raise OSError("proxy gone")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=True, proxy_launched=True, secrets=[]
    )
    assert diag["task_exit_code"] == 0
    assert diag["task_state"] == "exited"
    assert "egress_denied" not in diag
    assert "proxy_denied_count" not in diag


def test_capture_diagnostics_empty_squid_log_is_zero_denials():
    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=False, proxy_launched=True, secrets=[]
    )
    # The exec SUCCEEDED with zero TCP_DENIED lines - that is a real "no denials" signal, not
    # "could not check": both keys present, empty/zero.
    assert diag["egress_denied"] == []
    assert diag["proxy_denied_count"] == 0


def test_capture_diagnostics_redacts_secret_in_denied_host():
    secret = "sk-or-very-secret-9999"
    log_line = (
        f"1700000000.000 0 172.0.0.2 TCP_DENIED/403 3822 CONNECT {secret}.evil.com:443 - "
        "HIER_NONE/- text/html\n"
    )

    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout=log_line, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    diag = capture_diagnostics(
        "task1", "proxy1", "", runner, task_launched=False, proxy_launched=True, secrets=[secret]
    )
    assert diag["egress_denied"]
    for entry in diag["egress_denied"]:
        assert secret not in entry["host"]


def test_capture_diagnostics_raising_runner_omits_fields_without_raising():
    def raising_runner(argv, **kwargs):
        raise OSError("docker is on fire")

    diag = capture_diagnostics(
        "task1", "proxy1", "", raising_runner, task_launched=True, proxy_launched=True, secrets=[]
    )
    assert "task_exit_code" not in diag
    assert "oom_killed" not in diag
    assert "task_state" not in diag
    assert "egress_denied" not in diag
    assert "proxy_denied_count" not in diag
    # Transcript-only booleans never touch the runner, so they are still present.
    assert diag["dind_ready"] is None
    assert diag["tmpfs_full"] is False


# ---------------------------------------------------------------------------
# run_in_container's diagnostics_sink wiring (issue #69)
# ---------------------------------------------------------------------------


def test_run_in_container_no_sink_by_default():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    code, output = run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert code == 0
    assert "ok" in output


def test_run_in_container_populates_diagnostics_sink_when_given():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    sink: dict = {}
    run_in_container(
        _cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP, diagnostics_sink=sink
    )
    # The transcript-scan booleans are unconditionally added, so a non-empty sink proves the
    # capture actually ran.
    assert "dind_ready" in sink
    assert "tmpfs_full" in sink


def test_run_in_container_diagnostics_captured_before_task_reap():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, calls = _orchestration_runner(task)
    sink: dict = {}
    run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        diagnostics_sink=sink,
        run_id="abc123def456",
    )
    task_name = "franky-run-abc123def456"
    inspect_idx = next(
        i for i, c in enumerate(calls) if c[:3] == ["docker", "inspect", "-f"] and task_name in c
    )
    rm_idx = next(
        i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"] and task_name in c
    )
    assert inspect_idx < rm_idx


def test_run_in_container_diagnostics_sink_failure_does_not_affect_result(monkeypatch):
    import franky.container as container_mod

    def boom(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(container_mod, "capture_diagnostics", boom)

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    sink: dict = {}
    code, output = run_in_container(
        _cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP, diagnostics_sink=sink
    )
    assert code == 0
    assert "ok" in output
    assert sink == {}


# ---------------------------------------------------------------------------
# Resume + snapshot wiring (issue #71)
# ---------------------------------------------------------------------------


def test_build_docker_argv_resume_wait_adds_env_and_keeps_hardening():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"], resume_wait=True)
    # The resume-wait env flag is present, by-value (non-secret).
    e_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-e"]
    assert "FRANKY_RESUME_WAIT=1" in e_values
    # ALL hardening flags still present with resume on - the boundary is untouched.
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--rm" in argv
    assert "--cap-add=SETUID" in argv
    assert "--cap-add=SETGID" in argv
    assert "--security-opt=systempaths=unconfined" in argv
    assert "--device" in argv and "/dev/net/tun" in argv
    assert "--pids-limit=2048" in argv
    assert "--memory=2048m" in argv and "--memory-swap=2048m" in argv
    # No host bind mount / no docker socket even in resume mode.
    joined = " ".join(argv)
    assert "-v" not in argv and "type=bind" not in joined
    assert "docker.sock" not in joined


def test_build_docker_argv_no_resume_wait_by_default():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    e_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-e"]
    assert "FRANKY_RESUME_WAIT=1" not in e_values


def test_run_in_container_redacts_the_stored_atlassian_login_with_tools_off(connect_atlassian):
    """The refresh token and a previous access token are redacted although no tool is enabled."""
    connect_atlassian(refresh_token="rt-planted", previous_access_tokens=["prev-planted"])
    seen = []

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    _code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=seen.append,
        popen=_fake_popen_factory(["leak rt-planted and Bearer prev-planted\n"]),
    )
    both = out + "".join(seen)
    assert "rt-planted" not in both and "prev-planted" not in both
    assert {"rt-planted", "prev-planted"} <= set(_cfg().secret_values())


def test_run_in_container_snapshots_on_timeout():
    """A timed-out run populates snapshot_sink: extract fires BEFORE the task reap, finalize
    after."""
    calls = []

    def task(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    base_runner, _ = _orchestration_runner(task)

    def runner(argv, **kwargs):
        calls.append(argv)
        return base_runner(argv, **kwargs)

    import tempfile

    dest = tempfile.mktemp(suffix=".snapshot.tar.gz")
    sink = {"dest": dest}
    code, _out = run_in_container(
        _cfg(),
        ["pi"],
        timeout=5,
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        run_id="abc123def456",
        snapshot_sink=sink,
    )
    assert code == 124
    task_name = "franky-run-abc123def456"
    cp_idx = next(i for i, c in enumerate(calls) if c[:2] == ["docker", "cp"] and task_name in c[2])
    rm_idx = next(
        i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"] and task_name in c
    )
    # extract (docker cp) must happen BEFORE the task reap so /work still exists.
    assert cp_idx < rm_idx
    # finalize wrote the snapshot path back into the sink (empty tmpdir -> clean, verified tar).
    assert sink.get("snapshot_path") == dest
    import os as _os

    _os.unlink(dest)


def test_run_in_container_no_snapshot_on_clean_run():
    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, calls = _orchestration_runner(task)
    sink = {"dest": "/tmp/should-not-be-written.tar.gz"}
    code, _out = run_in_container(
        _cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP, snapshot_sink=sink
    )
    assert code == 0
    # No docker cp (extract) on a non-timeout run, and no snapshot path recorded.
    assert not any(c[:2] == ["docker", "cp"] for c in calls)
    assert "snapshot_path" not in sink


def test_run_in_container_resume_restores_after_launch(tmp_path):
    """resume_workspace forces the streaming path and fires untar/chown/touch after launch.

    The tar is piped over `docker exec -i` stdin (no `docker cp` INTO the read-only container)."""
    snap = tmp_path / "snap.tar.gz"
    snap.write_bytes(b"fake-tar-bytes")
    calls = []
    base_runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    )

    def full_runner(argv, **kwargs):
        calls.append(argv)
        # container_running poll -> report the task up so restore proceeds immediately.
        if argv[:3] == ["docker", "inspect", "-f"] and "{{.State.Running}}" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if argv[:2] == ["docker", "exec"] and "curl" in argv:
            return base_runner(argv, **kwargs)
        if argv[:2] == ["docker", "cp"] or argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return base_runner(argv, **kwargs)

    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=full_runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=_fake_popen_factory(["event\n"]),
        run_id="beef00beef00",
        resume_workspace=str(snap),
    )
    # No cp INTO the container; the untar (stdin, as uid 1001) and marker touch fire. No chown.
    assert not any(c[:2] == ["docker", "cp"] and "franky-run-beef00beef00:" in c[3] for c in calls)
    assert any("tar" in c and "-xzf" in c and "-" in c for c in calls)
    assert not any("chown" in c for c in calls)
    assert any("touch" in c for c in calls)


def test_storage_helper_lifetime_covers_delayed_profile_and_resume(monkeypatch):
    import inspect
    from types import SimpleNamespace

    from franky import snapshot

    # Pin the grace to the actual default bounds, including every readiness inspection.
    setup_bound = 0
    inspect_timeout = (
        inspect.signature(container_mod.container_running).parameters["timeout"].default
    )
    for setup in (container_mod.deliver_profile, snapshot.restore_into_container):
        defaults = inspect.signature(setup).parameters
        setup_bound += (
            defaults["ready_polls"].default * (inspect_timeout + defaults["poll_interval"].default)
            + 2 * defaults["timeout"].default
        )
        if "session_timeout" in defaults:  # the thread session untar rides the profile delivery
            setup_bound += defaults["session_timeout"].default
    now = [0.0]
    helper_expiry = []
    base_runner, _ = _orchestration_runner(lambda *a, **kw: None)

    def thread(*, target, args, daemon):
        helper_expiry.append(now[0] + args[-1])
        return SimpleNamespace(start=lambda: None, join=lambda **kw: None)

    def delayed_setup(*args, **kwargs):
        now[0] += setup_bound / 2
        return True

    monkeypatch.setattr(container_mod.subprocess, "run", base_runner)
    monkeypatch.setattr(container_mod, "_storage_sample", lambda *a: (0, 10**9))
    monkeypatch.setattr(container_mod.threading, "Thread", thread)
    monkeypatch.setattr(
        container_mod.threading,
        "Timer",
        lambda *a: SimpleNamespace(start=lambda: None, cancel=lambda: None),
    )
    monkeypatch.setattr(container_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(container_mod, "deliver_profile", delayed_setup)
    monkeypatch.setattr(snapshot, "restore_into_container", delayed_setup)
    code, _out = run_in_container(
        _cfg(),
        ["pi"],
        timeout=60,
        runner=base_runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=_fake_popen_factory(["event\n"]),
        profile_bundle=b"fixture",
        resume_workspace="fixture.tar.gz",
    )
    assert code == 0
    assert helper_expiry[0] >= now[0] + 60 + 30


def test_run_in_container_resume_false_restore_does_not_change_result(tmp_path):
    """A failed restore (untar rc!=0) must not change the returned (code, output)."""
    snap = tmp_path / "snap.tar.gz"
    snap.write_bytes(b"fake-tar-bytes")

    base_runner, _ = _orchestration_runner(
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    )

    def runner(argv, **kwargs):
        if argv[:3] == ["docker", "inspect", "-f"] and "{{.State.Running}}" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if "tar" in argv and "-xzf" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="untar boom")
        return base_runner(argv, **kwargs)

    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=_fake_popen_factory(["streamed line\n"]),
        run_id="beeff00dbeef",
        resume_workspace=str(snap),
    )
    # The stream still drained normally; the restore failure is swallowed.
    assert code == 0
    assert "streamed line" in out


class _BlockingPopen:
    """Fake Popen whose stdout NEVER yields a line and never EOFs until .kill() is called.

    Models a silently-wedged container (engine hung, or a resume-wait container that never gets
    its marker): `for line in proc.stdout` blocks on __next__ until the watchdog kills it."""

    def __init__(self):
        import threading

        self.returncode = -9
        self._killed = threading.Event()
        self.stdout = self

    def __iter__(self):
        return self

    def __next__(self):
        self._killed.wait()  # block until killed, then signal EOF
        raise StopIteration

    def close(self):
        pass

    def wait(self, timeout=None):
        self._killed.wait(timeout)
        return self.returncode

    def kill(self):
        self._killed.set()


def test_run_in_container_watchdog_kills_silent_hang():
    """A container that produces NO output must still hit the wall-clock watchdog and time out,
    not block forever (never-hang). Proves the streaming path's timeout fires without any line."""

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)

    def blocking_popen(argv, **kwargs):
        return _BlockingPopen()

    # Sub-second timeout keeps the suite fast; the watchdog Timer fires at ~0.3s and kills the
    # (otherwise forever-blocking) fake, ending the read loop.
    code, _out = run_in_container(
        _cfg(),
        ["pi"],
        timeout=0.3,
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=lambda _line: None,
        popen=blocking_popen,
    )
    assert code == 124  # CONTAINER_TIMEOUT_CODE - the silent hang was killed by the watchdog


# ---------------------------------------------------------------------------
# Mid-run steering mailbox (issue #72, `franky job attach`)
# ---------------------------------------------------------------------------


def test_build_steer_argv_default_file():
    assert build_steer_argv("franky-run-abc") == [
        "docker",
        "exec",
        "-i",
        "franky-run-abc",
        "tee",
        "-a",
        STEER_FILE,
    ]


def test_build_steer_argv_custom_file():
    assert build_steer_argv("c", steer_file="/tmp/other")[-1] == "/tmp/other"


def test_deliver_steer_true_on_rc0_and_passes_message_on_stdin():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="the message", stderr="")

    assert deliver_steer("franky-run-abc", "stop refactoring", runner=runner) is True
    (argv, kwargs) = calls[0]
    assert argv == build_steer_argv("franky-run-abc")
    assert kwargs["input"] == "stop refactoring"


def test_deliver_steer_false_on_nonzero_rc():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such container")

    assert deliver_steer("gone", "msg", runner=runner) is False


def test_deliver_steer_false_never_raises_on_oserror():
    def runner(argv, **kwargs):
        raise OSError("docker not found")

    assert deliver_steer("c", "msg", runner=runner) is False


def test_deliver_steer_false_never_raises_on_timeout():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 10.0))

    assert deliver_steer("c", "msg", runner=runner) is False


def test_deliver_steer_does_not_return_captured_output():
    # tee echoes the message back to its own stdout; deliver_steer must never surface that -
    # returning a bool only, never the captured stdout/stderr.
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=kwargs.get("input", ""), stderr="")

    result = deliver_steer("c", "a secret-looking correction", runner=runner)
    assert result is True  # the only thing deliver_steer returns is the bool


# ---------------------------------------------------------------------------
# Review thread sessions (`review-pr --thread`): stream-in and copy-out only, never a mount
# ---------------------------------------------------------------------------


def _session_runner(untar_rc=0):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        if "-xzf" in argv:
            if "stdin" in kwargs:
                kwargs["stdin"].read()
                return subprocess.CompletedProcess(argv, untar_rc, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return runner, calls


def test_deliver_profile_streams_profile_then_session_then_marker(tmp_path):
    tar = tmp_path / "session.tar.gz"
    tar.write_bytes(b"session-bytes")
    runner, calls = _session_runner()
    assert container_mod.deliver_profile(
        "t1", b"profile-bytes", runner, sleeper=NOOP_SLEEP, session_tar=str(tar)
    )
    steps = [(argv, kw) for argv, kw in calls if argv[:2] == ["docker", "exec"]]
    assert [("input" in kw, "stdin" in kw, "touch" in argv) for argv, kw in steps] == [
        (True, False, False),  # profile bytes
        (False, True, False),  # session streamed from the file, never read into argv/env
        (False, False, True),  # marker last
    ]
    assert all(CONTAINER_HOME in argv for argv, _kw in steps[:2])  # both untar into HOME
    assert not any(argv[:2] == ["docker", "cp"] for argv, _kw in calls)


def test_deliver_profile_session_only_and_marker_survives_a_failed_session_untar(tmp_path):
    tar = tmp_path / "session.tar.gz"
    tar.write_bytes(b"session-bytes")
    runner, calls = _session_runner(untar_rc=2)
    assert container_mod.deliver_profile(
        "t1", None, runner, sleeper=NOOP_SLEEP, session_tar=str(tar)
    )
    execs = [argv for argv, _kw in calls if argv[:2] == ["docker", "exec"]]
    assert len(execs) == 2 and "touch" in execs[-1]


def test_deliver_profile_streams_private_prompt_before_marker_and_fails_closed():
    runner, calls = _session_runner()
    assert container_mod.deliver_profile(
        "t1", None, runner, sleeper=NOOP_SLEEP, private_prompt_tar=b"private-archive"
    )
    execs = [(argv, kw) for argv, kw in calls if argv[:2] == ["docker", "exec"]]
    assert execs[0][1]["input"] == b"private-archive"
    assert "/tmp" in execs[0][0]
    assert "touch" in execs[-1][0]

    runner, calls = _session_runner()

    def failed_untar(argv, **kwargs):
        if "-xzf" in argv:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="")
        return runner(argv, **kwargs)

    assert not container_mod.deliver_profile(
        "t1", None, failed_untar, sleeper=NOOP_SLEEP, private_prompt_tar=b"private-archive"
    )
    assert not any("touch" in argv for argv, _ in calls)


def test_run_in_container_session_tar_alone_uses_injection_mode_without_new_mounts(tmp_path):
    tar = tmp_path / "session.tar.gz"
    tar.write_bytes(b"session-bytes")
    base_runner, calls = _orchestration_runner(lambda *a, **k: None)
    runner, _ = _session_runner()
    seen = {}

    def full_runner(argv, **kwargs):
        if argv[:2] == ["docker", "exec"] and "curl" not in argv:
            return runner(argv, **kwargs)
        return base_runner(argv, **kwargs)

    def popen(argv, **kwargs):
        seen["argv"] = argv
        return _FakePopen(["event\n"])

    code, _out = run_in_container(
        _cfg(),
        ["claude"],
        runner=full_runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=popen,
        session_tar=str(tar),
    )
    assert code == 0
    assert f"{PROFILE_WAIT_VAR}=1" in seen["argv"]
    mounts = [seen["argv"][i + 1] for i, t in enumerate(seen["argv"]) if t == "--mount"]
    assert mounts == [
        "type=volume,dst=/work",
        "type=volume,dst=/home/franky",
        "type=volume,dst=/tmp",
    ]
    assert not any("-v" == t or "--volume" == t for t in seen["argv"])


class _CpPopen:
    """Fake `docker cp ... -` for the session copy-out: serves one tar, or fails."""

    def __init__(self, data, returncode=0):
        import io

        self.stdout = io.BytesIO(data)
        self.returncode = returncode
        self.done = False

    def kill(self):
        self.done = True

    def poll(self):
        return self.returncode if self.done else None

    def wait(self, timeout=None):
        self.done = True
        return self.returncode


def _session_tar_bytes(name, data=b"{}"):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _copy_out_run(
    tmp_path,
    *,
    task_code=0,
    cp=None,
    paths=None,
    timeout=None,
    on_timeout=None,
    stream=None,
    marker_raises=False,
    calls=None,
):
    calls = [] if calls is None else calls
    runner, runner_calls = _orchestration_runner(lambda *a, **k: None)

    def logged_runner(argv, **kwargs):
        calls.append(argv)
        if marker_raises and argv[-1].startswith(snapshot.SESSION_COPIED_MARKER):
            raise subprocess.TimeoutExpired(argv, 10)
        return runner(argv, **kwargs)

    def popen(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "cp"]:
            return cp(argv)
        return _FakePopen(stream or ["event\n"], returncode=task_code)

    sink = {
        "paths": paths or [".claude/projects/-work/s1.jsonl", ".claude/projects/-work/s1"],
        "dest": str(tmp_path),
        "max_bytes": 10**6,
    }
    if on_timeout is not None:
        sink["on_timeout"] = on_timeout
    with mock.patch.object(container_mod, "_hold_nonce", return_value="n0nce"):
        run_in_container(
            _cfg(),
            ["claude"],
            runner=logged_runner,
            env={},
            sleeper=NOOP_SLEEP,
            popen=popen,
            run_id="abc123def456",
            session_sink=sink,
            **({} if timeout is None else {"timeout": timeout}),
        )
    return calls, sink


def test_run_in_container_session_copy_out_streams_only_session_paths_before_reap(tmp_path):
    def cp(argv):
        if argv[2].endswith("s1.jsonl"):
            return _CpPopen(_session_tar_bytes("s1.jsonl"))
        return _CpPopen(b"", returncode=1)  # no side dir: optional

    calls, sink = _copy_out_run(tmp_path, cp=cp)
    task = "franky-run-abc123def456"
    cps = [i for i, c in enumerate(calls) if c[:2] == ["docker", "cp"]]
    reap = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"] and task in c)
    assert [calls[i] for i in cps] == [
        ["docker", "cp", f"{task}:/home/franky/.claude/projects/-work/s1.jsonl", "-"],
        ["docker", "cp", f"{task}:/home/franky/.claude/projects/-work/s1", "-"],
    ]
    assert cps[-1] < reap
    # Never the whole project dir (Claude project memory lives in -work/memory/).
    assert not any(calls[i][2].endswith(("-work", "-work/.", "memory")) for i in cps)
    assert sink["status"] == "ok"
    assert (tmp_path / ".claude/projects/-work/s1.jsonl").read_text() == "{}"


# The entrypoint's hold line (nonce pinned by _copy_out_run), split across two read chunks.
_HOLD_STREAM = ["event\nfranky: engine exited, hol", "ding for session copy n0nce\n"]
_MARKER = [
    "docker",
    "exec",
    "franky-run-abc123def456",
    "touch",
    "/tmp/.franky-session-copied-n0nce",
]


def _ok_cp(argv):
    if argv[2].endswith("s1.jsonl"):
        return _CpPopen(_session_tar_bytes("s1.jsonl"))
    return _CpPopen(b"", returncode=1)  # no side dir: optional


def test_build_docker_argv_session_hold_flag_only_when_asked():
    assert not any(
        a.startswith("FRANKY_SESSION_HOLD") for a in build_docker_argv("franky", {}, ["pi"])
    )
    argv = build_docker_argv("franky", {}, ["pi"], session_hold="n0nce")
    assert argv[argv.index("FRANKY_SESSION_HOLD=n0nce") - 1] == "-e"
    assert "--rm" in argv  # the hold keeps --rm: a container never outlives its cap


def test_run_in_container_copies_during_the_hold_then_releases_it(tmp_path):
    # --rm removes the container as soon as the engine exits, so the copy must happen while the
    # entrypoint holds it: after the hold line, before the release marker, never again later.
    calls, sink = _copy_out_run(tmp_path, cp=_ok_cp, stream=_HOLD_STREAM)
    task = "franky-run-abc123def456"
    run = next(c for c in calls if c[:2] == ["docker", "run"] and task in c)
    assert "FRANKY_SESSION_HOLD=n0nce" in run and "--rm" in run
    cps = [i for i, c in enumerate(calls) if c[:2] == ["docker", "cp"]]
    marker = calls.index(_MARKER)
    reap = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"] and task in c)
    assert len(cps) == 2 and cps[-1] < marker < reap
    assert sink["status"] == "ok"


def test_run_in_container_finds_the_hold_line_before_long_trailing_output(tmp_path):
    # A leftover engine subprocess can write after the hold line in the same chunk.
    stream = ["franky: engine exited, holding for session copy n0nce\n" + "x" * 2000]
    calls, sink = _copy_out_run(tmp_path, cp=_ok_cp, stream=stream)
    assert sink["status"] == "ok" and _MARKER in calls


@pytest.mark.parametrize(
    "stream",
    [
        # Engine output quoting the phrase, as in a review of this repository.
        ['{"type":"user","content":"franky: engine exited, holding for session copy n0nce"}\n'],
        # The phrase without this run's nonce.
        ["franky: engine exited, holding for session copy\n"],
        # The right line, but as the tail of a line too long to have been checked whole.
        ["y" * 600, "franky: engine exited, holding for session copy n0nce\n"],
    ],
)
def test_run_in_container_ignores_anything_but_the_whole_hold_line(tmp_path, stream):
    calls, _sink = _copy_out_run(tmp_path, cp=_ok_cp, stream=stream)
    assert _MARKER not in calls


def test_run_in_container_releases_the_hold_when_the_copy_fails(tmp_path):
    calls, sink = _copy_out_run(
        tmp_path, cp=lambda argv: _CpPopen(b"", returncode=1), stream=_HOLD_STREAM
    )
    assert sink["status"] == "failed"
    assert _MARKER in calls


def test_run_in_container_hold_release_failure_still_reaps(tmp_path):
    calls, sink = _copy_out_run(tmp_path, cp=_ok_cp, stream=_HOLD_STREAM, marker_raises=True)
    assert sink["status"] == "ok"
    assert any(c[:3] == ["docker", "rm", "-f"] and "franky-run-abc123def456" in c for c in calls)


def test_run_in_container_interrupted_hold_copy_still_releases_and_reaps(tmp_path):
    def cp(argv):
        raise KeyboardInterrupt

    calls = []
    with pytest.raises(KeyboardInterrupt):
        _copy_out_run(tmp_path, cp=cp, stream=_HOLD_STREAM, calls=calls)
    assert _MARKER in calls
    assert any(c[:3] == ["docker", "rm", "-f"] and "franky-run-abc123def456" in c for c in calls)


def test_run_in_container_session_copy_out_skipped_on_failure(tmp_path):
    calls, sink = _copy_out_run(tmp_path, task_code=1, cp=lambda argv: pytest.fail("no copy"))
    assert not any(c[:2] == ["docker", "cp"] for c in calls) and "status" not in sink


def test_run_in_container_session_copy_out_reports_failure_and_too_large(tmp_path):
    _calls, sink = _copy_out_run(tmp_path, cp=lambda argv: _CpPopen(b"", returncode=1))
    assert sink["status"] == "failed"
    big = _session_tar_bytes("s1.jsonl", b"x" * 2_000_000)
    _calls, sink = _copy_out_run(tmp_path / "b", cp=lambda argv: _CpPopen(big))
    assert sink["status"] == "too_large"


@pytest.mark.parametrize("on_timeout", [True, False])
def test_run_in_container_session_copy_out_on_timeout_only_when_opted_in(tmp_path, on_timeout):
    def cp(argv):
        if argv[2].endswith("s1.jsonl"):
            return _CpPopen(_session_tar_bytes("s1.jsonl"))
        return _CpPopen(b"", returncode=1)

    calls, sink = _copy_out_run(tmp_path, cp=cp, timeout=0, on_timeout=on_timeout)
    task = "franky-run-abc123def456"
    cps = [i for i, c in enumerate(calls) if c[:2] == ["docker", "cp"]]
    reap = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"] and task in c)
    assert bool(cps) is on_timeout
    assert all(i < reap for i in cps)
    assert sink.get("status") == ("ok" if on_timeout else None)


def test_run_in_container_session_and_workspace_restore_wait_in_order(monkeypatch, tmp_path):
    """`job resume` V2: a session tar and a workspace in one container. The task starts in BOTH
    wait modes; the session streams in (profile channel, HOME marker) before /work is restored."""
    from franky import snapshot

    order, argvs = [], []
    monkeypatch.setattr(
        container_mod,
        "deliver_profile",
        lambda task, bundle, runner, **k: order.append(("profile", bundle, k["session_tar"])),
    )
    monkeypatch.setattr(
        snapshot, "restore_into_container", lambda task, snap, runner, **k: order.append("work")
    )
    runner, _ = _orchestration_runner(lambda *a, **k: None)
    popen = _fake_popen_factory(["event\n"])

    def logged_popen(argv, **kwargs):
        argvs.append(argv)
        return popen(argv, **kwargs)

    run_in_container(
        _cfg(),
        ["claude"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        popen=logged_popen,
        run_id="abc123def456",
        session_tar="s.tar.gz",
        resume_workspace="w.tar.gz",
    )
    assert order == [("profile", None, "s.tar.gz"), "work"]
    task_argv = argvs[0]
    assert f"{PROFILE_WAIT_VAR}=1" in task_argv and f"{snapshot.RESUME_WAIT_ENV}=1" in task_argv


def test_run_in_container_extra_secrets_redacted_from_stream_and_output():
    """Host-only secrets (the JIRA token) are redacted from the streamed and stored output."""
    planted = "jira-tok-planted"
    seen = []

    def task(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, calls = _orchestration_runner(task)
    code, out = run_in_container(
        _cfg(),
        ["pi"],
        runner=runner,
        env={},
        sleeper=NOOP_SLEEP,
        progress=seen.append,
        popen=_fake_popen_factory([f"leaked {planted}\n"]),
        extra_secrets=[planted],
    )
    assert planted not in out and planted not in "".join(seen)
    assert "***REDACTED***" in out
    assert all(planted not in " ".join(map(str, c)) for c in calls)
