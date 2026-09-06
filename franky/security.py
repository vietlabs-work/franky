"""Franky's packaged container policies and host-policy selection."""

import json
from pathlib import Path
import subprocess


# The JSON is moby/profiles/seccomp/default.json at this immutable commit. Its Apache-2.0
# notice is moby/profiles/LICENSE at the same commit. The default profile is byte-identical
# to upstream except for the repository-required final newline.
MOBY_PROFILES_COMMIT = "61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31"
# The AppArmor profiles render Moby v27.3.1's profiles/apparmor/template.go for an
# unconfined daemon. The hash pins that reviewed template; the existing Moby license applies.
MOBY_APPARMOR_TEMPLATE_SHA256 = "42130ca5908f45263facef820961d9e1a988e82f8a2937f4658e7bcccc96cc07"
_PACKAGE = Path(__file__).resolve().parent
DEFAULT_SECCOMP = _PACKAGE / "security-default.json"
TASK_SECCOMP = _PACKAGE / "security-task.json"
TASK_APPARMOR = _PACKAGE / "security-task.apparmor"
TASK_APPARMOR_NAME = "franky-task"
# Task-only additions support RootlessKit 2.3.1, slirp4netns 1.2.0, and runc 1.1.14.
# clone admits NEWUSER/NEWNS; unshare admits namespace flags plus FS/FILES, not NEWTIME.
# The kernel still requires namespace-local capabilities for mount, pivot_root, setns,
# sethostname, and umount2. Outer capabilities remain SETUID/SETGID only.
# keyctl remains blocked with ENOSYS because runc treats unavailable keyrings as optional.
# tests/test_security.py pins the complete delta; smoke-security and smoke-dind verify it.
SECCOMP_LICENSE = _PACKAGE / "security-default.LICENSE"
SECURITY_FILES = (
    DEFAULT_SECCOMP,
    TASK_SECCOMP,
    TASK_APPARMOR,
    SECCOMP_LICENSE,
)


class SecurityPolicyError(ValueError):
    """Docker host policy could not be selected safely."""


def select_task_apparmor(runner=subprocess.run) -> str | None:
    """Select Franky's task profile only when Docker reports AppArmor support."""
    argv = ["docker", "info", "--format", "{{json .SecurityOptions}}"]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=3)
        options = json.loads(proc.stdout) if proc.returncode == 0 else None
    except Exception as exc:
        raise SecurityPolicyError("could not inspect Docker security options") from exc
    if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
        raise SecurityPolicyError("could not inspect Docker security options")
    return TASK_APPARMOR_NAME if "name=apparmor" in options else None
