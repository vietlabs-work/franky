"""Hermetic checks for the opt-in sandbox smoke. Never invoke Docker here."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


def smoke_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-security.py"
    spec = importlib.util.spec_from_file_location("smoke_security", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(uid=1001, caps=0xC0):
    return (
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"CapBnd:\t{caps:016x}\nCapEff:\t0\nCapPrm:\t0\nCapInh:\t0\nCapAmb:\t0\n"
        "Seccomp:\t2\nSeccomp_filters:\t1\n"
    )


@pytest.mark.parametrize(
    "old,new",
    [
        ("Seccomp:\t2", "Seccomp:\t0"),
        ("Seccomp_filters:\t1", "Seccomp_filters:\t0"),
        ("Seccomp_filters:\t1", "Missing:\t1"),
        ("CapBnd:\t00000000000000c0", "CapBnd:\t00000000002000c0"),
        ("CapEff:\t0", "CapEff:\t200000"),
        ("Uid:\t1001", "Uid:\t0"),
    ],
)
def test_status_rejects_missing_filters_root_and_extra_capabilities(old, new):
    smoke = smoke_module()
    with pytest.raises(ValueError):
        smoke.check_status(status().replace(old, new), uid=1001, capabilities=0xC0)


def test_status_accepts_exact_bounding_set_with_empty_effective_set():
    smoke_module().check_status(status(), uid=1001, capabilities=0xC0)


def test_profile_requires_matching_expanded_policy():
    smoke = smoke_module()
    policy = json.loads(smoke.TASK_SECCOMP.read_text())
    smoke.check_profile(["seccomp=" + json.dumps(policy)], smoke.TASK_SECCOMP)
    for options in ([], ["seccomp=unconfined"], ["seccomp={}"]):
        with pytest.raises(ValueError):
            smoke.check_profile(options, smoke.TASK_SECCOMP)


@pytest.mark.parametrize("change", ["writable", "extra", "bind", "wrong-source"])
def test_helper_mounts_reject_anything_except_three_readonly_task_volumes(change):
    smoke = smoke_module()
    mounts = [
        {"Type": "volume", "Name": name, "Destination": f"/data/{name}", "RW": False}
        for name in ("work", "home", "tmp")
    ]
    smoke.check_helper_mounts(mounts, ["work", "home", "tmp"])
    if change == "writable":
        mounts[0]["RW"] = True
    elif change == "extra":
        mounts.append({"Type": "volume", "Name": "auth", "Destination": "/auth", "RW": False})
    elif change == "bind":
        mounts[0]["Type"] = "bind"
    else:
        mounts[0]["Name"] = "auth"
    with pytest.raises(ValueError):
        smoke.check_helper_mounts(mounts, ["work", "home", "tmp"])


def fake_runner(smoke, *, failed_probe=False, missing_filter=False, apparmor=False):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        assert 0 < kwargs["timeout"] <= 60
        output = ""
        if argv[:3] == ["docker", "info", "--format"]:
            output = json.dumps(["name=apparmor"] if apparmor else ["name=seccomp"])
        elif argv[:3] == ["docker", "inspect", "-f"]:
            if argv[3] == "{{json .Mounts}}":
                output = json.dumps(
                    [
                        {
                            "Type": "volume",
                            "Name": name,
                            "Destination": (
                                "/data/" + name.removeprefix("owned-")
                                if argv[-1].endswith("-disk")
                                else destination
                            ),
                            "RW": False,
                        }
                        for name, destination in (
                            ("owned-work", "/work"),
                            ("owned-home", "/home/franky"),
                            ("owned-tmp", "/tmp"),
                        )
                    ]
                )
            else:
                policy = (
                    smoke.DEFAULT_SECCOMP
                    if argv[-1].endswith(("-proxy", "-disk"))
                    else smoke.TASK_SECCOMP
                )
                options = ["seccomp=" + policy.read_text()]
                if apparmor and not argv[-1].endswith(("-proxy", "-disk")):
                    options.append("apparmor=" + smoke.TASK_APPARMOR_NAME)
                output = json.dumps(options)
        elif argv[:2] == ["docker", "exec"]:
            name = argv[2]
            if "cat" in argv:
                if argv[-1] == "/proc/self/attr/current":
                    output = smoke.TASK_APPARMOR_NAME + " (enforce)\n"
                else:
                    uid, caps = (13, 0) if name.endswith("-proxy") else (0, 4)
                    if not name.endswith(("-proxy", "-disk")):
                        uid, caps = 1001, 0xC0
                    output = status(uid, caps)
                if missing_filter:
                    output = output.replace("Seccomp_filters:\t1", "Seccomp_filters:\t0")
            elif "curl" in argv:
                return subprocess.CompletedProcess(argv, 56, stdout="403", stderr="")
            elif "python3" in argv:
                mode = argv[-1]
                output = json.dumps(
                    {
                        "uid": 0 if mode == "nested" else 1001,
                        "status": status(0 if mode == "nested" else 1001),
                        "uid_map": "0 1001 1\n1 100000 65536\n",
                        "results": {
                            "mount": [-1, 1],
                            "unshare": [-1, 1],
                            "keyctl": [-1, 38 if mode == "nested" else 1],
                            "bpf": [-1, 1],
                            "unshare_net": [0, 0],
                            "ip_unprivileged_port_start": [2, 0],
                            "disable_ipv6": [2, 0],
                            "ipv6_forwarding": [-1, 13],
                            "ip_forward": [-1, 13],
                        },
                    }
                )
                if failed_probe:
                    output = output.replace("[-1, 1]", "[0, 0]")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    return runner, calls


@pytest.mark.parametrize("failure", [None, "probe", "filter"])
def test_smoke_uses_production_roles_and_cleans_exact_resources(failure):
    smoke = smoke_module()
    runner, calls = fake_runner(
        smoke, failed_probe=failure == "probe", missing_filter=failure == "filter"
    )
    if failure:
        with pytest.raises(ValueError):
            smoke.run_smoke("task-image", "proxy-image", runner=runner)
    else:
        smoke.run_smoke("task-image", "proxy-image", runner=runner)
    runs = [argv for argv, _ in calls if argv[:2] == ["docker", "run"]]
    assert len(runs) == 3
    assert all(any("seccomp=" in token for token in argv) for argv in runs)
    task = next(argv for argv in runs if "sleep" in argv and "--entrypoint=sleep" not in argv)
    task_name = task[task.index("--name") + 1]
    assert task[task.index("--network") + 1] == "none"
    assert "--privileged" not in " ".join(" ".join(argv) for argv in runs)
    assert not any("GH_TOKEN" in token for argv in runs for token in argv)
    reaps = [argv[-1] for argv, _ in calls if argv[:3] == ["docker", "rm", "-f"]]
    assert reaps == [task_name + "-disk", task_name, task_name + "-proxy"]
    assert ["docker", "volume", "rm", "owned-work", "owned-home", "owned-tmp"] in [
        argv for argv, _ in calls
    ]


def test_start_timeout_still_cleans_named_containers_and_discovered_volumes():
    smoke = smoke_module()
    runner, calls = fake_runner(smoke)

    def timeout(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            calls.append((argv, kwargs))
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return runner(argv, **kwargs)

    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_smoke("task-image", "proxy-image", runner=timeout)
    assert sum(argv[:3] == ["docker", "rm", "-f"] for argv, _ in calls) == 3
    assert any(argv[:3] == ["docker", "volume", "rm"] for argv, _ in calls)


def test_smoke_verifies_enforcing_task_apparmor_and_exact_sysctl_exception():
    smoke = smoke_module()
    runner, calls = fake_runner(smoke, apparmor=True)

    smoke.run_smoke("task-image", "proxy-image", runner=runner)

    task_run = next(
        argv
        for argv, _ in calls
        if argv[:2] == ["docker", "run"] and "sleep" in argv and "--entrypoint=sleep" not in argv
    )
    assert f"--security-opt=apparmor={smoke.TASK_APPARMOR_NAME}" in task_run
    assert any(argv[-1] == "/proc/self/attr/current" for argv, _ in calls)


def test_probe_is_valid_python_and_rejects_unsupported_architecture(monkeypatch):
    smoke = smoke_module()
    monkeypatch.setattr("platform.machine", lambda: "unknown")
    with pytest.raises(ValueError, match="architecture"):
        exec(smoke.PROBE, {})


def test_total_deadline_reserves_time_for_every_owned_resource_cleanup():
    smoke = smoke_module()
    runner, calls = fake_runner(smoke)
    now = [0.0]

    def slow(argv, **kwargs):
        now[0] += kwargs["timeout"]
        return runner(argv, **kwargs)

    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_smoke("task-image", "proxy-image", runner=slow, clock=lambda: now[0])
    assert now[0] <= 90
    assert sum(argv[:3] == ["docker", "rm", "-f"] for argv, _ in calls) == 3
    assert any(argv[:3] == ["docker", "volume", "rm"] for argv, _ in calls)


def test_volume_cleanup_error_with_no_diagnostic_cannot_pass():
    smoke = smoke_module()
    runner, _ = fake_runner(smoke)

    def fail_volume_removal(argv, **kwargs):
        if argv[:3] == ["docker", "volume", "rm"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return runner(argv, **kwargs)

    with pytest.raises(ValueError, match="cleanup failed"):
        smoke.run_smoke("task-image", "proxy-image", runner=fail_volume_removal)


def test_volume_cleanup_accepts_daemon_lowercase_already_absent_error():
    smoke = smoke_module()
    runner, _ = fake_runner(smoke)

    def missing_volumes(argv, **kwargs):
        if argv[:3] == ["docker", "volume", "rm"]:
            errors = "\n".join(
                f"Error response from daemon: remove {name}: no such volume" for name in argv[3:]
            )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=errors)
        return runner(argv, **kwargs)

    smoke.run_smoke("task-image", "proxy-image", runner=missing_volumes)


def test_cleanup_does_not_replace_primary_failure(capsys):
    smoke = smoke_module()
    runner, _ = fake_runner(smoke, failed_probe=True)

    def failed_cleanup(argv, **kwargs):
        if argv[:3] == ["docker", "volume", "rm"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="daemon unavailable")
        return runner(argv, **kwargs)

    with pytest.raises(ValueError, match="mount did not fail"):
        smoke.run_smoke("task-image", "proxy-image", runner=failed_cleanup)
    assert "cleanup failed: task volumes" in capsys.readouterr().err


@pytest.mark.parametrize("hang", [False, True])
def test_readiness_failure_includes_bounded_daemon_log_before_cleanup(hang):
    smoke = smoke_module()
    runner, calls = fake_runner(smoke)
    now = [0.0]
    log = "old" * 2000 + "rootlesskit: namespace denied"

    def not_ready(argv, **kwargs):
        now[0] += 1
        if argv[:2] == ["docker", "exec"] and argv[-2:] == ["docker", "info"]:
            if hang:
                now[0] += kwargs["timeout"]
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not ready")
        if "tail" in argv:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=log, stderr="")
        return runner(argv, **kwargs)

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(ValueError, match="rootlesskit: namespace denied") as failure:
        smoke.run_smoke(
            "task-image", "proxy-image", runner=not_ready, sleeper=sleep, clock=lambda: now[0]
        )
    assert len(str(failure.value)) < 4200
    assert now[0] <= 90
    log_index = next(i for i, (argv, _) in enumerate(calls) if "tail" in argv)
    cleanup_index = next(
        i for i, (argv, _) in enumerate(calls) if argv[:3] == ["docker", "rm", "-f"]
    )
    assert log_index < cleanup_index
    assert calls[log_index][0][-3:] == ["-c", "4096", "/tmp/dockerd.log"]
