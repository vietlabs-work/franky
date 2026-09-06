"""Opt-in, credential-free checks for the production Docker sandbox."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from franky.container import (  # noqa: E402
    _reap,
    _storage_helper_argv,
    _task_storage_volumes,
    build_docker_argv,
    build_proxy_argv,
    wait_proxy_ready,
)
from franky.security import (  # noqa: E402
    DEFAULT_SECCOMP,
    TASK_APPARMOR_NAME,
    TASK_SECCOMP,
    select_task_apparmor,
)


# Syscall numbers: Linux arch/x86/entry/syscalls/syscall_64.tbl and
# include/uapi/asm-generic/unistd.h. Invalid commands cannot create keys or BPF programs.
PROBE = r"""
import ctypes
import json
import os
from pathlib import Path
import platform
import sys
import tempfile

numbers = {"x86_64": (250, 321), "aarch64": (219, 280)}
if platform.machine() not in numbers:
    raise ValueError("unsupported syscall architecture")
keyctl, bpf = numbers[platform.machine()]
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
results = {}
mode = sys.argv[1]

def record(name, function, *args):
    ctypes.set_errno(0)
    result = function(*args)
    results[name] = [result, ctypes.get_errno()]

if mode == "mount":
    target = tempfile.mkdtemp(prefix="franky-security-")
    record("mount", libc.mount, b"none", os.fsencode(target), b"tmpfs", 0, None)
elif mode == "helper":
    record("unshare", libc.unshare, 0x10000000)  # CLONE_NEWUSER
    record("keyctl", libc.syscall, keyctl, -1, 0, 0, 0, 0)
elif mode == "nested":
    record("keyctl", libc.syscall, keyctl, -1, 0, 0, 0, 0)
    record("bpf", libc.syscall, bpf, -1, 0, 0)
    record("unshare_net", libc.unshare, 0x40000000)  # CLONE_NEWNET
    for name in ("ip_unprivileged_port_start", "ip_forward"):
        path = Path("/proc/sys/net/ipv4") / name
        try:
            value = path.read_text()
            results[name] = [path.write_text(value), 0]
        except OSError as exc:
            results[name] = [-1, exc.errno]
else:
    raise ValueError("unknown syscall probe")
print(json.dumps({"uid": os.getuid(), "status": Path("/proc/self/status").read_text(),
                  "uid_map": Path("/proc/self/uid_map").read_text(), "results": results}))
