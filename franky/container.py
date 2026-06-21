"""Run the inner engine inside a hardened, disposable Docker container.

WHY the hardening is load-bearing: the engine runs autonomously (claude with
--dangerously-skip-permissions, pi with its default tools), so OS-level isolation - not
tool prompts - is what bounds it. The argv is built by a pure function so tests can assert
the hardening flags WITHOUT ever invoking docker. Secret values are passed by VAR NAME only
(`-e KEY`), so docker inherits the value from Franky's own env and the value never lands on
the argv or in `ps` output.
"""
from __future__ import annotations

import os
import subprocess
import uuid

from .config import redact

# Long agent runs: a full clone-build-test-PR cycle can take many minutes. 30 min cap.
FALLBACK_TIMEOUT_SECS = 1800

# Hardening flags applied to every run. No bind mounts, no docker socket: the repo is
# cloned INSIDE the container, so the agent never touches the host filesystem.
_HARDENING = [
    "--rm",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--read-only",
    "--tmpfs", "/work:exec",
    "--pids-limit=512",
    "--memory=4g",
]


def build_docker_argv(
    image: str,
    passthrough_env: dict[str, str],
    inner_argv: list[str],
    name: str | None = None,
) -> list[str]:
    """Build the full `docker run` argv. Pure - no docker invoked.

    `-e KEY` (name only) for each passthrough var: docker reads the value from the parent
    process env, keeping the secret value off the argv. `--name` is added so a reaper can
    target this exact container after a timeout. Image + inner argv go last.
    """
    container_name = name or f"franky-run-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", *_HARDENING, "--name", container_name]
    for key in passthrough_env:
        argv += ["-e", key]
    argv += [image, *inner_argv]
    return argv


def _reap(name: str, runner) -> None:
    """Best-effort `docker rm -f` so a container does not linger after a timeout/error.
    Swallows everything - the reaper must never mask the original outcome."""
    try:
        runner(["docker", "rm", "-f", name], capture_output=True, text=True)
    except Exception:
        pass


def run_in_container(
    cfg,
    inner_argv: list[str],
    image: str = "franky",
    timeout: int = FALLBACK_TIMEOUT_SECS,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run the inner engine in a hardened container; return (returncode, redacted_output).

    The child env is os.environ + cfg.passthrough_env, so the `-e KEY` (name-only) flags
    resolve to real values inside the child without those values ever hitting the argv.
    `runner` is injectable so tests run a fake and never touch real docker. All returned
    output is scrubbed of secret values.
    """
    name = f"franky-run-{uuid.uuid4().hex[:12]}"
    argv = build_docker_argv(image, cfg.passthrough_env, inner_argv, name=name)

    child_env = dict(os.environ if env is None else env)
    child_env.update(cfg.passthrough_env)

    secrets = cfg.secret_values()
    try:
        try:
            proc = runner(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            _reap(name, runner)
            return 1, redact(f"franky: container timed out after {timeout}s", secrets)
        except OSError as exc:
            return 1, redact(f"franky: could not launch docker ({exc})", secrets)

        combined = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, redact(combined, secrets)
    finally:
        # Unconditional reaper: --rm covers the clean exit, this covers everything else
        # (timeout already reaped above is idempotent; rm -f on a gone container is a no-op).
        _reap(name, runner)


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
