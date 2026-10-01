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
confirmed to deny an off-allowlist CONNECT first (fail-closed). All docker argv is built here;
the allowlist POLICY lives in egress.py.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import egress, franky_version, snapshot
from .config import DEFAULT_DISK_MB, DEFAULT_MEMORY_MB, redact
from .engine import CODEX_SUBSCRIPTION_VAR, ENGINES
from .profile import CONTAINER_HOME, PROFILE_WAIT_VAR
from .security import DEFAULT_SECCOMP, TASK_SECCOMP, SecurityPolicyError, select_task_apparmor
from .transcript import CHUNK_SIZE, MAX_EVENT_CHARS, Redactor, Transcript, chunks

# Long agent runs: a full clone-build-test-PR cycle can take many minutes. 30 min cap.
FALLBACK_TIMEOUT_SECS = 1800

# On a timeout Franky kills the container, so the agent's OWN exit code is never observed.
# We therefore return a Franky-set sentinel (124, the GNU `timeout(1)` convention) rather than
# the generic agent-error 1, so the CLI can map it to status="timeout" instead of agent_error.
CONTAINER_TIMEOUT_CODE = 124
CONTAINER_TIMEOUT_MSG = "franky: container timed out after {timeout}s"

# The image owns build directories as uid 1001. Docker volume copy-up preserves ownership.
# Small runtime tmpfs mounts need explicit uid/gid to remain writable by the non-root agent.
_RUN_UID = 1001
_RUN_GID = 1001
_HOME = "/home/franky"
CODEX_AUTH_HOME = f"{_HOME}/.codex"
_CODEX_AUTH_MAX_BYTES = 64 * 1024
# XDG_RUNTIME_DIR for the always-on rootless Docker daemon: it puts docker.sock + runtime state
# here (dockerd-rootless.sh defaults to /run/user/<uid>).
_XDG_RUNTIME = f"/run/user/{_RUN_UID}"
# Hardening flags applied to every run. No bind mounts, no docker socket: the repo is cloned
# INSIDE the container and so is everything the always-on rootless Docker daemon does, so the
# agent cannot access host paths. Docker creates private anonymous volumes for build data.
# --rm and the forced reaper remove them. Disk blocks can retain data after removal, so this
# is disposable storage, not secure erasure. The image owns /work and HOME as uid 1001.
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
    f"--security-opt=seccomp={TASK_SECCOMP}",
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
    "--mount",
    "type=volume,dst=/work",
    "--mount",
    f"type=volume,dst={_HOME}",
    "--mount",
    "type=volume,dst=/tmp",
    "--tmpfs",
    f"{_XDG_RUNTIME}:exec,uid={_RUN_UID},gid={_RUN_GID},size=16m",
    "--tmpfs",
    "/run:exec,size=16m",
    # dockerd + containerd + nested containers spawn many processes - 512 is too tight.
    "--pids-limit=2048",
]

# The Squid proxy image (built from proxy/) and the port it listens on inside the net.
PROXY_IMAGE = "franky-proxy"
PROXY_PORT = 3128
# Diagnostic counts cover only complete records in this bounded recent log tail.
_PROXY_LOG_BYTES = 64 * 1024
# The GHCR namespace the public images live under. Overridable via FRANKY_GHCR_REPO so the
# publishing org can move without a code change (and for dev/testing against a fork). The
# default MUST match the org that hosts the public packages; the release workflow pushes to
# ${{ github.repository_owner }}, so this default tracks the repo's owning org.
DEFAULT_GHCR_REPO = "ghcr.io/vietlabs-work"
GHCR_REPO_VAR = "FRANKY_GHCR_REPO"
FRANKY_IMAGE_VAR = "FRANKY_IMAGE"
FRANKY_PROXY_IMAGE_VAR = "FRANKY_PROXY_IMAGE"

# Hardening for the proxy container. Mirrors _HARDENING but the writable tmpfs dirs are
# squid's, not the agent's: the image runs with fixed uid/gid 13 (Debian `proxy`, or a numeric
# Alpine user). It writes its rendered config, pid, and logs there under a --read-only
# root. A bare --tmpfs mounts root-owned and `mode=` is silently ignored by the short form
# (see _HARDENING note), so we MUST pin uid=/gid= to squid's user or it cannot write and
# crashes on start. CONNECT tunneling does not require a large response cache.
_PROXY_UID = 13
_PROXY_GID = 13
_PROXY_HARDENING = [
    "--rm",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    f"--security-opt=seccomp={DEFAULT_SECCOMP}",
    "--read-only",
    "--tmpfs",
    f"/run:exec,uid={_PROXY_UID},gid={_PROXY_GID},size=16m",
    "--tmpfs",
    f"/var/log/squid:uid={_PROXY_UID},gid={_PROXY_GID},size=1m",
    "--tmpfs",
    f"/var/spool/squid:uid={_PROXY_UID},gid={_PROXY_GID},size=1m",
    "--pids-limit=512",
    "--memory=128m",
    "--memory-swap=128m",
    # Squid sizes descriptor tables from this limit. Docker's million-fd default
    # OOMs before readiness at 128 MiB; one task needs far fewer connections.
    "--ulimit=nofile=4096:4096",
]

