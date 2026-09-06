"""Pure checks for Franky's pinned seccomp profiles and Docker argv wiring."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from franky import container, security

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


REVIEWED_TASK_EXCEPTIONS = {
    "mount",
    "pivot_root",
    "setns",
    "sethostname",
    "umount2",
}
DEFAULT_APPARMOR = Path(__file__).with_name("fixtures") / "security-default.apparmor"


def _seccomp_arg(path: Path) -> str:
    return f"--security-opt=seccomp={path}"


def _apparmor_arg(name: str) -> str:
    return f"--security-opt=apparmor={name}"


def _load_footprint():
    path = Path(__file__).resolve().parents[1] / "scripts" / "footprint.py"
    spec = importlib.util.spec_from_file_location("footprint_security", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_profile_is_pinned_to_reviewed_moby_source():
    # Upstream default.json has no final newline. Repository hygiene adds exactly one.
    content = security.DEFAULT_SECCOMP.read_bytes()
    assert content.endswith(b"\n") and not content.endswith(b"\n\n")
    assert hashlib.sha256(content.removesuffix(b"\n")).hexdigest() == (
        "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74"
    )
    assert security.MOBY_PROFILES_COMMIT == "61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31"


def test_moby_license_is_pinned_and_packaged():
    content = security.SECCOMP_LICENSE.read_bytes()
    assert content.endswith(b"\n") and not content.endswith(b"\n\n")
    assert hashlib.sha256(content).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )


def test_default_apparmor_profile_is_pinned_to_reviewed_moby_source():
    content = DEFAULT_APPARMOR.read_bytes()
    assert content.endswith(b"\n") and not content.endswith(b"\n\n")
    assert hashlib.sha256(content).hexdigest() == (
        "537cd0f00ac450a17ac0c55c4d943f5cbbe6cba8faebfc18e73217a33ac02845"
    )
    assert security.MOBY_APPARMOR_TEMPLATE_SHA256 == (
        "42130ca5908f45263facef820961d9e1a988e82f8a2937f4658e7bcccc96cc07"
    )


def test_task_apparmor_profile_is_default_plus_only_reviewed_exceptions():
    default = DEFAULT_APPARMOR.read_text()
    task = security.TASK_APPARMOR.read_text()
    shared = "net/ipv"
    port = "net/ipv4/ip_unprivileged_port_start"
    ipv6 = "conf/"
    setting = "disable_ipv6"

    def mismatches(target, *, start=1):
        return [
            f"{target[:index]}[^{char}]**" for index, char in enumerate(target) if index >= start
        ]

    paths = [
        *mismatches(shared),
        shared + "[^46]**",
        *mismatches(port, start=len(shared) + 1),
        port + "?**",
    ]
    ipv6_paths = [f"[^{ipv6[0]}]**", *mismatches(ipv6)]
    settings = [f"[^{setting[0]}]**", *mismatches(setting), setting + "?**"]
    proc_exception = (
        "  deny @{PROC}/sys/[^kn]** w,\n"
        f"  deny @{{PROC}}/sys/{{{','.join(paths)}}} w,\n"
        f"  deny @{{PROC}}/sys/net/ipv6/{{{','.join(ipv6_paths)}}} w,\n"
        f"  deny @{{PROC}}/sys/net/ipv6/conf/*/{{{','.join(settings)}}} w,"
    )
    task = task.replace("franky-task", "docker-default")
    task = task.replace("  mount,\n  pivot_root,", "  deny mount,", 1)
    task = task.replace(proc_exception, "  deny @{PROC}/sys/[^k]** w,", 1)

    assert task == default
    assert "unconfined" not in security.TASK_APPARMOR_NAME
    assert "ip_unprivileged_port_start?**" in proc_exception
    assert "disable_ipv6?**" in proc_exception


def test_task_profile_is_default_plus_only_reviewed_exceptions():
    default = json.loads(security.DEFAULT_SECCOMP.read_text())
    task = json.loads(security.TASK_SECCOMP.read_text())
    added = task["syscalls"][-4:]
    del task["syscalls"][-4:]

    assert task == default
    assert added == [
        {
            "names": sorted(REVIEWED_TASK_EXCEPTIONS),
            "action": "SCMP_ACT_ALLOW",
        },
        {
            "names": ["clone"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0x6E000000, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}],
            "excludes": {"arches": ["s390", "s390x"]},
        },
        {
            "names": ["unshare"],
            "action": "SCMP_ACT_ALLOW",
            "args": [
                {
                    "index": 0,
                    "value": (~0x7E020600) & 0xFFFFFFFFFFFFFFFF,
                    "valueTwo": 0,
                    "op": "SCMP_CMP_MASKED_EQ",
                }
            ],
        },
        {"names": ["keyctl"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
    ]


def test_all_packaged_security_files_are_declared_as_package_data():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    package_data = set(project["tool"]["setuptools"]["package-data"]["franky"])

    assert {path.name for path in security.SECURITY_FILES} <= package_data
    assert all(path.is_file() for path in security.SECURITY_FILES)


def test_task_and_non_task_builders_select_explicit_profiles():
    task = container.build_docker_argv(
        "franky", {}, ["pi"], apparmor_profile=security.TASK_APPARMOR_NAME
    )
    proxy = container.build_proxy_argv("franky-proxy", "proxy", [".github.com"])
    auth = container.build_codex_auth_scrub_argv(
        "franky", require_auth=False, auth_volume="franky-codex-auth"
    )
    helper = container._storage_helper_argv("task", "franky", ["a", "b", "c"], 5)

    assert _seccomp_arg(security.TASK_SECCOMP) in task
    assert _apparmor_arg(security.TASK_APPARMOR_NAME) in task
    for argv in (proxy, auth, helper):
        assert _seccomp_arg(security.DEFAULT_SECCOMP) in argv
        assert _seccomp_arg(security.TASK_SECCOMP) not in argv
    assert not any(
        "seccomp=unconfined" in token for argv in (task, proxy, auth, helper) for token in argv
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["name=apparmor", "name=seccomp,profile=builtin"], "franky-task"),
        (["name=seccomp,profile=unconfined", "name=cgroupns"], None),
    ],
)
def test_select_task_apparmor_uses_only_reported_daemon_support(options, expected):
    def runner(argv, **kwargs):
        assert argv == ["docker", "info", "--format", "{{json .SecurityOptions}}"]
        assert kwargs["timeout"] == 3
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(options), stderr="")

    assert security.select_task_apparmor(runner) == expected


@pytest.mark.parametrize("stdout", ["", "{}", '["name=apparmor", 1]', "not-json"])
def test_select_task_apparmor_fails_closed_on_malformed_daemon_output(stdout):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    with pytest.raises(security.SecurityPolicyError, match="security options"):
        security.select_task_apparmor(runner)


def test_select_task_apparmor_fails_closed_when_docker_info_fails():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unavailable")

    with pytest.raises(security.SecurityPolicyError, match="security options"):
        security.select_task_apparmor(runner)


def test_storage_preflight_selects_explicit_default_profile():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        output = "0\n4194304\n" if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    assert container._storage_sample(None, "franky", runner) == (0, 4194304)
    run = next(argv for argv in calls if argv[:2] == ["docker", "run"])
    assert _seccomp_arg(security.DEFAULT_SECCOMP) in run


def test_footprint_version_probe_selects_explicit_default_profile(monkeypatch):
    footprint = _load_footprint()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="version", stderr="")

    monkeypatch.setattr(footprint.subprocess, "run", runner)
    footprint.probe_image("franky", ["pi", "--version"])

    assert _seccomp_arg(security.DEFAULT_SECCOMP) in calls[0]
