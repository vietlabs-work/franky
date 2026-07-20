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

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import egress, franky_version, snapshot
from .config import redact
from .engine import CODEX_AUTH_VOLUME, CODEX_SUBSCRIPTION_VAR
from .profile import PROFILE_BUNDLE_VAR

# Long agent runs: a full clone-build-test-PR cycle can take many minutes. 30 min cap.
FALLBACK_TIMEOUT_SECS = 1800

# On a timeout Franky kills the container, so the agent's OWN exit code is never observed.
# We therefore return a Franky-set sentinel (124, the GNU `timeout(1)` convention) rather than
# the generic agent-error 1, so the CLI can map it to status="timeout" instead of agent_error.
CONTAINER_TIMEOUT_CODE = 124
CONTAINER_TIMEOUT_MSG = "franky: container timed out after {timeout}s"

# The non-root user baked into the image (Dockerfile: useradd --uid 1001 franky). The writable
# tmpfs mounts are owned by this uid/gid so the agent can actually clone, commit, and write its
# HOME config under a --read-only root. (The --tmpfs PATH:opts short form silently ignores
# mode=, but honours uid=/gid=, which is what makes the dirs writable by a non-root user.)
_RUN_UID = 1001
_RUN_GID = 1001
_HOME = "/home/franky"
CODEX_AUTH_HOME = f"{_HOME}/.codex"
_CODEX_AUTH_MAX_BYTES = 64 * 1024
# XDG_RUNTIME_DIR for the always-on rootless Docker daemon: it puts docker.sock + runtime state
# here (dockerd-rootless.sh defaults to /run/user/<uid>).
_XDG_RUNTIME = f"/run/user/{_RUN_UID}"
# HOME tmpfs size cap. HOME holds git/gh config AND the rootless Docker data root
# ($HOME/.local/share/docker - images/layers live here, in RAM). Capping it BELOW --memory
# means a runaway or huge `docker pull` hits a clean tmpfs ENOSPC (the pull fails) instead of
# OOM-killing dockerd and taking the whole task down with it.
_HOME_TMPFS_SIZE = "6g"

# Hardening flags applied to every run. No bind mounts, no docker socket: the repo is cloned
# INSIDE the container and so is everything the always-on rootless Docker daemon does, so the
# agent never touches the host filesystem. Root FS is read-only; only the tmpfs paths below are
# writable, all owned by the non-root run user so git/gh and rootless dockerd work under
# --read-only.
#
# WHY this profile is RELAXED vs a non-DinD container: franky runs a rootless Docker daemon
# inside this container (always on - see Dockerfile + franky-dind-entrypoint.sh) so a task can
# build/test repos whose suites need local infra (compose, testcontainers). Rootless dockerd
# needs a few specific, MINIMAL relaxations (validated empirically in the issue-12 spike); each
# is justified inline. This is NOT --privileged and NOT seccomp=unconfined.
_HARDENING = [
    "--rm",
    # newuidmap/newgidmap (the setuid helpers rootlesskit uses to map the subordinate uid/gid
    # range) need CAP_SETUID/CAP_SETGID. So we drop ALL caps then add back exactly those two -
    # not a blanket grant.
    "--cap-drop=ALL",
    "--cap-add=SETUID",
    "--cap-add=SETGID",
    # systempaths=unconfined UNMASKS /proc so the nested runc can mount a fresh procfs for inner
    # containers (Docker's default masked /proc paths are locked mounts the inner user namespace
    # cannot mount over -> "mounting proc: operation not permitted"). This ONLY lifts the /proc
    # path masking; it is NOT --privileged and NOT seccomp=unconfined.
    "--security-opt=systempaths=unconfined",
    # NOTE: --security-opt=no-new-privileges is DELIBERATELY ABSENT (it used to be here). It
    # blocks the setuid escalation newuidmap/newgidmap rely on, so rootless dockerd cannot set
    # up its uid map and refuses to start - even WITH CAP_SETUID/SETGID added. It is incompatible
    # with rootless DinD. The remaining boundary (cap-drop=ALL baseline, rootless user namespace,
    # --read-only root, the egress proxy cage, no host bind mount / no host docker socket) still
    # bounds the autonomous agent.
    # /dev/net/tun: rootlesskit's slirp4netns needs it to create the tap device for the nested
    # daemon's network. The daemon FAILS to start without it (spike Exp1).
    "--device",
    "/dev/net/tun",
    "--read-only",
    "--tmpfs",
    f"/work:exec,uid={_RUN_UID},gid={_RUN_GID}",
    "--tmpfs",
    f"{_HOME}:exec,uid={_RUN_UID},gid={_RUN_GID},size={_HOME_TMPFS_SIZE}",
    "--tmpfs",
    f"{_XDG_RUNTIME}:exec,uid={_RUN_UID},gid={_RUN_GID}",
    # rootlesskit + dockerd also write under /run and /tmp (root-owned bare tmpfs is fine; they
    # create their own subdirs).
    "--tmpfs",
    "/run:exec",
    "--tmpfs",
    "/tmp:exec",
    # dockerd + containerd + nested containers spawn many processes - 512 is too tight.
    "--pids-limit=2048",
    # tmpfs image storage is RAM; the cap sits above the 6g HOME tmpfs with headroom, and
    # --memory-swap=--memory forbids swap so a hostile workload cannot balloon past the cap.
    "--memory=8g",
    "--memory-swap=8g",
]

