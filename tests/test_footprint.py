"""Pure tests for the footprint policy and budget harness."""

import importlib.util
from pathlib import Path
import subprocess

import pytest


def load_footprint():
    path = Path(__file__).resolve().parents[1] / "scripts" / "footprint.py"
    spec = importlib.util.spec_from_file_location("footprint", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


footprint = load_footprint()


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_image_budgets_cover_docker_architecture_names(architecture):
    budgets = footprint._read_json(str(footprint.ROOT / "scripts/footprint-budgets.json"))
    footprint.check_images(
        {
            "schema": 1,
            "metadata": {"architecture": architecture},
            "images": {"codex": {"bytes": 1, "layers": ["sha256:a"]}},
        },
        budgets,
    )


def test_runtime_comparison_checks_helper_when_baseline_is_measurable():
    budgets = footprint._read_json(str(footprint.ROOT / "scripts/footprint-budgets.json"))
    base = [_runtime() for _ in range(3)]
    head = [_runtime() for _ in range(3)]
    for report in head:
        report["scopes"]["helper"]["cpu_seconds"] = 0.3
    with pytest.raises(footprint.FootprintError, match="helper CPU regressed"):
        footprint.compare_runtime_reports(base, head, budgets)


@pytest.mark.parametrize(
    "paths,want",
    [
        (["README.md"], {"policy"}),
        (["proxy/README.md"], {"policy"}),
        (["franky/prompt.py"], {"host", "runtime"}),
        (["Dockerfile"], {"host", "images", "runtime"}),
        (["scripts/footprint.py"], {"host", "images", "runtime"}),
        (["scripts/smoke-memory.py"], {"host", "images", "runtime"}),
        (["mystery.file"], {"host", "images", "runtime"}),
    ],
)
def test_select_checks_fails_safe_for_unknown_paths(paths, want):
    assert footprint.select_checks(paths) == want


@pytest.mark.parametrize(
    "paths,want",
    [
        (["proxy/entrypoint.sh"], {"pi", "proxy"}),
        (["Dockerfile"], {"all", "pi", "claude", "codex", "opencode", "proxy"}),
        (["README.md"], set()),
        (["franky/prompt.py"], {"pi", "proxy"}),
        (["unknown.bin"], {"all", "pi", "claude", "codex", "opencode", "proxy"}),
    ],
)
def test_select_variants_builds_only_affected_images(paths, want):
    assert footprint.select_variants(paths) == want


def test_version_probe_timeout_reaps_its_exact_hardened_container(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 30)
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(footprint.subprocess, "run", runner)
    with pytest.raises(subprocess.TimeoutExpired):
        footprint.probe_image("owned-image", ["pi", "--version"])
    probe, cleanup = calls
    name = probe[probe.index("--name") + 1]
    assert cleanup == ["docker", "rm", "-f", "-v", name]
    assert "/home/franky:size=16m,uid=1001,gid=1001" in probe
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=256m",
        "--memory-swap=256m",
    ):
        assert flag in probe


def _host(cpu):
    return {
        "schema": 1,
        "metadata": {
            "architecture": "arm64",
            "python": "3.12.5",
            "dependencies": {"click": "8.2.1"},
            "fixture": "v1",
        },
        "host": {
            "parser": {
                "cpu_seconds": cpu,
                "elapsed_seconds": [1.0, 1.0, 1.0],
                "throughput_per_second": [100.0, 100.0, 100.0],
                "peak_mib": 12.0,
            }
        },
    }


def test_validate_host_report_rejects_missing_required_metrics():
    report = _host([1.0, 1.0, 1.0])
    del report["host"]["parser"]["throughput_per_second"]

    with pytest.raises(footprint.FootprintError, match="throughput_per_second"):
        footprint.validate_host_report(report, repeats=3)


def test_validate_host_report_rejects_missing_workload():
    report = _host([1.0, 1.0, 1.0])

    with pytest.raises(footprint.FootprintError, match="profile"):
        footprint.validate_host_report(report, repeats=3, required={"parser", "profile"})


def test_compare_host_reports_rejects_real_cpu_regression():
    budgets = {
        "cpu": {"maximum_ratio": 1.15, "minimum_delta_seconds": 0.05},
        "host": {
            "parser": {
                "maximum_cpu_seconds": 2.0,
                "maximum_peak_mib": 32.0,
                "maximum_elapsed_seconds": 2.0,
                "minimum_throughput_per_second": 50.0,
            }
        },
    }

    with pytest.raises(footprint.FootprintError, match="parser CPU regressed"):
        footprint.compare_host_reports(_host([1.0, 1.0, 1.0]), _host([1.3, 1.3, 1.3]), budgets)


