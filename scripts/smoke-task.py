"""Start a manual smoke container with the production argv builders."""

import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from franky.container import build_docker_argv, build_proxy_argv  # noqa: E402
from franky.security import select_task_apparmor  # noqa: E402

image, name, mode = sys.argv[1:4]
if mode == "proxy":
    argv = build_proxy_argv(image, name, sys.argv[4].split(","))
else:
    argv = build_docker_argv(
        image,
        {},
        sys.argv[4:],
        name=name,
        network=os.environ.get("SMOKE_NETWORK"),
        proxy_url=os.environ.get("SMOKE_PROXY_URL"),
        profile_wait=mode in ("profile", "both"),
        resume_wait=mode in ("resume", "both"),
        session_hold="smoke" if mode == "hold" else "",
        apparmor_profile=select_task_apparmor(),
    )
    if mode == "hold":
        # Attached, as production runs it: the hardening's log driver cannot be read back
        # with `docker logs`, so the hold line only exists on this process's own output.
        os.execvp(argv[0], argv)
    argv.insert(2, "-d")
subprocess.run(argv, check=True, timeout=60)