# The Squid proxy image (built from proxy/) and the port it listens on inside the net.
PROXY_IMAGE = "franky-proxy"
PROXY_PORT = 3128
# The GHCR namespace the public images live under. Overridable via FRANKY_GHCR_REPO so the
# publishing org can move without a code change (and for dev/testing against a fork). The
# default MUST match the org that hosts the public packages; the release workflow pushes to
# ${{ github.repository_owner }}, so this default tracks the repo's owning org.
DEFAULT_GHCR_REPO = "ghcr.io/vietlabs-work"
GHCR_REPO_VAR = "FRANKY_GHCR_REPO"
FRANKY_IMAGE_VAR = "FRANKY_IMAGE"
FRANKY_PROXY_IMAGE_VAR = "FRANKY_PROXY_IMAGE"

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
    "--tmpfs",
    f"/run:exec,uid={_PROXY_UID},gid={_PROXY_GID}",
    "--tmpfs",
    f"/var/log/squid:uid={_PROXY_UID},gid={_PROXY_GID}",
    "--tmpfs",
    f"/var/spool/squid:uid={_PROXY_UID},gid={_PROXY_GID}",
    "--pids-limit=512",
    "--memory=1g",
]

# Readiness poll for the proxy's HEALTHCHECK. 30 * 0.5s = 15s ceiling before we fail-closed.
PROXY_READY_POLLS = 30
PROXY_READY_INTERVAL = 0.5

# The mid-run steering mailbox (issue #72, `franky job attach`). HOME (/home/franky) is one of
# the writable tmpfs mounts under --read-only (see _HARDENING above), so a file written there by
# a host-side `docker exec` is a plain filesystem write - no bind mount, no new writable surface.
# The prompt (see prompt.py's _STEER_CONVENTION) tells the running agent to poll this exact path
# before each new sub-task; delivery here is guaranteed, incorporation is best-effort (the agent
# has to actually re-read the file at its next step).
STEER_FILE = "/home/franky/.franky-steer.md"


