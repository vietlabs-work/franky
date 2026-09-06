"""Hermetic tests for the concurrent memory smoke script."""

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def load_smoke_memory():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-memory.py"
    spec = importlib.util.spec_from_file_location("smoke_memory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


smoke_memory = load_smoke_memory()


def test_shared_security_adapter_changes_only_docker_policy_options(tmp_path, monkeypatch):
    default_profile = tmp_path / "default.json"
    task_profile = tmp_path / "task.json"
    default_profile.write_bytes(b"default-profile\n")
    task_profile.write_bytes(b"task-profile\n")
    monkeypatch.setattr(smoke_memory, "DEFAULT_SECCOMP", default_profile)
    monkeypatch.setattr(smoke_memory, "TASK_SECCOMP", task_profile)
    calls = []

    def original_popen(argv, *args, **kwargs):
        calls.append((argv, args, kwargs))
        return "process"

    monkeypatch.setattr(subprocess, "Popen", original_popen)
    task_name = "franky-run-abc123"
    command_policy = "--security-opt=seccomp=/command-argument.json"
    task = [
        "docker",
        "run",
        "--rm",
        "--name",
        task_name,
        "--security-opt=seccomp=/old.json",
        "--memory=2g",
        "task-image",
        "command",
        command_policy,
    ]
    helper = [
        "docker",
        "run",
        "--name",
        f"{task_name}-disk",
        "--security-opt",
        "seccomp=/old.json",
        "task-image",
        "sleep",
        "infinity",
    ]
    ordinary = ["docker", "inspect", task_name]

    with smoke_memory._shared_security_profiles(
        {task_name}, {"task-image", "proxy-image"}
    ) as metadata:
        imported_default = subprocess.Popen
        assert imported_default(task, cwd="/tmp") == "process"
        assert subprocess.Popen(helper) == "process"
        assert subprocess.Popen(ordinary) == "process"
        assert imported_default is subprocess.Popen

    assert subprocess.Popen is original_popen
    assert calls == [
        (
            [
                "docker",
                "run",
                "--rm",
                "--name",
                task_name,
                "--memory=2g",
                f"--security-opt=seccomp={task_profile}",
                "task-image",
                "command",
                command_policy,
            ],
            (),
            {"cwd": "/tmp"},
        ),
        (
            [
                "docker",
                "run",
                "--name",
                f"{task_name}-disk",
                f"--security-opt=seccomp={default_profile}",
                "task-image",
                "sleep",
                "infinity",
            ],
            (),
            {},
        ),
        (ordinary, (), {}),
    ]
    assert metadata == {
        "mode": "algorithm_comparison_shared_security",
        "apparmor": {"profile": None, "sha256": None},
        "profile_sha256": {
            "default": "07d81f383304754105b34fc4ddb4684da6e89d1f7964beb085a2d823ce9529e1",
            "task": "31eaf5474b53bce546b5ecae76ff67db4378a7ec34330ce896e65ca9dd1123b2",
        },
        "compatibility_applied": True,
    }


def test_shared_security_adapter_applies_same_task_apparmor_to_base_and_head(tmp_path, monkeypatch):
    task_profile = tmp_path / "task.apparmor"
    task_profile.write_text("profile franky-task {}\n", encoding="utf-8")
    monkeypatch.setattr(smoke_memory, "TASK_APPARMOR", task_profile)
    calls = []

    def original_popen(argv, *args, **kwargs):
        calls.append(argv)
        return "process"

    monkeypatch.setattr(subprocess, "Popen", original_popen)
    task_name = "franky-run-abc123"
    argv = [
        "docker",
        "run",
        "--name",
        task_name,
        "--security-opt=seccomp=/old.json",
        "task-image",
    ]

    with smoke_memory._shared_security_profiles(
        {task_name}, {"task-image"}, task_apparmor="franky-task"
    ) as metadata:
        subprocess.Popen(argv)

    assert "--security-opt=apparmor=franky-task" in calls[0]
    assert metadata["apparmor"] == {
        "profile": "franky-task",
        "sha256": "f3e24ec12a0570dc1c60002d1087033fb5eb6f11ef4a572bb17434b6c91344ca",
    }


@pytest.mark.parametrize("stdout", ["", "{}", '["name=apparmor", 1]', "invalid"])
def test_benchmark_apparmor_detection_fails_closed(stdout, monkeypatch):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(RuntimeError, match="security options"):
        smoke_memory._detect_task_apparmor()


def test_benchmark_reuses_outer_apparmor_selection_when_runtime_supports_it():
    def old_runtime(cfg, argv):
        pass

    def current_runtime(cfg, argv, apparmor_selector=None):
        pass

    assert smoke_memory._apparmor_selector_kwargs(old_runtime, "franky-task") == {}
    kwargs = smoke_memory._apparmor_selector_kwargs(current_runtime, "franky-task")
    assert kwargs["apparmor_selector"](None) == "franky-task"


def test_shared_security_adapter_leaves_matching_policy_unadapted(tmp_path, monkeypatch):
    default_profile = tmp_path / "default.json"
    task_profile = tmp_path / "task.json"
    default_profile.write_text("default", encoding="utf-8")
    task_profile.write_text("task", encoding="utf-8")
    monkeypatch.setattr(smoke_memory, "DEFAULT_SECCOMP", default_profile)
    monkeypatch.setattr(smoke_memory, "TASK_SECCOMP", task_profile)
    calls = []

    def original_popen(argv, *args, **kwargs):
        calls.append(argv)
        return "process"

    monkeypatch.setattr(subprocess, "Popen", original_popen)
    argv = [
        "docker",
        "run",
        "--name",
        "franky-run-abc123",
        f"--security-opt=seccomp={task_profile}",
        "task-image",
        "command",
    ]
    with smoke_memory._shared_security_profiles(
        {"franky-run-abc123"}, {"task-image", "proxy-image"}
    ) as metadata:
        subprocess.Popen(argv)

    assert calls == [argv]
    assert metadata["compatibility_applied"] is False


def test_runtime_imports_fail_when_cached_franky_is_not_selected_repo(tmp_path, monkeypatch):
    cached = types.ModuleType("franky")
    cached.__file__ = str(smoke_memory.ROOT / "franky/__init__.py")
    monkeypatch.setitem(sys.modules, "franky", cached)

    with pytest.raises(RuntimeError, match="selected repository"):
        smoke_memory._load_runtime_modules(tmp_path)


def test_workload_checks_nested_docker_before_using_resources(monkeypatch):
    class CheckedDocker(Exception):
        pass

    def check(argv, **kwargs):
        assert argv == ["docker", "info"]
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 5
        raise CheckedDocker

    def unexpected_write(*args, **kwargs):
        pytest.fail("workload started before the nested Docker check")

    monkeypatch.setattr(subprocess, "run", check)
    monkeypatch.setattr(os, "mkdir", unexpected_write)
    with pytest.raises(CheckedDocker):
        exec(smoke_memory.WORKLOAD, {})


@pytest.mark.parametrize(
    "log", [b"old" * 3000 + b"namespace denied", None], ids=["bounded", "missing"]
)
def test_workload_reports_bounded_daemon_log_on_docker_failure(monkeypatch, capsys, log):
    failure = subprocess.CalledProcessError(1, ["docker", "info"])

    def fail(*args, **kwargs):
        raise failure

    def open_log(path, mode):
        assert str(path) == "/tmp/dockerd.log"
        assert mode == "rb"
        if log is None:
            raise FileNotFoundError
        return io.BytesIO(log)

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(Path, "open", open_log)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        exec(smoke_memory.WORKLOAD, {})
    assert exc.value is failure
    output = capsys.readouterr().out
    assert output == (log[-4096:].decode() + "\n" if log else "dockerd log unavailable\n")


@pytest.mark.parametrize("jobs", ["0", "9"])
def test_smoke_memory_rejects_job_counts_outside_bounded_range(jobs, monkeypatch):
    monkeypatch.setattr("sys.argv", ["smoke-memory.py", "--jobs", jobs])

    with pytest.raises(SystemExit) as exc:
        smoke_memory.main()

    assert exc.value.code == 2


def _samples(start, end, peak_mib, available_kib, oom_kill=0):
    return [
        {
            "time": start,
            "bytes": peak_mib * 1024**2,
            "shmem": 0,
            "oom_kill": oom_kill,
            "cpu_usage_usec": 100_000,
            "peak_bytes": peak_mib * 1024**2,
            "peak_kind": "cgroup",
            "vm_total_kib": 1000,
            "vm_available_kib": available_kib,
        },
        {
            "time": end,
            "bytes": peak_mib * 1024**2,
            "shmem": 0,
            "oom_kill": oom_kill,
            "cpu_usage_usec": 200_000,
            "peak_bytes": peak_mib * 1024**2,
            "peak_kind": "cgroup",
            "vm_total_kib": 1000,
            "vm_available_kib": available_kib,
        },
    ]


def test_smoke_memory_summary_uses_every_job_result():
    results = [
        _samples(0, 4, 301, 400),
        _samples(1, 5, 302, 300),
        _samples(2, 6, 303, 210),
    ]

    assert smoke_memory.summarize_results(results) == {
        "jobs": 3,
        "peak_task_mib": [301.0, 302.0, 303.0],
        "minimum_vm_available_percent": 21.0,
        "oom_kills": 0,
    }


def test_smoke_memory_summary_detects_oom_in_later_job():
    results = [
        _samples(0, 4, 301, 400),
        _samples(1, 5, 302, 300),
        _samples(2, 6, 303, 210, oom_kill=1),
    ]

    with pytest.raises(AssertionError, match="task OOM"):
        smoke_memory.summarize_results(results)


def test_smoke_memory_headroom_error_includes_measured_value():
    results = [_samples(0, 4, 301, 190)]

    with pytest.raises(AssertionError, match="19.0% VM headroom"):
        smoke_memory.summarize_results(results)


def _scope(cpu, peak_mib, oom=0):
    return {
        "cpu_usage_usec": cpu,
        "memory_current": peak_mib * 1024**2,
        "memory_peak": peak_mib * 1024**2,
        "peak_kind": "cgroup",
        "oom_kill": oom,
    }


def test_runtime_summary_aggregates_all_jobs_proxy_and_helper():
    results = [_samples(0, 4, 301, 400), _samples(1, 5, 302, 300)]
    scopes = [
        {
            "task": [_scope(1_000_000, 301)],
            "proxy": [_scope(200_000, 20)],
            "helper": [_scope(50_000, 8)],
        },
        {
            "task": [_scope(1_100_000, 302)],
            "proxy": [_scope(250_000, 21)],
            "helper": [_scope(60_000, 9)],
        },
    ]

    summary = smoke_memory.summarize_results(results, scopes)

    assert summary["scopes"]["task"] == {
        "containers": 2,
        "cpu_seconds": 2.1,
        "peak_mib": 302.0,
        "oom_kills": 0,
        "peak_kind": "cgroup",
    }
    assert summary["scopes"]["proxy"]["cpu_seconds"] == 0.45
    assert summary["scopes"]["helper"]["peak_mib"] == 9.0


def test_runtime_summary_rejects_missing_required_scope():
    results = [_samples(0, 4, 301, 400)]

    with pytest.raises(AssertionError, match="helper metrics missing"):
        smoke_memory.summarize_results(
            results,
            [{"task": [_scope(1, 301)], "proxy": [_scope(1, 20)], "helper": []}],
        )


def test_runtime_summary_labels_missing_historical_helper():
    summary = smoke_memory.summarize_results(
        [_samples(0, 4, 301, 400)],
        [{"task": [_scope(1, 301)], "proxy": [_scope(1, 20)], "helper": []}],
        allow_missing_helper=True,
    )

    assert summary["scopes"]["helper"] == {
        "status": "unavailable_historical",
        "cpu_seconds": None,
        "peak_mib": None,
        "oom_kills": None,
        "peak_kind": None,
    }


@pytest.mark.parametrize("scope", ["proxy", "helper"])
def test_runtime_summary_rejects_partially_missing_scope(scope):
    scopes = [{name: [_scope(1, 20), _scope(2, 20)] for name in ("task", "proxy", "helper")}]
    scopes[0][scope].pop()
    with pytest.raises(AssertionError, match=f"{scope} metrics missing"):
        smoke_memory._scope_summary(scopes)


def test_runtime_summary_does_not_treat_partial_historical_helper_as_complete():
    scopes = [{name: [_scope(1, 20), _scope(2, 20)] for name in ("task", "proxy", "helper")}]
    scopes[0]["helper"].pop()
    result = smoke_memory._scope_summary(scopes, allow_missing_helper=True)
    assert result["helper"]["status"] == "unavailable_historical"


def test_runtime_summary_rejects_scope_oom():
    results = [_samples(0, 4, 301, 400)]
    scopes = [
        {
            "task": [_scope(1, 301)],
            "proxy": [_scope(1, 20, oom=1)],
            "helper": [_scope(1, 8)],
        }
    ]

    with pytest.raises(AssertionError, match="proxy OOM"):
        smoke_memory.summarize_results(results, scopes)


def test_parse_cgroup_metrics_labels_sampled_peak_when_memory_peak_is_missing():
    metrics = smoke_memory.parse_cgroup_metrics(
        "usage_usec 123\nmemory.current 456\nmemory.events oom_kill 0\n"
    )

    assert metrics == {
        "cpu_usage_usec": 123,
        "memory_current": 456,
        "memory_peak": 456,
        "peak_kind": "sampled",
        "oom_kill": 0,
    }
