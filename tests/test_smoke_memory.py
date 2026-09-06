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
            "vm_total_kib": 1000,
            "vm_available_kib": available_kib,
        },
        {
            "time": end,
            "bytes": peak_mib * 1024**2,
            "shmem": 0,
            "oom_kill": oom_kill,
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