def build_docker_argv(
    image: str,
    passthrough_env: dict[str, str],
    inner_argv: list[str],
    name: str | None = None,
    network: str | None = None,
    proxy_url: str | None = None,
    profile_bundle: str | None = None,
    resume_wait: bool = False,
    auth_volume: str | None = None,
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

    When `profile_bundle` is given (a base64-encoded gzip tar of curated prose files), it
    is passed BY VALUE as FRANKY_PROFILE_BUNDLE.  This is correct and does not weaken the
    secret-by-name discipline: the bundle is NOT a credential - it is curated, secret-scrubbed
    operator content (Tier-1: static markdown/text only).  The entrypoint decodes and extracts
    it into HOME before exec-ing the engine.  No bind mount is added; the hardening flags are
    unchanged.

    When `resume_wait` is True (issue #71) the container is started in resume-wait mode via
    `-e FRANKY_RESUME_WAIT=1` (by-value, non-secret): the entrypoint blocks until the host
    docker-cp's the prior workspace into /work and touches the ready marker. The hardening flags
    are unchanged - resume uses only host-side docker cp/exec + this env flag, no bind mount.

    `auth_volume` is the fixed Codex subscription named volume selected by fail-closed config.
    It is never a caller-supplied path or bind mount.
    """
    container_name = name or f"franky-run-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", *_HARDENING, "--name", container_name]
    if auth_volume:
        argv += [
            "--mount",
            f"type=volume,src={auth_volume},dst={CODEX_AUTH_HOME}",
        ]
    if network:
        argv += ["--network", network]
    if proxy_url:
        # By-value (non-secret) proxy config. Both upper/lower case forms: tools disagree on
        # which they read. NO_PROXY keeps loopback direct. --dns 127.0.0.1 kills in-container
        # name resolution so the only way out is via the proxy.
        argv += [
            "-e",
            f"HTTP_PROXY={proxy_url}",
            "-e",
            f"HTTPS_PROXY={proxy_url}",
            "-e",
            f"http_proxy={proxy_url}",
            "-e",
            f"https_proxy={proxy_url}",
            "-e",
            "NO_PROXY=localhost,127.0.0.1",
            "-e",
            "no_proxy=localhost,127.0.0.1",
            "--dns",
            "127.0.0.1",
        ]
    if profile_bundle:
        # By-value (non-secret): curated prose, already secret-scrubbed on the host.
        # The entrypoint unpacks this before exec-ing the engine; see profile.py and
        # franky-dind-entrypoint.sh.
        argv += ["-e", f"{PROFILE_BUNDLE_VAR}={profile_bundle}"]
    if resume_wait:
        # By-value (non-secret): puts the entrypoint into resume-wait mode (issue #71).
        argv += ["-e", f"{snapshot.RESUME_WAIT_ENV}=1"]
    for key in passthrough_env:
        argv += ["-e", key]
    argv += [image, *inner_argv]
    return argv


def _codex_auth_argv(
    image: str,
    entrypoint: str,
    args: list[str],
    *,
    networkless: bool = True,
    user: str = f"{_RUN_UID}:{_RUN_GID}",
    readonly_volume: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    """The one hardened shape used by trusted fixed-volume auth helpers."""
    mount = f"type=volume,src={CODEX_AUTH_VOLUME},dst={CODEX_AUTH_HOME}"
    if readonly_volume:
        mount += ",readonly"
    return [
        "docker",
        "run",
        "--rm",
        *(["--network", "none"] if networkless else []),
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--pids-limit=128",
        "--memory=512m",
        "--memory-swap=512m",
        "--user",
        user,
        *(extra or []),
        "--mount",
        mount,
        "--entrypoint",
        entrypoint,
        image,
        *args,
    ]


def build_codex_auth_scrub_argv(image: str, *, require_auth: bool) -> list[str]:
    """Build a networkless helper that keeps only auth.json in persistent CODEX_HOME."""
    auth_file = f"{CODEX_AUTH_HOME}/auth.json"
    check = (
        f" && test ! -L {auth_file} && test -f {auth_file} && test -s {auth_file}"
        f" && test $(wc -c < {auth_file}) -le {_CODEX_AUTH_MAX_BYTES}"
    )
    if not require_auth:
        check = ""
    command = (
        f"find {CODEX_AUTH_HOME} -mindepth 1 ! -path {CODEX_AUTH_HOME}/auth.json -delete{check}"
    )
    return _codex_auth_argv(image, "sh", ["-c", command])


def _codex_auth_volume_exists(runner) -> bool:
    proc = _run(runner, ["docker", "volume", "inspect", CODEX_AUTH_VOLUME])
    return proc is not None and getattr(proc, "returncode", 1) == 0


def codex_auth_ready(image: str, runner=subprocess.run) -> bool:
    """Scrub persistent state and require a bounded, valid Codex credential."""
    return _codex_auth_state(image, runner) is not None


def _codex_auth_state(image: str, runner=subprocess.run) -> list[str] | None:
    """Return auth strings for in-memory redaction, never logging or persisting them."""
    if not _codex_auth_volume_exists(runner):
        return None
    proc = _run(runner, build_codex_auth_scrub_argv(image, require_auth=True))
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    auth_file = f"{CODEX_AUTH_HOME}/auth.json"
    read = _run(
        runner,
        _codex_auth_argv(image, "cat", [auth_file], readonly_volume=True),
    )
    raw = getattr(read, "stdout", "") if read is not None else ""
    if (
        getattr(read, "returncode", 1) != 0
        or not raw
        or len(raw.encode("utf-8", errors="replace")) > _CODEX_AUTH_MAX_BYTES
    ):
        return None
    try:
        payload = json.loads(raw)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    status = _run(
        runner,
        _codex_auth_argv(image, "codex", ["login", "status"], readonly_volume=True),
    )
    if status is None or getattr(status, "returncode", 1) != 0:
        return None

    secrets: list[str] = []
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and len(value) >= 8:
            secrets.append(value)
    return secrets


def codex_auth_status(image: str, runner=subprocess.run) -> bool:
    """Ask Codex to recognize the scrubbed credential without granting network access."""
    return _codex_auth_state(image, runner) is not None


def codex_auth_login(image: str, runner=subprocess.run) -> bool:
    """Run trusted browserless Codex login, persisting only file-backed credentials."""
    create = _run(runner, ["docker", "volume", "create", CODEX_AUTH_VOLUME])
    if create is None or getattr(create, "returncode", 1) != 0:
        return False
    init_argv = _codex_auth_argv(
        image,
        "chown",
        [f"{_RUN_UID}:{_RUN_GID}", CODEX_AUTH_HOME],
        user="0:0",
        extra=["--cap-add=CHOWN"],
    )
    init = _run(runner, init_argv)
    scrub = _run(runner, build_codex_auth_scrub_argv(image, require_auth=False))
    if any(p is None or getattr(p, "returncode", 1) != 0 for p in (init, scrub)):
        return False
    login_argv = _codex_auth_argv(
        image,
        "codex",
        ["-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"],
        networkless=False,
        extra=["--tmpfs", f"/tmp:uid={_RUN_UID},gid={_RUN_GID}"],
    )
    try:
        proc = runner(login_argv)
    except OSError:
        return False
    if getattr(proc, "returncode", 1) != 0:
        return False
    return codex_auth_ready(image, runner)


def codex_auth_logout(runner=subprocess.run) -> bool:
    """Remove the fixed auth volume; missing state is already logged out."""
    proc = _run(runner, ["docker", "volume", "rm", CODEX_AUTH_VOLUME])
    if proc is None:
        return False
    return (
        getattr(proc, "returncode", 1) == 0
        or "no such volume" in (getattr(proc, "stderr", "") or "").lower()
    )


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
        "docker",
        "run",
        "-d",
        *_PROXY_HARDENING,
        "--name",
        proxy_name,
        "-e",
        f"FRANKY_ALLOWED_DOMAINS={','.join(allowed_domains)}",
        proxy_image,
    ]


def proxy_url(proxy_name: str) -> str:
    """The URL the task container uses to reach the proxy, by container name on the shared
    internal network (docker's embedded DNS resolves the name)."""
    return f"http://{proxy_name}:{PROXY_PORT}"


def run_names(run_id: str) -> tuple[str, str, str]:
    """Derive the (network, proxy, task) container names from a run id.

    Deterministic so the CLI can generate one job id up front, record these names in the run
    registry (issue #63), and later target the SAME container/net/proxy for `job status`/`kill`
    - the run id IS the handle. The `franky-net-`/`franky-proxy-`/`franky-run-` prefixes are the
    stable contract other tooling (and the tests) match on.
    """
    return (f"franky-net-{run_id}", f"franky-proxy-{run_id}", f"franky-run-{run_id}")


def container_running(name: str, runner=subprocess.run) -> bool:
    """True iff a container named `name` exists AND is currently running (`docker inspect`).

    For `job status`: distinguishes a still-alive (possibly stuck) run from one that is gone.
    Never raises - a docker error or a missing container reads as not-running."""
    try:
        proc = runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return (getattr(proc, "stdout", "") or "").strip() == "true"


def build_steer_argv(container: str, steer_file: str = STEER_FILE) -> list[str]:
    """`docker exec -i <container> tee -a <steer_file>` - the mid-run steering delivery argv.

    Pure - no docker invoked. The correction message is delivered on STDIN (via `input=` at
    the call site), NEVER on the argv or interpolated into a shell string, so an operator
    message can never be parsed as a shell command inside the container. `tee -a` both creates
    the file (if absent) and appends (if the agent has not yet consumed a prior correction),
    and the file it creates is owned by the exec user (uid 1001, the same non-root user the
    task runs as - see _RUN_UID), so the agent can read (and delete) it.
    """
    return ["docker", "exec", "-i", container, "tee", "-a", steer_file]


def deliver_steer(
    container: str,
    message: str,
    runner=subprocess.run,
    *,
    timeout: float = 10.0,
) -> bool:
    """Deliver `message` into `container`'s steer-file mailbox. Returns True iff the exec
    succeeded (exit 0). Never raises - any failure (docker gone, container not running, a
    hung exec, docker not installed) degrades to False so `job attach` can report a clean
    typed error instead of a traceback.

    WHY the captured stdout/stderr is discarded: `tee` echoes whatever it was fed straight back
    to its own stdout, so `proc.stdout` here would just be the operator's message a second time.
    Returning or logging it would create a second, unredacted copy of operator-supplied prose
    outside the normal redact-before-print path - so we deliberately capture-and-drop it rather
    than surface it anywhere.
    """
    try:
        proc = runner(
            build_steer_argv(container),
            input=message,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


def reap_run(run_id: str, runner=subprocess.run) -> bool:
    """Force-remove a run's task container and reap its proxy sidecar + internal network.

    For `franky job kill`: tears down the whole run topology by id, in the same task -> proxy
    -> net order as run_in_container's own teardown. Returns True iff the task container (the
    one holding the injected creds) was removed; proxy/net are lower-severity resource leaks.
    Never raises."""
    net, proxy, task = run_names(run_id)
    task_reaped = _reap(task, runner)
    _reap(proxy, runner)
    _reap_network(net, runner)
    return task_reaped


def capture_diagnostics(
    task: str,
    proxy: str,
    transcript: str,
    runner,
    *,
    task_launched: bool,
    proxy_launched: bool,
    secrets: list[str],
    timeout: float = 3.0,
) -> dict:
    """Best-effort runtime diagnostics, captured host-side JUST BEFORE the task/proxy
    containers are reaped (issue #69).

    WHY this exists: once `_reap` fires the container is gone and `franky job diagnose` (or a
    human) is left analyzing prose alone. A handful of hard signals - did the task OOM, did the
    nested rootless Docker daemon come up, did the egress proxy deny anything, did a tmpfs fill
    up - are cheap to grab with a couple of read-only `docker inspect`/`docker exec` calls RIGHT
    BEFORE teardown, while the containers still exist. This function is that grab.

    WHY it can never regress the run: every docker call below is wrapped in its OWN
    try/except Exception with `timeout` (default 3s) so one slow/failing capture can never
    delay or wedge the reap that follows, and a capture failure never raises - it just omits
    that field. The caller (`run_in_container`) treats the whole call as opt-in-and-optional:
    a raise here must never change the run's returncode/output.

    WHY no new secret surface: this reads ONLY container state (exit code / OOM flag / status)
    and the proxy's OWN access log (which contains destination hosts, never request bodies or
    creds - Squid does blind HTTPS CONNECT). Every host string extracted from the access log is
    passed through `redact(host, secrets)` before being stored, so a secret that happened to
    leak into a hostname (e.g. via DNS-exfil attempt) is scrubbed the same way the rest of
    Franky's output is. The `transcript` passed here MAY be RAW - on the `run_in_container` path
    this runs in the `finally` BEFORE the closing `redact()` at return, so the string is not yet
    scrubbed. That is fine BECAUSE we deliberately extract only True/False/None booleans from it
    and never persist any transcript substring - there is no path for a secret in the transcript
    to reach the returned dict.

    Returns a dict containing only the fields it successfully captured (a fresh dict each call,
    never a superset contract) - see the module docstring / issue #69 for the field list.
    """
    diag: dict = {}

    if task_launched:
        try:
            proc = runner(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Status}}",
                    task,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            parts = (getattr(proc, "stdout", "") or "").strip().split("|")
            if len(parts) == 3:
                try:
                    diag["task_exit_code"] = int(parts[0])
                except ValueError:
                    pass  # not parseable - omit rather than store a bogus value
                diag["oom_killed"] = parts[1].strip().lower() == "true"
                state = parts[2].strip()
                if state:
                    diag["task_state"] = state
        except Exception:
            pass

    if proxy_launched:
        try:
            proc = runner(
                ["docker", "exec", proxy, "cat", "/run/squid-access.log"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            counts: dict[str, int] = {}
            for line in (getattr(proc, "stdout", "") or "").splitlines():
                if "TCP_DENIED" not in line:
                    continue
                tokens = line.split()
                host = None
                # Egress is HTTPS-only blind CONNECT, so a denied request always carries a literal
                # "CONNECT" method token and its target is the very next token (`host:443`). A
                # line with no CONNECT token is not a proxied-host denial we can trust (a plain
                # http:// request URL would mangle to garbage), so skip it rather than guess.
                if "CONNECT" in tokens:
                    idx = tokens.index("CONNECT")
                    if idx + 1 < len(tokens):
                        host = tokens[idx + 1]
                if not host:
                    continue
                if ":" in host:
                    host = host.rsplit(":", 1)[0]
                host = redact(host, secrets)
                counts[host] = counts.get(host, 0) + 1
            # Assigned only inside this try (i.e. only when the exec itself succeeded) - an
            # empty list + zero count IS the correct signal for "no denials", distinct from
            # "we could not even reach the proxy to check".
            diag["egress_denied"] = [{"host": h, "count": c} for h, c in sorted(counts.items())]
            diag["proxy_denied_count"] = sum(counts.values())
        except Exception:
            pass

    # Booleans only, extracted from the transcript at capture time - never the raw transcript
    # text itself (see the WHY-no-secret-surface note above). One try/except around BOTH scans
    # for symmetry: this is a pure string search with no I/O so it cannot really fail, but if it
    # somehow did we leave both keys at their safe defaults (dind_ready=None, tmpfs_full=False)
    # rather than one absent and one defaulted.
    try:
        if "rootless dockerd did not become ready" in transcript:
            diag["dind_ready"] = False
        elif "rootless dockerd ready" in transcript:
            diag["dind_ready"] = True
        else:
            diag["dind_ready"] = None
        diag["tmpfs_full"] = "No space left on device" in transcript or "ENOSPC" in transcript
    except Exception:
        diag["dind_ready"] = None
        diag["tmpfs_full"] = False

    return diag


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
    output. Returns the proc (or None on OSError) so the caller can check returncode. Codex's
    auth reader deliberately captures credential JSON for in-memory redaction and discards it;
    no call here prints captured output. We do NOT pass the task's child_env."""
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
    proxy_image: str = PROXY_IMAGE,
    profile_bundle: str | None = None,
    progress=None,
    popen=subprocess.Popen,
    run_id: str | None = None,
    diagnostics_sink: dict | None = None,
    snapshot_sink: dict | None = None,
    resume_workspace: str | None = None,
) -> tuple[int, str]:
    """Run the inner engine in a hardened, egress-controlled container; return
    (returncode, redacted_output).

    `diagnostics_sink`, when given a dict, is populated (via `.update`) with best-effort
    runtime diagnostics (issue #69) captured JUST BEFORE teardown - see `capture_diagnostics`.
    Opt-in: None (the default) is byte-identical to the pre-#69 behavior for every existing
    caller/test. A capture failure never changes the returned (code, output).

    `snapshot_sink` (issue #71), when given a dict with a `"dest"` path, opts into capturing the
    task's `/work` workspace ON TIMEOUT: `extract_workspace` runs in the `finally` BEFORE the
    reap (the only step that needs the container alive), then `finalize_snapshot` (scrub + verify
    + pack) runs AFTER the reap so teardown is never delayed by it. On success the sink gains a
    `"snapshot_path"` key. Any failure is swallowed - a snapshot can never regress the run.

    `resume_workspace` (issue #71), when set to a host tar path, RESTORES that workspace into the
    freshly launched container between launch and the engine run. This forces the streaming
    (popen) path even without `progress`, because the restore must happen after the container is
    up but before it completes; a no-op progress callback is used when none was given.

    Topology: an --internal network (no internet route) hosts a Squid proxy (default-deny
    allowlist) and the task container. The task's HTTP(S)_PROXY points at the proxy and its
    own DNS is killed, so its only path out is through the allowlist. We refuse to start the
    task until the proxy is confirmed healthy (fail-closed). A `finally` ALWAYS tears down
    whatever was created, in order task -> proxy -> net.

    The child env is os.environ + cfg.passthrough_env, so the `-e KEY` (name-only) flags
    resolve to real values inside the task without those values ever hitting the argv. The
    PROXY gets NO cred env. `runner`/`sleeper` are injectable so tests run fakes and never
    touch real docker. All returned output is scrubbed of secret values.

    When `progress` is given, it is called with each redacted output line as it arrives so
    the caller can stream live feedback to stderr. The returned output still contains the
    full accumulated transcript (redacted) for end-of-run processing. `popen` is the
    injectable Popen-compatible callable used by the streaming path (tests pass a fake).
    """
    # Names derive from run_id so the CLI can register them up front and later target the same
    # container/net/proxy for `job status`/`kill` (issue #63). None -> a fresh id (unchanged
    # behavior for callers that do not track a job).
    net, proxy, task = run_names(run_id or uuid.uuid4().hex[:12])

    provider_env = dict(cfg.passthrough_env)
    if cfg.auth_volume:
        provider_env[CODEX_SUBSCRIPTION_VAR] = "1"
    allowed = egress.build_allowlist(cfg.engine, provider_env, cfg.extra_allowed_domains)

    child_env = dict(os.environ if env is None else env)
    child_env.update(cfg.passthrough_env)

    secrets = cfg.secret_values()

    net_created = False
    proxy_launched = False
    task_launched = False  # an OSError on spawn means docker never created the container
    code, output = 1, ""

    try:
        if cfg.auth_volume:
            auth_secrets = _codex_auth_state(image, runner)
            if auth_secrets is None:
                raise _AbortRun(
                    "franky: Codex subscription login is missing or invalid - "
                    "run `franky auth login codex`"
                )
            secrets.extend(auth_secrets)

        # 1. Internal network. If this fails there is nothing to reap (it was not created).
        if getattr(_run(runner, build_network_argv(net)), "returncode", 1) != 0:
            raise _AbortRun("franky: could not create egress network - refusing to run")
        net_created = True

        # 2. Proxy container (detached). On failure: teardown runs in `finally`.
        if (
            getattr(_run(runner, build_proxy_argv(proxy_image, proxy, allowed)), "returncode", 1)
            != 0
        ):
            raise _AbortRun("franky: could not start egress proxy - refusing to run")
        proxy_launched = True

        # 3. Attach the proxy to the internal net (the task joins via --network on run).
        if getattr(_run(runner, build_network_connect_argv(net, proxy)), "returncode", 1) != 0:
            raise _AbortRun(
                "franky: could not attach egress proxy to the network - refusing to run"
            )

        # 4. Fail-closed gate: NEVER run the task without a confirmed-healthy proxy.
        if not _wait_proxy_ready(proxy, runner, sleeper):
            raise _AbortRun(
                "franky: egress proxy did not become ready - refusing to run (fail-closed)"
            )

        # 5. The task container: on the internal net, all traffic forced through the proxy.
        # Resume (issue #71) forces the streaming (popen) path even without a progress callback:
        # the workspace restore must run AFTER the container is up but BEFORE it completes, which
        # only the popen path exposes. A no-op progress stand-in keeps the streaming loop working
        # when the caller passed none.
        resuming = resume_workspace is not None
        effective_progress = progress
        if resuming and effective_progress is None:
            effective_progress = lambda _line: None  # noqa: E731 - tiny no-op for the stream loop
        argv = build_docker_argv(
            image,
            cfg.passthrough_env,
            inner_argv,
            name=task,
            network=net,
            proxy_url=proxy_url(proxy),
            profile_bundle=profile_bundle,
            resume_wait=resuming,
            auth_volume=cfg.auth_volume,
        )
        if effective_progress is not None:
            # Streaming path: iterate stdout/stderr line by line, redact per line, call
            # progress(), and accumulate raw lines for the end-of-run full-buffer redact.
            # WHY raw accumulation: a secret that spans a line boundary (unlikely for JSONL
            # but theoretically possible) is caught by the final redact() call below.
            try:
                proc = popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    env=child_env,
                )
                task_launched = True
                # Restore the prior workspace into the just-launched container (issue #71) BEFORE
                # draining stdout: the container is waiting on the ready marker in resume-wait
                # mode. A False result is fine to proceed on - the entrypoint will exit 75 quickly
                # (refusing to run on an empty /work) and the run classifies as agent_error; do
                # NOT abort the stream, just drain it.
                if resume_workspace is not None:
                    snapshot.restore_into_container(
                        task, Path(resume_workspace), runner, sleeper=sleeper
                    )
                raw_lines: list[str] = []
                t_start = time.monotonic()
                timed_out = False
                # HARD wall-clock watchdog (never-hang): the per-line elapsed check below only
                # fires when a line ARRIVES, so a container that produces NO output (engine wedged,
                # or a resume-wait container that never gets its marker) would block `for line in
                # proc.stdout` forever and the timeout would never trigger. A background timer
                # kills the process at the deadline regardless of output; killing it closes the
                # pipe, so the read loop ends and we classify the run as a timeout. Cancelled on
                # normal completion. This matters for `job resume` and `build -v` especially.
                watchdog_hit = threading.Event()

                def _on_deadline() -> None:
                    watchdog_hit.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass

                watchdog = threading.Timer(timeout, _on_deadline)
                watchdog.daemon = True
                watchdog.start()
                try:
                    for line in proc.stdout:
                        # Belt-and-suspenders: the per-line elapsed check still catches a slow
                        # trickle of output between deadline checks; the watchdog above catches a
                        # total silence.
                        if watchdog_hit.is_set() or time.monotonic() - t_start >= timeout:
                            proc.kill()
                            timed_out = True
                            break
                        effective_progress(redact(line, secrets))
                        raw_lines.append(line)
                finally:
                    watchdog.cancel()
                    proc.stdout.close()
                    remaining = max(1.0, timeout - (time.monotonic() - t_start))
                    try:
                        proc.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if timed_out or watchdog_hit.is_set():
                    code, output = (
                        CONTAINER_TIMEOUT_CODE,
                        CONTAINER_TIMEOUT_MSG.format(timeout=timeout),
                    )
                else:
                    code = proc.returncode
                    output = "".join(raw_lines)
            except OSError as exc:
                code, output = 1, f"franky: could not launch docker ({exc})"
        else:
            # Blocking path (original): capture all output then return.
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
                code, output = (
                    CONTAINER_TIMEOUT_CODE,
                    CONTAINER_TIMEOUT_MSG.format(timeout=timeout),
                )
            except OSError as exc:
                code, output = 1, f"franky: could not launch docker ({exc})"
    except _AbortRun as abort:
        # A pre-task step failed. code/output set here; teardown still runs in `finally` and
        # its warnings append to THIS output, which the single return below surfaces.
        code, output = 1, abort.message
    finally:
        # Capture-before-reap diagnostics (issue #69): MUST run before the reaps below - once
        # `_reap` fires the container is gone and there is nothing left to inspect. Opt-in
        # (sink is None for every pre-#69 caller) and fully isolated: any exception here is
        # swallowed so a capture failure can never delay or wedge the teardown that follows.
        if diagnostics_sink is not None:
            try:
                diagnostics_sink.update(
                    capture_diagnostics(
                        task,
                        proxy,
                        output,
                        runner,
                        task_launched=task_launched,
                        proxy_launched=proxy_launched,
                        secrets=secrets,
                        timeout=3.0,
                    )
                )
            except Exception:
                pass
        # Snapshot-on-timeout (issue #71): capture /work so `franky job resume` can continue this
        # run later. The EXTRACT must run before the reap (only the live container has /work), so
        # it happens here - but ONLY the extract, capped, so the reap is never delayed by the
        # slower scrub/verify/pack. The finalize runs AFTER the reaps below. Opt-in (sink None for
        # every non-resume caller) and fully isolated: any failure is swallowed.
        _snapshot_tmp = None
        if snapshot_sink is not None and task_launched and code == CONTAINER_TIMEOUT_CODE:
            try:
                import tempfile

                _snapshot_tmp = tempfile.mkdtemp(prefix="franky-snapshot-")
                if not snapshot.extract_workspace(task, _snapshot_tmp, runner):
                    _snapshot_tmp = None
            except Exception:
                _snapshot_tmp = None
        # Best-effort teardown, ALWAYS, in order task -> proxy -> net. A reap FAILURE on the
        # TASK container is surfaced at full severity because it holds the injected creds. A
        # proxy/net reap failure is a lower-severity resource leak (the proxy holds NO creds).
        if task_launched and not _reap(task, runner):
            output += f"\nfranky: WARNING container {task} may not have been removed - check `docker ps -a`"
        if proxy_launched and not _reap(proxy, runner):
            output += f"\nfranky: WARNING egress proxy {proxy} may not have been removed (resource leak, no creds) - check `docker ps -a`"
        if net_created and not _reap_network(net, runner):
            output += f"\nfranky: WARNING egress network {net} may not have been removed (resource leak) - check `docker network ls`"
        # Finalize the snapshot AFTER the reap (scrub + fail-closed verify + pack), so the
        # container is already gone and this potentially-slower step never delays teardown.
        if _snapshot_tmp is not None:
            try:
                path = snapshot.finalize_snapshot(
                    Path(_snapshot_tmp), Path(snapshot_sink["dest"]), secrets, runner
                )
                if path:
                    snapshot_sink["snapshot_path"] = path
            except Exception:
                pass

    return code, redact(output, secrets)


def image_exists(image: str = "franky", runner=subprocess.run) -> bool:
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


def resolve_image(env: dict, var: str = FRANKY_IMAGE_VAR, name: str = "franky") -> str:
    """Return the image ref to use. If the per-image env var is set and truthy, return it
    (dev override). Otherwise return the version-pinned GHCR ref, with the GHCR namespace
    taken from FRANKY_GHCR_REPO when set, else DEFAULT_GHCR_REPO. NEVER resolves to :latest."""
    override = env.get(var)
    if override:
        return override
    repo = env.get(GHCR_REPO_VAR) or DEFAULT_GHCR_REPO
    return f"{repo}/{name}:{franky_version()}"


def ensure_image_available(image: str, runner=subprocess.run) -> tuple[bool, str]:
    """Ensure `image` is available locally, pulling if needed.
    Returns (True, "") on success.
    Returns (False, "no-docker") if docker is not available (OSError).
    Returns (False, "auth") if the pull failed with an auth/credentials error.
    Returns (False, "pull-failed") for any other pull failure.
    Never raises."""
    if image_exists(image, runner):
        return True, ""
    try:
        proc = runner(["docker", "pull", image], capture_output=True, text=True)
    except OSError:
        return False, "no-docker"
    if proc.returncode == 0:
        return True, ""
    combined = ((proc.stdout or "") + (proc.stderr or "")).lower()
    if any(
        kw in combined for kw in ("denied", "unauthorized", "authentication", "forbidden", "401")
    ):
        return False, "auth"
    return False, "pull-failed"
