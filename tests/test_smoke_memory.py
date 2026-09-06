"""Hermetic tests for the concurrent memory smoke script."""

import importlib.util
from pathlib import Path

import pytest


def load_smoke_memory():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-memory.py"
    spec = importlib.util.spec_from_file_location("smoke_memory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


smoke_memory = load_smoke_memory()


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
