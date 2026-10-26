#!/usr/bin/env python3
"""Start this task's rootless Docker once, within one bounded readiness budget."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

READY = "franky: rootless dockerd ready"
FAILED = (
    "franky: WARNING rootless dockerd did not become ready in 30s - docker-dependent "
    "build/test steps will fail (see /tmp/dockerd.log inside the container)."
)


def _stop(process):
    # The launcher owns this detached group, including children left by an exited leader.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def start(runtime_dir, *, timeout=30, log_path=Path("/tmp/dockerd.log")):
    deadline = time.monotonic() + timeout
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DOCKER_HOST": f"unix://{runtime_dir}/docker.sock",
    }
    process = None
    ready = False

    def probe():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            return (
                subprocess.run(
                    ["docker", "version"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(1, remaining),
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

    try:
        with (runtime_dir / "franky-docker.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(0.1, remaining))
            try:
                if probe():
                    ready = True
                    return True
                if time.monotonic() >= deadline:
                    return False
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        ["dockerd-rootless.sh"],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        return False
                    if probe():
                        ready = True
                        return True
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                return False
            finally:
                # Keep the lock until cleanup ends, so another caller cannot race a dying daemon.
                if process is not None and not ready:
                    _stop(process)
    except OSError:
        return False


if __name__ == "__main__":
    try:
        ok = start(Path(f"/run/user/{os.getuid()}"))
    except OSError:
        ok = False
    print(READY if ok else FAILED, file=sys.stderr)
    sys.exit(0 if ok else 1)
