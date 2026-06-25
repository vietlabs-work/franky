import subprocess


from franky.config import Config
from franky.container import (
    FRANKY_PROXY_IMAGE_VAR,
    GHCR_REPO_VAR,
    _HOME_TMPFS_SIZE,
    build_docker_argv,
    build_network_argv,
    build_network_connect_argv,
    build_proxy_argv,
    ensure_image_available,
    image_exists,
    resolve_image,
    run_in_container,
)
from franky.engine import PiEngine
from franky.profile import PROFILE_BUNDLE_VAR

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
    # /work and HOME are both writable tmpfs, owned by the non-root run uid so git/gh work
    # under --read-only. (Verified against the real image: bare /work:exec is root-owned and
    # a non-root agent cannot write to it; uid= fixes that.)
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    work = next(s for s in tmpfs_specs if s.startswith("/work:"))
    home = next(s for s in tmpfs_specs if s.startswith("/home/franky:"))
    assert "exec" in work and "uid=1001" in work and "gid=1001" in work
    assert "exec" in home and "uid=1001" in home and "gid=1001" in home


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
    # Raised limits: dockerd+containerd+nested procs need headroom; tmpfs image storage is RAM,
    # capped with no swap blow-up.
    assert "--pids-limit=2048" in argv
    assert "--memory=8g" in argv
    assert "--memory-swap=8g" in argv
    # HOME tmpfs is size-capped (holds the rootless docker data root) so a huge pull ENOSPCs
    # before the --memory cap OOM-kills dockerd. The XDG runtime dir holds docker.sock.
    tmpfs_specs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    home = next(s for s in tmpfs_specs if s.startswith("/home/franky:"))
    assert f"size={_HOME_TMPFS_SIZE}" in home
    assert any(s.startswith("/run/user/1001:") and "uid=1001" in s for s in tmpfs_specs)
    # rootless dockerd + rootlesskit also write under /run and /tmp (bare tmpfs).
    assert "/run:exec" in tmpfs_specs
    assert "/tmp:exec" in tmpfs_specs


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
    i_inspect = first_index(lambda a: a[:2] == ["docker", "inspect"])
    i_task_run = first_index(lambda a: a[:2] == ["docker", "run"] and a[2] != "-d")
    i_rm_task = first_index(
        lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-run-" in x for x in a)
    )
    i_rm_proxy = first_index(
        lambda a: a[:3] == ["docker", "rm", "-f"] and any("franky-proxy-" in x for x in a)
    )
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
    code, out = run_in_container(
        _cfg(), ["pi"], timeout=5, runner=runner, env={}, sleeper=NOOP_SLEEP
    )
    assert code != 0
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


def test_resolve_image_proxy_uses_proxy_var():
    ref = resolve_image({}, FRANKY_PROXY_IMAGE_VAR, "franky-proxy")
    assert ref.startswith("ghcr.io/vietlabs-work/franky-proxy:")


def test_resolve_image_ghcr_repo_override():
    # FRANKY_GHCR_REPO retargets the namespace (publishing org can move with no code change),
    # while the per-image FRANKY_IMAGE override still wins outright.
    ref = resolve_image({GHCR_REPO_VAR: "ghcr.io/acme"})
    assert ref.startswith("ghcr.io/acme/franky:")
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


def test_build_docker_argv_profile_bundle_injected_by_value():
    bundle = "SGVsbG8gV29ybGQ="  # base64("Hello World")
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"], profile_bundle=bundle)
    assert f"{PROFILE_BUNDLE_VAR}={bundle}" in argv


def test_build_docker_argv_no_profile_bundle_when_none():
    argv = build_docker_argv("franky", {"GH_TOKEN": "x"}, ["pi"])
    assert PROFILE_BUNDLE_VAR not in " ".join(argv)


def test_build_docker_argv_profile_bundle_before_passthrough_env():
    """Profile bundle must appear before the passthrough name-only -e flags."""
    bundle = "SGVsbG8="
    argv = build_docker_argv(
        "franky", {"GH_TOKEN": "x", "OPENROUTER_API_KEY": "y"}, ["pi"], profile_bundle=bundle
    )
    bundle_idx = argv.index(f"{PROFILE_BUNDLE_VAR}={bundle}")
    # Name-only -e flags come after the by-value bundle
    name_only_idxs = [i for i, a in enumerate(argv) if a in ("GH_TOKEN", "OPENROUTER_API_KEY")]
    assert name_only_idxs, "passthrough env not found"
    assert all(bundle_idx < idx for idx in name_only_idxs)


def test_run_in_container_threads_profile_bundle():
    """run_in_container forwards profile_bundle to build_docker_argv."""
    captured = {}

    def task(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    bundle = "dGVzdA=="
    run_in_container(
        _cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP, profile_bundle=bundle
    )
    assert f"{PROFILE_BUNDLE_VAR}={bundle}" in captured["argv"]


def test_run_in_container_no_bundle_by_default():
    """Without profile_bundle, FRANKY_PROFILE_BUNDLE must not appear in the task argv."""
    captured = {}

    def task(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    runner, _ = _orchestration_runner(task)
    run_in_container(_cfg(), ["pi"], runner=runner, env={}, sleeper=NOOP_SLEEP)
    assert PROFILE_BUNDLE_VAR not in " ".join(captured["argv"])


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
