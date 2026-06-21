"""Run the inner engine inside a hardened, disposable Docker container.

WHY the hardening is load-bearing: the engine runs autonomously (claude with
--dangerously-skip-permissions, pi with its default tools), so OS-level isolation - not
tool prompts - is what bounds it. The argv is built by a pure function so tests can assert
the hardening flags WITHOUT ever invoking docker. Secret values are passed by VAR NAME only
(`-e KEY`), so docker inherits the value from Franky's own env and the value never lands on
the argv or in `ps` output.

WHY the egress proxy is load-bearing: open egress would let a prompt-injected agent exfil
the creds it carries. So the task container runs on a Docker `--internal` network (NO route
to the internet at all) whose only peer is a Squid proxy enforcing a default-deny domain
allowlist. Squid does blind HTTPS CONNECT (no TLS termination), so the creds tunnel through
it without the proxy ever seeing them. We refuse to run the task unless the proxy is
confirmed healthy first (fail-closed). All docker argv is built by pure functions here;
the allowlist POLICY lives in egress.py.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid

from . import egress
from .config import redact

# Long agent runs: a full clone-build-test-PR cycle can take many minutes. 30 min cap.
FALLBACK_TIMEOUT_SECS = 1800

# The non-root user baked into the image (Dockerfile: useradd franky). The writable tmpfs
# mounts are owned by this uid/gid so the agent can actually clone, commit, and write its
# HOME config under a --read-only root. (The --tmpfs PATH:opts short form silently ignores
# mode=, but honours uid=/gid=, which is what makes the dirs writable by a non-root user.)
_RUN_UID = 1001
_RUN_GID = 1001
_HOME = "/home/franky"

# Hardening flags applied to every run. No bind mounts, no docker socket: the repo is
# cloned INSIDE the container, so the agent never touches the host filesystem. Root FS is
# read-only; only /work (the clone) and the agent's HOME (git/gh config) are writable tmpfs,
# both owned by the non-root run user so git/gh actually work under --read-only.
_HARDENING = [
    "--rm",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--read-only",
    "--tmpfs", f"/work:exec,uid={_RUN_UID},gid={_RUN_GID}",
    "--tmpfs", f"{_HOME}:exec,uid={_RUN_UID},gid={_RUN_GID}",
    "--pids-limit=512",
    "--memory=4g",
]

# The Squid proxy image (built from proxy/) and the port it listens on inside the net.
PROXY_IMAGE = "franky-proxy"
PROXY_PORT = 3128

# Hardening for the proxy container. Mirrors _HARDENING but the writable tmpfs dirs are
# squid's, not the agent's: squid (debian package user `proxy`, uid/gid 13 - verified in the
# franky-proxy image) writes its rendered config, pid, and logs there under a --read-only
# root. A bare --tmpfs mounts root-owned and `mode=` is silently ignored by the short form
# (see _HARDENING note), so we MUST pin uid=/gid= to squid's user or it cannot write and
# crashes on start. The proxy is light (no clone/build), so 1g is plenty.
_PROXY_UID = 13
_PROXY_GID = 13
_PROXY_HARDENING = [
    "--rm",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--read-only",
    "--tmpfs", f"/run:exec,uid={_PROXY_UID},gid={_PROXY_GID}",
    "--tmpfs", f"/var/log/squid:uid={_PROXY_UID},gid={_PROXY_GID}",
    "--tmpfs", f"/var/spool/squid:uid={_PROXY_UID},gid={_PROXY_GID}",
    "--pids-limit=512",
    "--memory=1g",
]

# Readiness poll for the proxy's HEALTHCHECK. 30 * 0.5s = 15s ceiling before we fail-closed.
PROXY_READY_POLLS = 30
PROXY_READY_INTERVAL = 0.5


def build_docker_argv(
    image: str,
    passthrough_env: dict[str, str],
    inner_argv: list[str],
    name: str | None = None,
    network: str | None = None,
    proxy_url: str | None = None,
) -> list[str]:
    """Build the full `docker run` argv. Pure - no docker invoked.

    `-e KEY` (name only) for each passthrough var: docker reads the value from the parent
    process env, keeping the secret value off the argv. `--name` is added so a reaper can
    target this exact container after a timeout. Image + inner argv go last.

    When `network` is given the container joins that (internal) network. When `proxy_url` is
    given we force ALL traffic through the proxy: the HTTP(S)_PROXY env vars are passed BY
    VALUE (inline `-e KEY=VALUE`) because a proxy URL is not a secret. Crucially we also set
    `--dns 127.0.0.1`: the container's own DNS is dead, so a hostile agent cannot DNS-exfil
    or resolve an off-allowlist host directly; proxied clients still work because Squid does
    the DNS resolution on their behalf. Existing call sites pass neither and are unchanged.
    """
    container_name = name or f"franky-run-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", *_HARDENING, "--name", container_name]
    if network:
        argv += ["--network", network]
    if proxy_url:
        # By-value (non-secret) proxy config. Both upper/lower case forms: tools disagree on
        # which they read. NO_PROXY keeps loopback direct. --dns 127.0.0.1 kills in-container
        # name resolution so the only way out is via the proxy.
        argv += [
            "-e", f"HTTP_PROXY={proxy_url}",
            "-e", f"HTTPS_PROXY={proxy_url}",
            "-e", f"http_proxy={proxy_url}",
            "-e", f"https_proxy={proxy_url}",
            "-e", "NO_PROXY=localhost,127.0.0.1",
            "-e", "no_proxy=localhost,127.0.0.1",
            "--dns", "127.0.0.1",
        ]
    for key in passthrough_env:
        argv += ["-e", key]
    argv += [image, *inner_argv]
    return argv


def build_network_argv(net_name: str) -> list[str]:
    """`docker network create` for an --internal network: NO gateway to the host/internet, so
    the only reachable peer is whatever we also attach (the proxy). This is the wall."""
    return ["docker", "network", "create", "--internal", "--driver", "bridge", net_name]


def build_network_connect_argv(net_name: str, name: str) -> list[str]:
    """Attach an existing container to the network. Used to put the proxy on the internal net
    (the task container joins via `docker run --network` instead)."""
    return ["docker", "network", "connect", net_name, name]


def build_proxy_argv(proxy_image: str, proxy_name: str, allowed_domains: list[str]) -> list[str]:
    """`docker run -d` for the Squid proxy. The allowlist is passed BY VALUE (inline
    `-e FRANKY_ALLOWED_DOMAINS=...`), unlike secrets which are name-only. WHY by-value is
    correct here and does not weaken the secret-by-name discipline: the domain list is policy
    (which hosts may be reached), not a credential, and the proxy container is given NO cred
    env at all - so there is nothing secret on its argv to leak."""
    return [
        "docker", "run", "-d", *_PROXY_HARDENING,
        "--name", proxy_name,
        "-e", f"FRANKY_ALLOWED_DOMAINS={','.join(allowed_domains)}",
        proxy_image,
    ]


def proxy_url(proxy_name: str) -> str:
    """The URL the task container uses to reach the proxy, by container name on the shared
    internal network (docker's embedded DNS resolves the name)."""
    return f"http://{proxy_name}:{PROXY_PORT}"


def _reap(name: str, runner) -> bool:
    """Best-effort `docker rm -f` so a container does not linger after a timeout/error.
    Returns True iff the reap succeeded. Never raises - the reaper must not mask the
    original outcome - but a failure is reported (see run_in_container) because a surviving
    container still holds the injected tokens in its env."""
    try:
        proc = runner(["docker", "rm", "-f", name], capture_output=True, text=True)
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


def _reap_network(net_name: str, runner) -> bool:
    """Best-effort `docker network rm`. Same never-raise contract as _reap. A leaked network
    is a low-severity resource leak (it holds NO creds), unlike a leaked task container."""
    try:
        proc = runner(["docker", "network", "rm", net_name], capture_output=True, text=True)
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


def _wait_proxy_ready(proxy_name: str, runner, sleeper=time.sleep) -> bool:
    """Poll the proxy container's HEALTHCHECK status until it reports "healthy". The
    healthcheck asserts default-deny (a known-denied host gets a 403), so "healthy" means the
    allowlist actually loaded - not mere liveness. Returns True on healthy, False if the poll
    cap is exhausted. Never raises (a docker error just counts as not-yet-ready). `sleeper`
    is injectable so tests pass a no-op."""
    for _ in range(PROXY_READY_POLLS):
        try:
            proc = runner(
                ["docker", "inspect", "-f", "{{.State.Health.Status}}", proxy_name],
                capture_output=True,
                text=True,
            )
        except Exception:
            proc = None
        status = (getattr(proc, "stdout", "") or "").strip()
        if status == "healthy":
            return True
        sleeper(PROXY_READY_INTERVAL)
    return False


def _run(runner, argv):
    """Run a fire-and-check docker subcommand (network create/connect, proxy run), capturing
    output. Returns the proc (or None on OSError) so the caller can check returncode. We do
    NOT pass these through the task's child_env: orchestration commands need no creds."""
    try:
        return runner(argv, capture_output=True, text=True)
    except OSError:
        return None


class _AbortRun(Exception):
    """Internal control-flow: a pre-task orchestration step failed. Raising (instead of an
    early return) routes through the single return below so the `finally`-appended teardown
    warnings are never discarded. Carries the operator-facing (secret-free) message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def run_in_container(
    cfg,
    inner_argv: list[str],
    image: str = "franky",
    timeout: int = FALLBACK_TIMEOUT_SECS,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
    sleeper=time.sleep,
) -> tuple[int, str]:
    """Run the inner engine in a hardened, egress-controlled container; return
    (returncode, redacted_output).

    Topology: an --internal network (no internet route) hosts a Squid proxy (default-deny
    allowlist) and the task container. The task's HTTP(S)_PROXY points at the proxy and its
    own DNS is killed, so its only path out is through the allowlist. We refuse to start the
    task until the proxy is confirmed healthy (fail-closed). A `finally` ALWAYS tears down
    whatever was created, in order task -> proxy -> net.

    The child env is os.environ + cfg.passthrough_env, so the `-e KEY` (name-only) flags
    resolve to real values inside the task without those values ever hitting the argv. The
    PROXY gets NO cred env. `runner`/`sleeper` are injectable so tests run fakes and never
    touch real docker. All returned output is scrubbed of secret values.
    """
    net = f"franky-net-{uuid.uuid4().hex[:12]}"
    proxy = f"franky-proxy-{uuid.uuid4().hex[:12]}"
    task = f"franky-run-{uuid.uuid4().hex[:12]}"

    allowed = egress.build_allowlist(cfg.engine, cfg.passthrough_env, cfg.extra_allowed_domains)

    child_env = dict(os.environ if env is None else env)
    child_env.update(cfg.passthrough_env)

    secrets = cfg.secret_values()

    net_created = False
    proxy_launched = False
    task_launched = False  # an OSError on spawn means docker never created the container
    code, output = 1, ""

    try:
        # 1. Internal network. If this fails there is nothing to reap (it was not created).
        if getattr(_run(runner, build_network_argv(net)), "returncode", 1) != 0:
            raise _AbortRun("franky: could not create egress network - refusing to run")
        net_created = True

        # 2. Proxy container (detached). On failure: teardown runs in `finally`.
        if getattr(_run(runner, build_proxy_argv(PROXY_IMAGE, proxy, allowed)), "returncode", 1) != 0:
            raise _AbortRun("franky: could not start egress proxy - refusing to run")
        proxy_launched = True

        # 3. Attach the proxy to the internal net (the task joins via --network on run).
        if getattr(_run(runner, build_network_connect_argv(net, proxy)), "returncode", 1) != 0:
            raise _AbortRun("franky: could not attach egress proxy to the network - refusing to run")

        # 4. Fail-closed gate: NEVER run the task without a confirmed-healthy proxy.
        if not _wait_proxy_ready(proxy, runner, sleeper):
            raise _AbortRun("franky: egress proxy did not become ready - refusing to run (fail-closed)")

        # 5. The task container: on the internal net, all traffic forced through the proxy.
        argv = build_docker_argv(
            image, cfg.passthrough_env, inner_argv,
            name=task, network=net, proxy_url=proxy_url(proxy),
        )
        try:
            proc = runner(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=child_env,
            )
            task_launched = True
            code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            task_launched = True
            code, output = 1, f"franky: container timed out after {timeout}s"
        except OSError as exc:
            code, output = 1, f"franky: could not launch docker ({exc})"
    except _AbortRun as abort:
        # A pre-task step failed. code/output set here; teardown still runs in `finally` and
        # its warnings append to THIS output, which the single return below surfaces.
        code, output = 1, abort.message
    finally:
        # Best-effort teardown, ALWAYS, in order task -> proxy -> net. A reap FAILURE on the
        # TASK container is surfaced at full severity because it holds the injected creds. A
        # proxy/net reap failure is a lower-severity resource leak (the proxy holds NO creds).
        if task_launched and not _reap(task, runner):
            output += f"\nfranky: WARNING container {task} may not have been removed - check `docker ps -a`"
        if proxy_launched and not _reap(proxy, runner):
            output += f"\nfranky: WARNING egress proxy {proxy} may not have been removed (resource leak, no creds) - check `docker ps -a`"
        if net_created and not _reap_network(net, runner):
            output += f"\nfranky: WARNING egress network {net} may not have been removed (resource leak) - check `docker network ls`"

    return code, redact(output, secrets)


def ensure_image(image: str = "franky", runner=subprocess.run) -> bool:
    """True iff the image already exists locally (`docker image inspect`). Does NOT build -
    the caller decides whether to build. `runner` injectable for tests."""
    try:
        proc = runner(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0