def test_compare_host_reports_ignores_noisy_insignificant_cpu_delta():
    budgets = {
        "cpu": {"maximum_ratio": 1.02, "minimum_delta_seconds": 0.05},
        "host": {
            "parser": {
                "maximum_cpu_seconds": 2.0,
                "maximum_peak_mib": 32.0,
                "maximum_elapsed_seconds": 2.0,
                "minimum_throughput_per_second": 50.0,
            }
        },
    }

    result = footprint.compare_host_reports(
        _host([1.0, 1.0, 1.0]), _host([1.03, 1.03, 1.03]), budgets
    )

    assert result["parser"]["status"] == "pass"


def test_compare_host_reports_rejects_dependency_drift():
    head = _host([1.0, 1.0, 1.0])
    head["metadata"]["dependencies"]["click"] = "9.0"

    with pytest.raises(footprint.FootprintError, match="dependency drift"):
        footprint.compare_host_reports(
            _host([1.0, 1.0, 1.0]),
            head,
            {"cpu": {"maximum_ratio": 1.2, "minimum_delta_seconds": 0.05}, "host": {}},
        )


def test_compare_host_reports_rejects_slow_throttled_result():
    budgets = {
        "cpu": {"maximum_ratio": 1.15, "minimum_delta_seconds": 0.05},
        "host": {
            "parser": {
                "maximum_cpu_seconds": 2.0,
                "maximum_peak_mib": 32.0,
                "maximum_elapsed_seconds": 0.9,
                "minimum_throughput_per_second": 110.0,
            }
        },
    }

    with pytest.raises(footprint.FootprintError, match="parser elapsed time"):
        footprint.compare_host_reports(_host([1.0, 1.0, 1.0]), _host([0.5, 0.5, 0.5]), budgets)


def test_check_images_rejects_actual_size_over_architecture_budget():
    report = {
        "schema": 1,
        "metadata": {"architecture": "arm64"},
        "images": {"codex": {"bytes": 121, "layers": ["sha256:a"]}},
    }
    budgets = {"images": {"arm64": {"codex": 120}}}

    with pytest.raises(footprint.FootprintError, match="codex image"):
        footprint.check_images(report, budgets)


def _runtime(jobs=2, helper_peak=8.0):
    return {
        "schema": 1,
        "metadata": {
            "architecture": "arm64",
            "python": "3.12.5",
            "source_sha": "a" * 40,
            "sandbox": {
                "mode": "algorithm_comparison_shared_security",
                "profile_sha256": {"default": "d" * 64, "task": "e" * 64},
                "compatibility_applied": False,
            },
            "images": {
                "task": {
                    "id": "sha256:a",
                    "layers": ["sha256:x"],
                    "base_layer": "sha256:x",
                    "dependencies": {"pi": "1", "python": "3.12"},
                },
                "proxy": {
                    "id": "sha256:b",
                    "layers": ["sha256:y"],
                    "base_layer": "sha256:y",
                    "dependencies": {"squid": "2", "os": "3.23"},
                },
            },
        },
        "jobs": jobs,
        "elapsed_seconds": 20.0,
        "throughput": {"files_per_second": 1000.0, "write_mib_per_second": 50.0},
        "minimum_vm_available_percent": 30.0,
        "scopes": {
            "task": {
                "cpu_seconds": 2.0,
                "peak_mib": 302.0,
                "oom_kills": 0,
                "peak_kind": "cgroup",
            },
            "proxy": {
                "cpu_seconds": 0.2,
                "peak_mib": 20.0,
                "oom_kills": 0,
                "peak_kind": "cgroup",
            },
            "helper": {
                "cpu_seconds": 0.1,
                "peak_mib": helper_peak,
                "oom_kills": 0,
                "peak_kind": "cgroup",
            },
        },
        "rusage": {"self_cpu_seconds": 0.1, "children_cpu_seconds": 0.5},
        "native_daemon_cpu": {"status": "unavailable_docker_desktop", "cpu_seconds": None},
    }


def test_check_runtime_rejects_helper_over_absolute_budget():
    budgets = {
        "runtime": {
            "2": {
                "minimum_vm_available_percent": 20,
                "minimum_files_per_second": 1,
                "minimum_write_mib_per_second": 1,
                "maximum_elapsed_seconds": 90,
                "maximum_children_cpu_seconds": 5,
                "maximum_native_daemon_cpu_seconds": 5,
                "maximum_self_cpu_seconds": 5,
                "scopes": {
                    "task": {"maximum_cpu_seconds": 10, "maximum_peak_mib": 450},
                    "proxy": {"maximum_cpu_seconds": 5, "maximum_peak_mib": 128},
                    "helper": {"maximum_cpu_seconds": 5, "maximum_peak_mib": 16},
                },
            }
        }
    }

    with pytest.raises(footprint.FootprintError, match="helper memory"):
        footprint.check_runtime(_runtime(helper_peak=17), budgets)