# The startup denial probe polls for at most 15 seconds before it fails closed.
PROXY_READY_POLLS = 30
PROXY_READY_INTERVAL = 0.5
PROXY_READY_TIMEOUT = 15.0
PROXY_PROBE_TIMEOUT = 2.0
# A pull that hangs (registry or network stall) must end the run with an error, not hold it
# forever: `docker pull` has no timeout of its own. 15 min covers a ~1.8 GB image at ~2 MB/s.
PULL_TIMEOUT_SECS = 900

# The mid-run steering mailbox (issue #72, `franky job attach`). HOME (/home/franky) is one of
# the writable volumes under --read-only (see _HARDENING above), so a file written there by
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
    profile_wait: bool = False,
    resume_wait: bool = False,
    auth_volume: str | None = None,
    memory_mb: int = DEFAULT_MEMORY_MB,
    disk_mb: int = DEFAULT_DISK_MB,
    apparmor_profile: str | None = None,
    session_hold: str = "",
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

    When `profile_wait` is True the container starts in profile-wait mode via
    `-e FRANKY_PROFILE_WAIT=1` (by-value, non-secret - it carries policy, not content): the
    entrypoint blocks until the host has streamed the operator's bundle into HOME and touched
    the ready marker (see `deliver_profile`).  The bundle itself NEVER reaches the argv - a
    swept setup runs to ~1 MB, which no single `-e` value can carry on Linux (128 KB
    MAX_ARG_STRLEN), and keeping it off the argv also keeps it out of `ps`.

    When `resume_wait` is True (issue #71) the container is started in resume-wait mode via
    `-e FRANKY_RESUME_WAIT=1` (by-value, non-secret): the entrypoint blocks until the host
    docker-cp's the prior workspace into /work and touches the ready marker. The hardening flags
    are unchanged - resume uses only host-side docker cp/exec + this env flag, no bind mount.

    `auth_volume` is the fixed Codex subscription named volume selected by fail-closed config.
    It is never a caller-supplied path or bind mount.

    When `session_hold` (a per-run nonce) is set the container starts with
    `-e FRANKY_SESSION_HOLD=<nonce>` (by-value, non-secret): after a clean engine exit the
    entrypoint keeps the --rm container alive, capped, until the host has copied the engine
    session out (see run_in_container).
    """
    container_name = name or f"franky-run-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        *_HARDENING,
        *([f"--security-opt=apparmor={apparmor_profile}"] if apparmor_profile is not None else []),
        f"--memory={memory_mb}m",
        f"--memory-swap={memory_mb}m",
        "--name",
        container_name,
        "-e",
        f"FRANKY_DISK_MB={disk_mb}",
        # Attached stdout still streams; Docker must not persist raw pre-redaction bytes.
        "--log-driver=none",
    ]
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
    if profile_wait:
        # By-value (non-secret): puts the entrypoint into profile-wait mode. The content
        # arrives later over `docker exec -i` stdin; see deliver_profile + the entrypoint.
        argv += ["-e", f"{PROFILE_WAIT_VAR}=1"]
    if resume_wait:
        # By-value (non-secret): puts the entrypoint into resume-wait mode (issue #71).
        argv += ["-e", f"{snapshot.RESUME_WAIT_ENV}=1"]
    if session_hold:
        # By-value (non-secret): the entrypoint holds after a clean exit for the session copy.
        argv += ["-e", f"{snapshot.SESSION_HOLD_ENV}={session_hold}"]
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
    auth_volume: str,
) -> list[str]:
    """The one hardened shape used by trusted fixed-volume auth helpers."""
    mount = f"type=volume,src={auth_volume},dst={CODEX_AUTH_HOME}"
    if readonly_volume:
        mount += ",readonly"
    return [
        "docker",
        "run",
        "--rm",
        *(["--network", "none"] if networkless else []),
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={DEFAULT_SECCOMP}",
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


def build_codex_auth_scrub_argv(image: str, *, require_auth: bool, auth_volume: str) -> list[str]:
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
    return _codex_auth_argv(image, "sh", ["-c", command], auth_volume=auth_volume)


def _codex_auth_volume_exists(runner, *, auth_volume: str) -> bool:
    proc = _run(runner, ["docker", "volume", "inspect", auth_volume])
    return proc is not None and getattr(proc, "returncode", 1) == 0


def codex_auth_ready(image: str, runner=subprocess.run, *, auth_volume: str) -> bool:
    """Scrub persistent state and require a bounded, valid Codex credential."""
    return _codex_auth_state(image, runner, auth_volume=auth_volume) is not None


def _codex_auth_state(image: str, runner=subprocess.run, *, auth_volume: str) -> list[str] | None:
    """Return auth strings for in-memory redaction, never logging or persisting them."""
    if not _codex_auth_volume_exists(runner, auth_volume=auth_volume):
        return None
    scrub_argv = build_codex_auth_scrub_argv(image, require_auth=True, auth_volume=auth_volume)
    proc = _run(runner, scrub_argv)
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    auth_file = f"{CODEX_AUTH_HOME}/auth.json"
    read = _run(
        runner,
        _codex_auth_argv(image, "cat", [auth_file], readonly_volume=True, auth_volume=auth_volume),
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
        _codex_auth_argv(
            image, "codex", ["login", "status"], readonly_volume=True, auth_volume=auth_volume
        ),
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


def codex_auth_status(image: str, runner=subprocess.run, *, auth_volume: str) -> bool:
    """Ask Codex to recognize the scrubbed credential without granting network access."""
    return _codex_auth_state(image, runner, auth_volume=auth_volume) is not None


def codex_auth_login(image: str, runner=subprocess.run, *, auth_volume: str) -> bool:
    """Run trusted browserless Codex login, persisting only file-backed credentials."""
    create = _run(runner, ["docker", "volume", "create", auth_volume])
    if create is None or getattr(create, "returncode", 1) != 0:
        return False
    init_argv = _codex_auth_argv(
        image,
        "chown",
        [f"{_RUN_UID}:{_RUN_GID}", CODEX_AUTH_HOME],
        user="0:0",
        extra=["--cap-add=CHOWN"],
        auth_volume=auth_volume,
    )
    init = _run(runner, init_argv)
    scrub = _run(
        runner, build_codex_auth_scrub_argv(image, require_auth=False, auth_volume=auth_volume)
    )
    if any(p is None or getattr(p, "returncode", 1) != 0 for p in (init, scrub)):
        return False
    login_argv = _codex_auth_argv(
        image,
        "codex",
        ["-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"],
        networkless=False,
        extra=["--tmpfs", f"/tmp:uid={_RUN_UID},gid={_RUN_GID}"],
        auth_volume=auth_volume,
    )
    try:
        proc = runner(login_argv)
    except OSError:
        return False
    if getattr(proc, "returncode", 1) != 0:
        return False
    return codex_auth_ready(image, runner, auth_volume=auth_volume)


def codex_auth_logout(runner=subprocess.run, *, auth_volume: str) -> bool:
    """Remove the fixed auth volume; missing state is already logged out."""
    proc = _run(runner, ["docker", "volume", "rm", auth_volume])
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


def container_running(name: str, runner=subprocess.run, timeout: float = 3) -> bool:
    """True iff a container named `name` exists AND is currently running (`docker inspect`).

    For `job status`: distinguishes a still-alive (possibly stuck) run from one that is gone.
    Never raises - a docker error or a missing container reads as not-running."""
    return _container_running_state(name, runner, timeout) is True


def container_state(name: str, runner=subprocess.run, timeout: float = 3) -> bool | None:
    """Tri-state `container_running`: True / False, or None when Docker could not answer.

    For `job status`, which must not read "docker is down" as "the run is dead"."""
    return _container_running_state(name, runner, timeout)


def _container_running_state(name: str, runner, timeout: float = 3) -> bool | None:
    """Separate confirmed task death from an unavailable Docker daemon."""
    try:
        proc = runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if getattr(proc, "returncode", 1) == 0:
        state = (getattr(proc, "stdout", "") or "").strip()
        return state == "true" if state in {"true", "false"} else None
    error = (getattr(proc, "stderr", "") or "").strip().lower()
    if error in {f"error: no such object: {name}", f"error: no such container: {name}"}:
        return False
    return None


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


# The file the host touches (last, after a clean untar) to release the entrypoint's wait.
PROFILE_READY_MARKER = f"{CONTAINER_HOME}/.franky-profile-ready"


def deliver_profile(
    task: str,
    bundle: bytes | None,
    runner=subprocess.run,
    *,
    ready_polls: int = 30,
    poll_interval: float = 0.5,
    timeout: float = 60.0,
    sleeper=time.sleep,
    session_tar: str | None = None,
    session_timeout: float = 20.0,
    private_prompt_tar: bytes | None = None,
) -> bool:
    """Stream the operator profile tar into a freshly launched container, then signal it.

    Same channel and same reasoning as `snapshot.restore_into_container` (whose argv builders
    are reused here, only with HOME as the extraction target): a `docker cp` INTO the
    `--read-only` task container is refused by the daemon even for a tmpfs destination, so the
    bytes are piped to `tar -xzf -` over `docker exec -i` stdin. Extraction runs as the image's
    default uid 1001, which owns the HOME tmpfs, with `--no-same-owner` so no chown is needed
    under `--cap-drop=ALL`.

    The ready marker is touched LAST, only after a clean untar, so the entrypoint never execs
    the engine on a half-unpacked profile: no marker means it exits nonzero and the run is
    classified as a failure rather than silently proceeding without the operator's setup.
    Returns True iff every step succeeded; never raises.

    `session_tar` (a host tar path, `review-pr --thread`) is streamed from the file into HOME on
    the same channel AFTER the profile. Its failure still touches the marker: the review runs,
    the engine's resume fails, and the thread store drops that session for the next run.
    """
    from . import snapshot

    try:
        for _ in range(ready_polls):
            if container_running(task, runner):
                break
            sleeper(poll_interval)

        if bundle is not None:
            untar = runner(
                snapshot.build_untar_argv(task, target=CONTAINER_HOME),
                input=bundle,
                capture_output=True,
                timeout=timeout,
            )
            if getattr(untar, "returncode", 1) != 0:
                return False
        if session_tar is not None:
            try:
                with open(session_tar, "rb") as tar_fh:
                    runner(
                        snapshot.build_untar_argv(task, target=CONTAINER_HOME),
                        stdin=tar_fh,
                        capture_output=True,
                        timeout=session_timeout,
                    )
            except Exception:
                pass
        if private_prompt_tar is not None:
            untar = runner(
                snapshot.build_untar_argv(task, target="/tmp"),
                input=private_prompt_tar,
                capture_output=True,
                timeout=timeout,
            )
            if getattr(untar, "returncode", 1) != 0:
                return False
        marker = runner(
            snapshot.build_marker_argv(task, marker=PROFILE_READY_MARKER),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return getattr(marker, "returncode", 1) == 0
    except Exception:
        return False


def reap_run(run_id: str, runner=subprocess.run) -> bool:
    """Force-remove a run's disk helper, task, proxy sidecar, and internal network.

    The disk helper goes first so the task's `docker rm -v` can remove its anonymous volumes.
    Returns True iff the task container holding the injected credentials was removed.
    Never raises."""
    net, proxy, task = run_names(run_id)
    _reap(f"{task}-disk", runner)
    task_reaped = _reap(task, runner)
    _reap(proxy, runner)
    _reap_network(net, runner)
    return task_reaped


# Longest unfinished line kept while looking for the hold line; a longer one is not it.
_HOLD_LINE_MAX = 512


def _hold_nonce() -> str:
    return secrets.token_hex(8)


def _copy_session_in_hold(task: str, sink: dict, nonce: str, runner, popen) -> None:
    """The engine exited cleanly and the entrypoint holds the --rm container: copy the session
    out while it still exists, then release the hold. The release runs even when the copy fails
    or is interrupted, so the container exits; if the release itself fails, the entrypoint's cap
    ends the hold and --rm still removes the container. Never raises an Exception."""
    try:
        sink["status"] = snapshot.copy_session(
            task, sink["paths"], sink["dest"], max_bytes=sink["max_bytes"], popen=popen
        )
    except Exception:
        pass
    finally:
        try:
            runner(
                snapshot.build_marker_argv(
                    task, marker=f"{snapshot.SESSION_COPIED_MARKER}-{nonce}"
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass


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
                [
                    "docker",
                    "exec",
                    proxy,
                    "tail",
                    "-c",
                    str(_PROXY_LOG_BYTES + 1),
                    "/run/squid-access.log",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            proc.check_returncode()
            log = getattr(proc, "stdout", "") or ""
            truncated = len(log.encode("utf-8")) > _PROXY_LOG_BYTES
            incomplete = bool(log) and not log.endswith("\n")
            if truncated:
                # The first record can start inside a hostname or secret. Never parse it.
                log = log.partition("\n")[2]
            counts: dict[str, int] = {}
            for line in log.splitlines(keepends=True):
                # Squid can still be writing the final record during capture.
                if not line.endswith("\n"):
                    continue
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
            # Empty/zero means no denials in this tail, not an unreadable log. The flag
            # distinguishes a complete log from bounded recent observations.
            diag["egress_denied"] = [{"host": h, "count": c} for h, c in sorted(counts.items())]
            diag["proxy_denied_count"] = sum(counts.values())
            diag["proxy_log_truncated"] = truncated or incomplete
        except Exception:
            pass

    # Booleans only, extracted from the transcript at capture time - never the raw transcript
    # text itself (see the WHY-no-secret-surface note above). One try/except around BOTH scans
    # for symmetry: this is a pure string search with no I/O so it cannot really fail, but if it
    # somehow did we leave both keys at their safe defaults (dind_ready=None, tmpfs_full=False)
    # rather than one absent and one defaulted.
    try:
        signals = {
            s: False
            for s in (
                "rootless dockerd did not become ready",
                "rootless dockerd ready",
                "No space left on device",
                "ENOSPC",
            )
        }
        overlap = ""
        for chunk in chunks(transcript):
            text = overlap + chunk
            for signal in signals:
                signals[signal] |= signal in text
            overlap = text[-64:]
        if signals["rootless dockerd did not become ready"]:
            diag["dind_ready"] = False
        elif signals["rootless dockerd ready"]:
            diag["dind_ready"] = True
        else:
            diag["dind_ready"] = None
        diag["tmpfs_full"] = signals["No space left on device"] or signals["ENOSPC"]
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
        proc = runner(
            ["docker", "rm", "-f", "-v", name], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0 or (
        f"no such container: {name}" in (getattr(proc, "stderr", "") or "").lower()
    )


def _reap_network(net_name: str, runner) -> bool:
    """Best-effort `docker network rm`. Same never-raise contract as _reap. A leaked network
    is a low-severity resource leak (it holds NO creds), unlike a leaked task container."""
    try:
        proc = runner(
            ["docker", "network", "rm", net_name], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


def _storage_sample(task: str | None, image: str, runner) -> tuple[int, int]:
    """Read free KiB before launch without trusting the autonomous task's tools.

    Task storage uses the persistent helper in `_watch_storage`; this one-shot path is only the
    production preflight before a task exists. Raw helper errors never leave this function.
    """
    if task is not None:
        raise ValueError("task storage needs a persistent helper")
    helper = f"franky-disk-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        helper,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--cap-add=DAC_READ_SEARCH",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={DEFAULT_SECCOMP}",
        "--user=0",
        "--pids-limit=32",
        "--memory=64m",
        "--memory-swap=64m",
        "--entrypoint=bash",
    ]
    script = "set -eo pipefail\nprintf '0\\n'\ndf -Pk / | awk 'NR==2 {print $4}'"
    try:
        proc = runner(argv + [image, "-c", script], capture_output=True, text=True, timeout=10)
        values = proc.stdout.split()
        if proc.returncode or len(values) != 2 or not all(v.isdigit() for v in values):
            raise ValueError("could not measure task storage")
        return int(values[0]), int(values[1])
    finally:
        _reap(helper, runner)


def _task_storage_volumes(task: str, runner, timeout: float = 3) -> list[str]:
    inspected = runner(
        ["docker", "inspect", "-f", "{{json .Mounts}}", task],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if getattr(inspected, "returncode", 1) != 0:
        raise ValueError("could not inspect task storage")
    destinations = {"/work": "work", _HOME: "home", "/tmp": "tmp"}
    selected = {}
    for mount in json.loads(inspected.stdout):
        dest = mount.get("Destination")
        if dest not in destinations or mount.get("Type") != "volume":
            continue
        name = mount.get("Name", "")
        if dest in selected or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("invalid task storage")
        selected[dest] = name
    if set(selected) != set(destinations):
        raise ValueError("task storage is missing")
    return [selected[dest] for dest in destinations]


def _storage_helper_argv(task: str, image: str, volumes: list[str], lifetime: int) -> list[str]:
    destinations = ("work", "home", "tmp")
    mounts = [
        token
        for volume, destination in zip(volumes, destinations, strict=True)
        for token in ("--mount", f"type=volume,src={volume},dst=/data/{destination},readonly")
    ]
    return [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        f"{task}-disk",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--cap-add=DAC_READ_SEARCH",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={DEFAULT_SECCOMP}",
        "--user=0",
        "--pids-limit=32",
        "--memory=64m",
        "--memory-swap=64m",
        *mounts,
        "--entrypoint=sleep",
        image,
        str(max(1, lifetime)),
    ]


def _storage_helper_sample(helper: str, runner) -> tuple[int, int]:
    script = (
        "set -eo pipefail\n"
        "du -skx /data/work /data/home /data/tmp | awk '{n += $1} END {print n}'\n"
        "df -Pk /data/home | awk 'NR==2 {print $4}'"
    )
    proc = runner(
        ["docker", "exec", helper, "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    values = (getattr(proc, "stdout", "") or "").split()
    if getattr(proc, "returncode", 1) or len(values) != 2 or not all(v.isdigit() for v in values):
        raise ValueError("could not measure task storage")
    return int(values[0]), int(values[1])


def _remove_volumes(volumes: list[str], runner) -> None:
    try:
        runner(["docker", "volume", "rm", *volumes], capture_output=True, timeout=10)
    except Exception:
        pass


def _watch_storage(
    task: str,
    image: str,
    disk_mb: int,
    runner,
    stop,
    failures: list[str],
    helper_lifetime: int,
) -> None:
    """Stop excess disk use. This five-second watchdog is not a filesystem quota.

    Keep 1 GiB free for other containers. A fast writer can overshoot between samples;
    the Docker VM disk limit remains the final disk boundary.
    """
    helper = f"{task}-disk"
    volumes: list[str] = []
    helper_attempted = False
    stop_task = False
    try:
        # The Popen can return before Docker has created the task container.
        mount_deadline = time.monotonic() + 10
        for _ in range(20):
            if stop.is_set():
                return
            remaining = mount_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                volumes = _task_storage_volumes(task, runner, min(3, remaining))
                break
            except Exception:
                remaining = mount_deadline - time.monotonic()
                if remaining <= 0:
                    break
                if stop.wait(min(0.5, remaining)):
                    return
        else:
            raise ValueError("task storage did not appear")
        if not volumes:
            raise ValueError("task storage did not appear")

        helper_attempted = True
        started = runner(
            _storage_helper_argv(task, image, volumes, helper_lifetime),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if getattr(started, "returncode", 1) != 0:
            raise ValueError("could not start task storage helper")

        while not stop.is_set():
            used, free = _storage_helper_sample(helper, runner)
            message = (
                "franky: task exceeded its disk budget"
                if used > disk_mb * 1024
                else "franky: Docker has less than 1 GiB free disk"
                if free < 1024 * 1024
                else ""
            )
            if message:
                failures.append(message)
                stop_task = True
                return
            if stop.wait(5):
                return
    except Exception:
        if not stop.is_set() and _container_running_state(task, runner) is not False:
            failures.append("franky: could not verify task disk usage - stopping the run")
            stop_task = True
    finally:
        if helper_attempted:
            _reap(helper, runner)
        if stop_task:
            _reap(task, runner)
        if volumes and (stop_task or _container_running_state(task, runner) is False):
            _remove_volumes(volumes, runner)


def build_proxy_probe_argv(proxy_name: str) -> list[str]:
    """Probe Squid's default-deny HTTPS CONNECT policy from inside its container."""
    return [
        "docker",
        "exec",
        proxy_name,
        "curl",
        "--silent",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_connect}",
        "--proxy",
        f"http://127.0.0.1:{PROXY_PORT}",
        "--noproxy",
        "",
        "--connect-timeout",
        "2",
        "--max-time",
        "2",
        "https://denied.invalid:443",
    ]


