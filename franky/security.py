"""Paths to Franky's packaged, reviewed container seccomp policies."""

from pathlib import Path


# The JSON is moby/profiles/seccomp/default.json at this immutable commit. Its Apache-2.0
# notice is moby/profiles/LICENSE at the same commit. The default profile is byte-identical
# to upstream except for the repository-required final newline.
MOBY_PROFILES_COMMIT = "61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31"
_PACKAGE = Path(__file__).resolve().parent
DEFAULT_SECCOMP = _PACKAGE / "security-default.json"
TASK_SECCOMP = _PACKAGE / "security-task.json"
# Task-only additions support RootlessKit 2.3.1, slirp4netns 1.2.0, and runc 1.1.14.
# clone admits NEWUSER/NEWNS; unshare admits namespace flags plus FS/FILES, not NEWTIME.
# The kernel still requires namespace-local capabilities for mount, pivot_root, setns,
# sethostname, and umount2. Outer capabilities remain SETUID/SETGID only.
# keyctl remains blocked with ENOSYS because runc treats unavailable keyrings as optional.
# tests/test_security.py pins the complete delta; smoke-security and smoke-dind verify it.
SECCOMP_LICENSE = _PACKAGE / "security-default.LICENSE"
SECURITY_FILES = (DEFAULT_SECCOMP, TASK_SECCOMP, SECCOMP_LICENSE)