"""


def check_status(text, *, uid, capabilities=None):
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    if int(fields.get("Seccomp", "")) != 2 or int(fields.get("Seccomp_filters", "")) < 1:
        raise ValueError("an active seccomp filter is required")
    if fields.get("Uid", "").split() != [str(uid)] * 4:
        raise ValueError("unexpected container uid")
    if capabilities is not None:
        if int(fields.get("CapBnd", ""), 16) != capabilities:
            raise ValueError("unexpected capability bounding set")
        for name in ("CapEff", "CapPrm", "CapInh", "CapAmb"):
            if int(fields.get(name, ""), 16) & ~capabilities:
                raise ValueError("unexpected process capabilities")


def check_profile(options, path):
    policies = [
        option.removeprefix("seccomp=") for option in options if option.startswith("seccomp=")
    ]
    if len(policies) != 1 or json.loads(policies[0]) != json.loads(path.read_text()):
        raise ValueError("container does not use its packaged seccomp policy")


def check_helper_mounts(mounts, volumes):
    expected = set(zip(volumes, ("/data/work", "/data/home", "/data/tmp"), strict=True))
    actual = {(mount.get("Name"), mount.get("Destination")) for mount in mounts}
    if (
        len(mounts) != 3
        or actual != expected
        or any(mount.get("Type") != "volume" or mount.get("RW") is not False for mount in mounts)
    ):
        raise ValueError("helper must mount only the three task volumes read-only")


def run_smoke(
    image, proxy_image, *, runner=subprocess.run, sleeper=time.sleep, clock=time.monotonic
):
    task = f"franky-security-{uuid.uuid4().hex[:12]}"
    proxy, helper = f"{task}-proxy", f"{task}-disk"
    overall_deadline = clock() + 90
    deadline = overall_deadline - 25  # Reserve cleanup time, including a failed Docker create.
    call_limit = 60
    volumes = []

    def bounded(argv, **kwargs):
        remaining = deadline - clock()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, 0)
        kwargs["timeout"] = min(kwargs.get("timeout", 10), call_limit, remaining)
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return runner(argv, **kwargs)

    def checked(argv, timeout=10):
        return bounded(argv, timeout=timeout, check=True).stdout

    def probe(name, mode, *, nested=False):
        command = ["docker", "exec", name]
        if nested:
            command += ["rootlesskit", "--net=host", f"--state-dir=/tmp/{task}-rootless"]
        return json.loads(checked(command + ["python3", "-c", PROBE, mode]))

    def denied(result, name, expected):
        if result["results"].get(name) != [-1, expected]:
            raise ValueError(f"{name} did not fail with the required errno {expected}")

    try:
        apparmor_profile = select_task_apparmor(bounded)
        argv = build_docker_argv(
            image,
            {},
            ["sleep", "90"],
            name=task,
            network="none",
            apparmor_profile=apparmor_profile,
        )
        argv.insert(2, "-d")
        checked(argv, timeout=40)
        volumes = _task_storage_volumes(task, bounded)
        checked(_storage_helper_argv(task, image, volumes, 90))
        argv = build_proxy_argv(proxy_image, proxy, [".github.com"])
        argv.insert(3, "--network=none")
        checked(argv)
        mounts = checked(["docker", "inspect", "-f", "{{json .Mounts}}", helper])
        check_helper_mounts(json.loads(mounts), volumes)
        for name, policy, uid, caps in (
            (task, TASK_SECCOMP, 1001, 0xC0),  # SETUID and SETGID only.
            (proxy, DEFAULT_SECCOMP, 13, 0),
            (helper, DEFAULT_SECCOMP, 0, 4),  # DAC_READ_SEARCH only.
        ):
            options = checked(["docker", "inspect", "-f", "{{json .HostConfig.SecurityOpt}}", name])
            check_profile(json.loads(options), policy)
            check_status(
                checked(["docker", "exec", name, "cat", "/proc/self/status"]),
                uid=uid,
                capabilities=caps,
            )
        if apparmor_profile is not None:
            options = json.loads(
                checked(["docker", "inspect", "-f", "{{json .HostConfig.SecurityOpt}}", task])
            )
            if options.count(f"apparmor={TASK_APPARMOR_NAME}") != 1:
                raise ValueError("task does not use the required AppArmor profile")
            label = checked(["docker", "exec", task, "cat", "/proc/self/attr/current"])
            if not label.startswith(f"{TASK_APPARMOR_NAME} (enforce)"):
                raise ValueError("task AppArmor profile is not enforcing")
        if not wait_proxy_ready(proxy, bounded, sleeper):
            raise ValueError("proxy did not enforce default-deny")
        ready_deadline = min(deadline, clock() + 35)
        while True:
            try:
                result = bounded(["docker", "exec", task, "docker", "info"], timeout=3)
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                break
            remaining = ready_deadline - clock()
            if remaining <= 0:
                # Allow one bounded diagnostic read, while retaining 20 seconds for cleanup.
                deadline = min(overall_deadline - 20, clock() + 3)
                try:
                    log = checked(
                        ["docker", "exec", task, "tail", "-c", "4096", "/tmp/dockerd.log"],
                        timeout=3,
                    )[-4096:]
                except (OSError, subprocess.SubprocessError):
                    log = "dockerd log unavailable"
                raise ValueError("production rootless Docker did not become ready\n" + log)
            sleeper(min(0.5, remaining))
        denied(probe(task, "mount"), "mount", 1)  # Linux EPERM, independent of the host OS.
        result = probe(helper, "helper")
        denied(result, "unshare", 1)
        denied(result, "keyctl", 1)
        result = probe(task, "nested", nested=True)
        check_status(result["status"], uid=0)
        if result["uid"] != 0 or result["uid_map"].splitlines()[0].split() != ["0", "1001", "1"]:
            raise ValueError("RootlessKit did not map the unprivileged task uid")
        denied(result, "keyctl", 38)  # Linux ENOSYS: runc accepts unavailable keyrings.
        denied(result, "bpf", 1)
        if apparmor_profile is not None:
            if result["results"].get("unshare_net") != [0, 0]:
                raise ValueError("nested network namespace setup failed")
            if result["results"].get("ip_unprivileged_port_start", [-1])[0] < 0:
                raise ValueError("required nested Docker sysctl was denied")
            denied(result, "ip_forward", 13)  # AppArmor EACCES: unrelated sysctls stay denied.
    finally:
        primary_error = sys.exc_info()[1]
        deadline = overall_deadline
        call_limit = 5
        if not volumes:
            try:
                volumes = _task_storage_volumes(task, bounded)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
        failures = [name for name in (helper, task, proxy) if not _reap(name, bounded)]
        if volumes:
            try:
                result = bounded(["docker", "volume", "rm", *volumes], timeout=5)
                errors = result.stderr.splitlines()
                if result.returncode and (
                    not errors or not all("no such volume" in line.lower() for line in errors)
                ):
                    failures.append("task volumes")
            except (OSError, subprocess.SubprocessError):
                failures.append("task volumes")
        if failures:
            message = "sandbox smoke cleanup failed: " + ", ".join(failures)
            if primary_error is None:
                raise ValueError(message)
            print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="franky")
    parser.add_argument("--proxy-image", default="franky-proxy")
    args = parser.parse_args()
    run_smoke(args.image, args.proxy_image)
    print("sandbox smoke: explicit filters, rootless Docker, and denied syscalls verified")


if __name__ == "__main__":
    main()