def test_check_runtime_rejects_missing_shared_security_metadata():
    report = _runtime()
    del report["metadata"]["sandbox"]
    budgets = footprint._read_json(str(footprint.ROOT / "scripts/footprint-budgets.json"))

    with pytest.raises(footprint.FootprintError, match="sandbox"):
        footprint.check_runtime(report, budgets)


def test_check_runtime_rejects_oom_and_missing_metrics():
    report = _runtime()
    report["scopes"]["proxy"]["oom_kills"] = 1
    budgets = {
        "runtime": {
            "2": {
                "minimum_vm_available_percent": 20,
                "minimum_files_per_second": 1,
                "minimum_write_mib_per_second": 1,
                "maximum_elapsed_seconds": 90,
                "maximum_children_cpu_seconds": 5,
                "maximum_native_daemon_cpu_seconds": 5,
                "maximum_self_cpu_seconds": 5,
                "scopes": {
                    name: {"maximum_cpu_seconds": 10, "maximum_peak_mib": 450}
                    for name in ("task", "proxy", "helper")
                },
            }
        }
    }

    with pytest.raises(footprint.FootprintError, match="proxy OOM"):
        footprint.check_runtime(report, budgets)

    del report["throughput"]["files_per_second"]
    report["scopes"]["proxy"]["oom_kills"] = 0
    with pytest.raises(footprint.FootprintError, match="files_per_second"):
        footprint.check_runtime(report, budgets)


def test_compare_runtime_uses_three_repeat_cpu_medians_and_allows_historical_helper():
    budgets = {
        "cpu": {
            "maximum_ratio": 1.15,
            "minimum_delta_seconds": 0.05,
            "minimum_repeats": 3,
        },
        "runtime": {
            "2": {
                "minimum_vm_available_percent": 20,
                "minimum_files_per_second": 1,
                "minimum_write_mib_per_second": 1,
                "maximum_elapsed_seconds": 90,
                "maximum_self_cpu_seconds": 5,
                "maximum_children_cpu_seconds": 5,
                "maximum_native_daemon_cpu_seconds": 5,
                "scopes": {
                    name: {"maximum_cpu_seconds": 10, "maximum_peak_mib": 450}
                    for name in ("task", "proxy", "helper")
                },
            }
        },
    }
    base = [_runtime() for _ in range(3)]
    head = [_runtime() for _ in range(3)]
    for report in base:
        report["metadata"]["sandbox"]["compatibility_applied"] = True
        report["scopes"]["helper"] = {
            "status": "unavailable_historical",
            "cpu_seconds": None,
            "peak_mib": None,
            "oom_kills": None,
            "peak_kind": None,
        }

    result = footprint.compare_runtime_reports(base, head, budgets)

    assert result["task"]["status"] == "pass"
    assert result["helper"]["status"] == "absolute_only"


def test_compare_runtime_rejects_shared_security_profile_drift():
    budgets = footprint._read_json(str(footprint.ROOT / "scripts/footprint-budgets.json"))
    base = [_runtime() for _ in range(3)]
    head = [_runtime() for _ in range(3)]
    head[1]["metadata"]["sandbox"]["profile_sha256"]["task"] = "f" * 64

    with pytest.raises(footprint.FootprintError, match="shared security profile drift"):
        footprint.compare_runtime_reports(base, head, budgets)


def test_compare_runtime_rejects_adapter_on_head():
    budgets = footprint._read_json(str(footprint.ROOT / "scripts/footprint-budgets.json"))
    base = [_runtime() for _ in range(3)]
    head = [_runtime() for _ in range(3)]
    for report in head:
        report["metadata"]["sandbox"]["compatibility_applied"] = True

    with pytest.raises(footprint.FootprintError, match="head runtime required compatibility"):
        footprint.compare_runtime_reports(base, head, budgets)


def test_compare_runtime_rejects_cpu_regression():
    base = [_runtime() for _ in range(3)]
    head = [_runtime() for _ in range(3)]
    for report in head:
        report["scopes"]["task"]["cpu_seconds"] = 3.0
    budgets = {
        "cpu": {
            "maximum_ratio": 1.15,
            "minimum_delta_seconds": 0.05,
            "minimum_repeats": 3,
        },
        "runtime": {
            "2": {
                "minimum_vm_available_percent": 20,
                "minimum_files_per_second": 1,
                "minimum_write_mib_per_second": 1,
                "maximum_elapsed_seconds": 90,
                "maximum_self_cpu_seconds": 5,
                "maximum_children_cpu_seconds": 5,
                "maximum_native_daemon_cpu_seconds": 5,
                "scopes": {
                    name: {"maximum_cpu_seconds": 10, "maximum_peak_mib": 450}
                    for name in ("task", "proxy", "helper")
                },
            }
        },
    }

    with pytest.raises(footprint.FootprintError, match="task CPU regressed"):
        footprint.compare_runtime_reports(base, head, budgets)