def wait_proxy_ready(proxy_name: str, runner=subprocess.run, sleeper=time.sleep) -> bool:
    """Wait for Squid to refuse a known-denied HTTPS CONNECT with exactly 403."""
    deadline = time.monotonic() + PROXY_READY_TIMEOUT
    for _ in range(PROXY_READY_POLLS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            proc = runner(
                build_proxy_probe_argv(proxy_name),
                capture_output=True,
                text=True,
                timeout=min(PROXY_PROBE_TIMEOUT, remaining),
            )
        except Exception:
            proc = None
        status = (getattr(proc, "stdout", "") or "").strip()
        # curl returns nonzero when Squid refuses the tunnel. The CONNECT status is the gate;
        # process success alone proves neither liveness nor a default-deny policy.
        if status == "403":
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not container_running(proxy_name, runner, min(3, remaining)):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleeper(min(PROXY_READY_INTERVAL, remaining))
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
    profile_bundle: bytes | None = None,
    progress=None,
    popen=subprocess.Popen,
    run_id: str | None = None,
    diagnostics_sink: dict | None = None,
    snapshot_sink: dict | None = None,
    resume_workspace: str | None = None,
    apparmor_selector=select_task_apparmor,
    session_tar: str | None = None,
    session_sink: dict | None = None,
    private_prompt_tar: bytes | None = None,
) -> tuple[int, str | Transcript]:
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

    `session_tar` (`review-pr --thread`) is a host tar of a stored engine session, streamed into
    HOME with the profile (same wait mode). `session_sink` = {"paths": [HOME-relative paths],
    "dest": host dir, "max_bytes": int}: after a clean exit (code 0), before the reap, each path
    is streamed out (`docker cp ... -`, 20s cap each) and extracted as plain files and dirs under
    `dest`. The first path is required, the rest optional; the sink gains `"status"`: ok,
    too_large (the combined stream passed `max_bytes`), or failed. `"on_timeout": True` also
    copies the session out of a timed-out run (`build --thread`, `job resume`).

    Topology: an --internal network (no internet route) hosts a Squid proxy (default-deny
    allowlist) and the task container. The task's HTTP(S)_PROXY points at the proxy and its
    own DNS is killed, so its only path out is through the allowlist. We refuse to start the
    task until the proxy passes the default-deny startup probe. A `finally` ALWAYS tears down
    whatever was created, in order task -> proxy -> net.

    The child env is os.environ + cfg.passthrough_env, so the `-e KEY` (name-only) flags
    resolve to real values inside the task without those values ever hitting the argv. The
    PROXY gets NO cred env. `runner`/`sleeper` are injectable so tests run fakes and never
    touch real docker. All returned output is scrubbed of secret values.

    Production always drains Popen in bounded chunks into a redacted disk transcript.
    The caller must persist or close that transcript. Progress receives redacted lines,
    or bounded fragments when a line exceeds the parser limit. Injected runner-only callers
    retain their string result; injected Popen callers use the same disk path as production.
    """
    # Names derive from run_id so the CLI can register them up front and later target the same
    # container/net/proxy for `job status`/`kill` (issue #63). None -> a fresh id (unchanged
    # behavior for callers that do not track a job).
    net, proxy, task = run_names(run_id or uuid.uuid4().hex[:12])

    provider_env = dict(cfg.passthrough_env)
    if cfg.auth_volume:
        provider_env[CODEX_SUBSCRIPTION_VAR] = "1"
    allowed = egress.build_allowlist(
        cfg.engine, provider_env, cfg.extra_allowed_domains, model=cfg.model
    )

    child_env = dict(os.environ if env is None else env)
    child_env.update(cfg.passthrough_env)

    secrets = cfg.secret_values()

    net_created = False
    proxy_launched = False
    task_launched = False  # an OSError on spawn means docker never created the container
    code, output = 1, ""
    storage_stop = threading.Event()
    storage_failures: list[str] = []
    storage_thread = None
    hold_copied = False  # the session was copied while the entrypoint held the container
    hold_nonce = _hold_nonce() if session_sink is not None else ""
    hold_want = f"{snapshot.SESSION_HOLD_LINE} {hold_nonce}"
    hold_buf, hold_at_line_start = "", True

    try:
        try:
            apparmor_profile = apparmor_selector(runner)
        except SecurityPolicyError as exc:
            raise _AbortRun(f"franky: {exc} - refusing to run") from exc
        if runner is subprocess.run:
            try:
                _, free = _storage_sample(None, image, runner)
            except Exception:
                raise _AbortRun("franky: could not verify Docker free disk - refusing to run")
            if free < 1024 * 1024:
                raise _AbortRun("franky: Docker has less than 1 GiB free disk - refusing to run")
        if cfg.auth_volume:
            auth_secrets = _codex_auth_state(image, runner, auth_volume=cfg.auth_volume)
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

        # 4. Fail-closed gate: NEVER run the task before Squid proves default-deny CONNECT.
        if not wait_proxy_ready(proxy, runner, sleeper):
            raise _AbortRun(
                "franky: egress proxy did not become ready - refusing to run (fail-closed)"
            )

        # 5. The task container: on the internal net, all traffic forced through the proxy.
        # Production always streams, including quiet runs. Runner-only test doubles keep
        # their injected capture boundary; profile/restore and injected Popen use streaming.
        resuming = resume_workspace is not None
        injecting = (
            profile_bundle is not None or session_tar is not None or private_prompt_tar is not None
        )
        effective_progress = progress
        if (
            resuming or injecting or runner is subprocess.run or popen is not subprocess.Popen
        ) and effective_progress is None:
            effective_progress = lambda _line: None  # noqa: E731 - tiny no-op for the stream loop
        argv = build_docker_argv(
            image,
            cfg.passthrough_env,
            inner_argv,
            name=task,
            network=net,
            proxy_url=proxy_url(proxy),
            profile_wait=injecting,
            resume_wait=resuming,
            auth_volume=cfg.auth_volume,
            memory_mb=cfg.memory_mb,
            disk_mb=cfg.disk_mb,
            apparmor_profile=apparmor_profile,
            session_hold=hold_nonce,
        )
        if effective_progress is not None:
            # Redact before disk and callbacks, retaining only bounded unfinished fragments.
            output = Transcript()
            stream = Redactor(secrets)
            progress_pending = ""

            def emit(text, *, final=False):
                nonlocal progress_pending
                clean = stream.feed(text, final=final)
                output.write(clean)
                progress_pending += clean
                while "\n" in progress_pending:
                    line, progress_pending = progress_pending.split("\n", 1)
                    effective_progress(line + "\n")
                if len(progress_pending) > MAX_EVENT_CHARS or final:
                    if progress_pending:
                        effective_progress(progress_pending)
                    progress_pending = ""

            try:
                proc = popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                )
                task_launched = True
                if runner is subprocess.run:
                    # Setup precedes the task timeout. Current bounded profile and restore
                    # calls need at most 245 + 145 seconds, including readiness inspections.
                    setup_grace = 400 if injecting or resuming else 0
                    storage_thread = threading.Thread(
                        target=_watch_storage,
                        args=(
                            task,
                            image,
                            cfg.disk_mb,
                            runner,
                            storage_stop,
                            storage_failures,
                            timeout + setup_grace + 30,
                        ),
                        daemon=True,
                    )
                    storage_thread.start()
                # Stream the operator profile in BEFORE draining stdout: the container is blocked
                # on the ready marker in profile-wait mode. A False result is fine to proceed on -
                # the entrypoint exits nonzero on its own (refusing to run without the operator's
                # setup) and the run classifies as agent_error; do NOT abort the stream, drain it.
                if injecting:
                    deliver_profile(
                        task,
                        profile_bundle,
                        runner,
                        sleeper=sleeper,
                        session_tar=session_tar,
                        private_prompt_tar=private_prompt_tar,
                    )
                # Restore the prior workspace into the just-launched container (issue #71) BEFORE
                # draining stdout: the container is waiting on the ready marker in resume-wait
                # mode. A False result is fine to proceed on - the entrypoint will exit 75 quickly
                # (refusing to run on an empty /work) and the run classifies as agent_error; do
                # NOT abort the stream, just drain it.
                if resume_workspace is not None:
                    snapshot.restore_into_container(
                        task, Path(resume_workspace), runner, sleeper=sleeper
                    )
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
                    import codecs

                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    reader = getattr(proc.stdout, "read1", None) or getattr(
                        proc.stdout, "read", None
                    )
                    source = iter(lambda: reader(CHUNK_SIZE), b"") if reader else iter(proc.stdout)
                    for line in source:
                        if not line:
                            break
                        text = decoder.decode(line) if isinstance(line, bytes) else line
                        emit(text)
                        if session_sink is not None and not hold_copied:
                            # Only a WHOLE line equal to the hold line counts. The run timeout
                            # stays armed: the agent can read the nonce and print the line, which
                            # only buys an early, partial copy of its own session. Known limit:
                            # an engine that exits seconds before the deadline can time out
                            # mid-copy.
                            *complete, hold_buf = (hold_buf + text).split("\n")
                            for done_line in complete:
                                if hold_at_line_start and done_line.rstrip("\r") == hold_want:
                                    hold_copied = True
                                hold_at_line_start = True
                            if len(hold_buf) > _HOLD_LINE_MAX:
                                hold_buf, hold_at_line_start = "", False
                            if hold_copied:
                                _copy_session_in_hold(task, session_sink, hold_nonce, runner, popen)
                        # Belt-and-suspenders: the per-line elapsed check still catches a slow
                        # trickle of output between deadline checks; the watchdog above catches a
                        # total silence.
                        if watchdog_hit.is_set() or time.monotonic() - t_start >= timeout:
                            proc.kill()
                            timed_out = True
                            break
                finally:
                    watchdog.cancel()
                    proc.stdout.close()
                    remaining = max(1.0, timeout - (time.monotonic() - t_start))
                    try:
                        proc.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        proc.kill()
                        proc.wait(timeout=5)
                    emit(decoder.decode(b"", final=True), final=True)
                if timed_out or watchdog_hit.is_set():
                    code = CONTAINER_TIMEOUT_CODE
                    output.write("\n" + CONTAINER_TIMEOUT_MSG.format(timeout=timeout))
                else:
                    code = proc.returncode
            except OSError as exc:
                code = 1
                emit(f"franky: could not launch docker ({exc})", final=True)
            except BaseException:
                output.close()
                raise
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
        tail = output.tail(4096) if isinstance(output, Transcript) else output[-4096:]
        if (
            code
            and apparmor_profile
            and "apparmor" in tail.lower()
            and apparmor_profile.lower() in tail.lower()
            and any(marker in tail.lower() for marker in ("not found", "not loaded"))
        ):
            hint = (
                "\nfranky: the AppArmor task profile is not loaded. Run:\n"
                "`franky apparmor-profile > franky-task.apparmor && "
                "sudo /usr/sbin/apparmor_parser -K -r franky-task.apparmor && "
                "sudo /usr/bin/install -m 0644 franky-task.apparmor "
                "/etc/apparmor.d/franky-task`"
            )
            if isinstance(output, Transcript):
                output.write(hint)
            else:
                output += hint
    except _AbortRun as abort:
        # A pre-task step failed. code/output set here; teardown still runs in `finally` and
        # its warnings append to THIS output, which the single return below surfaces.
        code, output = 1, abort.message
    finally:
        storage_stop.set()
        if storage_thread is not None:
            storage_thread.join(timeout=40)
            _reap(f"{task}-disk", runner)
        if storage_failures:
            code = code or 1
            message = "\n" + storage_failures[0]
            if isinstance(output, Transcript):
                output.write(message)
            else:
                output += message
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
        # Thread session copy-out: like the snapshot extract, it needs the live container, so it
        # runs before the reap, capped. A clean exit normally copied already, during the
        # entrypoint's session hold (--rm removes the container once the engine exits); this
        # covers a timeout when the sink opts in with `on_timeout` (the killed CLI leaves the
        # container running until the reap), and an image without the hold. Failures are swallowed.
        if (
            session_sink is not None
            and task_launched
            and not hold_copied
            and (code == 0 or (session_sink.get("on_timeout") and code == CONTAINER_TIMEOUT_CODE))
        ):
            try:
                session_sink["status"] = snapshot.copy_session(
                    task,
                    session_sink["paths"],
                    session_sink["dest"],
                    max_bytes=session_sink["max_bytes"],
                    popen=popen,
                )
            except Exception:
                pass
        # Best-effort teardown, ALWAYS, in order task -> proxy -> net. A reap FAILURE on the
        # TASK container is surfaced at full severity because it holds the injected creds. A
        # proxy/net reap failure is a lower-severity resource leak (the proxy holds NO creds).
        if task_launched and not _reap(task, runner):
            message = f"\nfranky: WARNING container {task} may not have been removed - check `docker ps -a`"
            if isinstance(output, Transcript):
                output.write(redact(message, secrets))
            else:
                output += message
        if proxy_launched and not _reap(proxy, runner):
            message = f"\nfranky: WARNING egress proxy {proxy} may not have been removed (resource leak, no creds) - check `docker ps -a`"
            if isinstance(output, Transcript):
                output.write(redact(message, secrets))
            else:
                output += message
        if net_created and not _reap_network(net, runner):
            message = f"\nfranky: WARNING egress network {net} may not have been removed (resource leak) - check `docker network ls`"
            if isinstance(output, Transcript):
                output.write(redact(message, secrets))
            else:
                output += message
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

    return code, output if isinstance(output, Transcript) else redact(output, secrets)


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


def resolve_image(
    env: dict,
    var: str = FRANKY_IMAGE_VAR,
    name: str = "franky",
    *,
    engine: str | None = None,
) -> str:
    """Return the image ref to use. If the per-image env var is set and truthy, return it
    (dev override). Otherwise return the version-pinned GHCR ref, with the GHCR namespace
    taken from FRANKY_GHCR_REPO when set, else DEFAULT_GHCR_REPO. NEVER resolves to :latest."""
    override = env.get(var)
    if override:
        return override
    repo = env.get(GHCR_REPO_VAR) or DEFAULT_GHCR_REPO
    tag = franky_version()
    if engine is not None:
        if engine not in ENGINES:
            raise ValueError("unknown image engine")
        tag = f"{tag}-{engine}"
    return f"{repo}/{name}:{tag}"


def ensure_image_available(
    image: str, runner=subprocess.run, timeout: float = PULL_TIMEOUT_SECS
) -> tuple[bool, str]:
    """Ensure `image` is available locally, pulling if needed.
    Returns (True, "") on success.
    Returns (False, "no-docker") if docker is not available (OSError).
    Returns (False, "auth") if the pull failed with an auth/credentials error.
    Returns (False, "pull-timeout") if the pull did not finish within `timeout` seconds.
    Returns (False, "pull-failed") for any other pull failure.
    Never raises."""
    if image_exists(image, runner):
        return True, ""
    try:
        proc = runner(["docker", "pull", image], capture_output=True, text=True, timeout=timeout)
    except OSError:
        return False, "no-docker"
    except subprocess.TimeoutExpired:
        return False, "pull-timeout"
    if proc.returncode == 0:
        return True, ""
    combined = ((proc.stdout or "") + (proc.stderr or "")).lower()
    if any(
        kw in combined for kw in ("denied", "unauthorized", "authentication", "forbidden", "401")
    ):
        return False, "auth"
    return False, "pull-failed"
