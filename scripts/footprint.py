#!/usr/bin/env python3
"""Measure and enforce Franky's credential-free resource footprint."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECCOMP = ROOT / "franky/security-default.json"
FIXTURE_VERSION = "1"
HOST_WORKLOADS = ("parser", "redaction", "profile", "snapshot")
FULL = {"host", "images", "runtime"}
ALL_IMAGES = {"all", "pi", "claude", "codex", "opencode", "proxy"}


class FootprintError(ValueError):
    """A required metric or budget check failed."""


def select_checks(paths: list[str]) -> set[str]:
    """Select bounded work. Unknown paths fail closed to the full gate."""
    if paths and all(path.endswith((".md", ".rst")) for path in paths):
        return {"policy"}
    if not paths or any(
        path in {"Dockerfile", "pyproject.toml", "Makefile"}
        or path.startswith(("proxy/", ".github/workflows/", "scripts/footprint"))
        or path
        in {
            "scripts/footprint-budgets.json",
            "scripts/smoke-memory.py",
            "tests/test_footprint.py",
            "tests/test_smoke_memory.py",
        }
        for path in paths
    ):
        return set(FULL)
    if all(path.startswith(("franky/", "tests/", "scripts/")) for path in paths):
        return {"host", "runtime"}
    return set(FULL)


def select_variants(paths: list[str]) -> set[str]:
    """Return only the images needed by the selected checks."""
    if not paths:
        return set(ALL_IMAGES)
    if all(path.endswith((".md", ".rst")) for path in paths):
        return set()
    if all(path.startswith("proxy/") or path.endswith((".md", ".rst")) for path in paths):
        return {"pi", "proxy"}
    if any(
        path in {"Dockerfile", "pyproject.toml", "Makefile"}
        or path.startswith((".github/workflows/", "scripts/footprint"))
        or path
        in {
            "scripts/footprint-budgets.json",
            "scripts/smoke-memory.py",
            "tests/test_footprint.py",
            "tests/test_smoke_memory.py",
        }
        for path in paths
    ):
        return set(ALL_IMAGES)
    if all(path.startswith(("franky/", "tests/", "scripts/")) for path in paths):
        return {"pi", "proxy"}
    return set(ALL_IMAGES)


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FootprintError(f"missing or malformed {label}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise FootprintError(f"missing or malformed {label}")
    return value


def _sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not any(character not in "0123456789abcdef" for character in value)
    )


def validate_host_report(report: dict, repeats: int = 3, required: set[str] | None = None) -> None:
    if report.get("schema") != 1 or not isinstance(report.get("metadata"), dict):
        raise FootprintError("missing or malformed host metadata")
    metadata = report["metadata"]
    for key in ("architecture", "python", "dependencies", "fixture"):
        if not metadata.get(key):
            raise FootprintError(f"missing or malformed metadata.{key}")
    host = report.get("host")
    if not isinstance(host, dict) or not host:
        raise FootprintError("missing or malformed host metrics")
    for name in required or ():
        if name not in host:
            raise FootprintError(f"host report is missing {name}")
    for name, metrics in host.items():
        if not isinstance(metrics, dict):
            raise FootprintError(f"missing or malformed host.{name}")
        for key in ("cpu_seconds", "elapsed_seconds", "throughput_per_second"):
            values = metrics.get(key)
            if not isinstance(values, list) or len(values) < repeats:
                raise FootprintError(f"missing or malformed host.{name}.{key}")
            for value in values:
                _number(value, f"host.{name}.{key}")
        _number(metrics.get("peak_mib"), f"host.{name}.peak_mib")


def compare_host_reports(base: dict, head: dict, budgets: dict) -> dict:
    """Enforce absolute caps plus meaningful median CPU regressions."""
    repeats = int(budgets.get("cpu", {}).get("minimum_repeats", 3))
    required = set(budgets.get("host", {}))
    validate_host_report(base, repeats, required)
    validate_host_report(head, repeats, required)
    comparable = ("architecture", "python", "dependencies", "fixture")
    if any(base["metadata"][key] != head["metadata"][key] for key in comparable):
        raise FootprintError("dependency drift makes the relative comparison unresolved")
    cpu_budget = budgets.get("cpu", {})
    ratio_limit = _number(cpu_budget.get("maximum_ratio"), "cpu.maximum_ratio")
    delta_limit = _number(cpu_budget.get("minimum_delta_seconds"), "cpu.minimum_delta_seconds")
    results = {}
    for name, head_metrics in head["host"].items():
        if name not in base["host"]:
            raise FootprintError(f"base report is missing host.{name}")
        limits = budgets.get("host", {}).get(name)
        if not isinstance(limits, dict):
            raise FootprintError(f"budget is missing host.{name}")
        head_cpu = statistics.median(head_metrics["cpu_seconds"])
        base_cpu = statistics.median(base["host"][name]["cpu_seconds"])
        if head_cpu > _number(limits.get("maximum_cpu_seconds"), f"host.{name}.CPU cap"):
            raise FootprintError(f"{name} CPU exceeds its absolute budget")
        if head_metrics["peak_mib"] > _number(
            limits.get("maximum_peak_mib"), f"host.{name}.memory cap"
        ):
            raise FootprintError(f"{name} memory exceeds its absolute budget")
        head_elapsed = statistics.median(head_metrics["elapsed_seconds"])
        if head_elapsed > _number(
            limits.get("maximum_elapsed_seconds"), f"host.{name}.elapsed cap"
        ):
            raise FootprintError(f"{name} elapsed time exceeds its absolute budget")
        head_throughput = statistics.median(head_metrics["throughput_per_second"])
        if head_throughput < _number(
            limits.get("minimum_throughput_per_second"),
            f"host.{name}.throughput floor",
        ):
            raise FootprintError(f"{name} throughput is below its absolute budget")
        delta = head_cpu - base_cpu
        ratio = head_cpu / base_cpu if base_cpu else math.inf
        if delta > delta_limit and ratio > ratio_limit:
            raise FootprintError(f"{name} CPU regressed: {base_cpu:.6f}s -> {head_cpu:.6f}s")
        results[name] = {
            "status": "pass",
            "base_cpu_seconds": base_cpu,
            "head_cpu_seconds": head_cpu,
            "ratio": ratio,
            "delta_seconds": delta,
        }
    return results


def check_images(report: dict, budgets: dict) -> None:
    if report.get("schema") != 1:
        raise FootprintError("missing or malformed image report schema")
    architecture = report.get("metadata", {}).get("architecture")
    limits = budgets.get("images", {}).get(architecture)
    if not isinstance(limits, dict):
        raise FootprintError(f"image budgets are missing for architecture {architecture!r}")
    images = report.get("images")
    if not isinstance(images, dict) or not images:
        raise FootprintError("missing image metrics")
    for name, metrics in images.items():
        if name not in limits:
            raise FootprintError(f"budget is missing for {name} image")
        size = _number(metrics.get("bytes"), f"images.{name}.bytes")
        layers = metrics.get("layers")
        if (
            not isinstance(layers, list)
            or not layers
            or not all(isinstance(x, str) for x in layers)
        ):
            raise FootprintError(f"missing or malformed images.{name}.layers")
        if size > _number(limits[name], f"images.{name} budget"):
            raise FootprintError(f"{name} image exceeds its architecture budget")


def check_runtime(
    report: dict,
    budgets: dict,
    allow_historical_helper: bool = False,
    enforce_budgets: bool = True,
) -> None:
    if report.get("schema") != 1 or not isinstance(report.get("metadata"), dict):
        raise FootprintError("missing or malformed runtime report")
    metadata = report["metadata"]
    for key in ("architecture", "python", "source_sha"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise FootprintError(f"missing or malformed runtime metadata.{key}")
    sandbox = metadata.get("sandbox")
    if (
        not isinstance(sandbox, dict)
        or sandbox.get("mode") != "algorithm_comparison_shared_security"
        or not isinstance(sandbox.get("compatibility_applied"), bool)
    ):
        raise FootprintError("missing or malformed runtime metadata.sandbox")
    profiles = sandbox.get("profile_sha256")
    if not isinstance(profiles, dict) or set(profiles) != {"default", "task"}:
        raise FootprintError("missing or malformed runtime metadata.sandbox.profile_sha256")
    for digest in profiles.values():
        if not _sha256(digest):
            raise FootprintError("missing or malformed runtime metadata.sandbox.profile_sha256")
    apparmor = sandbox.get("apparmor")
    if not isinstance(apparmor, dict) or set(apparmor) != {"profile", "sha256"}:
        raise FootprintError("missing or malformed runtime metadata.sandbox.apparmor")
    apparmor_profile = apparmor["profile"]
    apparmor_sha256 = apparmor["sha256"]
    if not (
        (apparmor_profile is None and apparmor_sha256 is None)
        or (isinstance(apparmor_profile, str) and apparmor_profile and _sha256(apparmor_sha256))
    ):
        raise FootprintError("missing or malformed runtime metadata.sandbox.apparmor")
    for name in ("task", "proxy"):
        image = metadata.get("images", {}).get(name)
        if (
            not isinstance(image, dict)
            or not isinstance(image.get("id"), str)
            or not image["id"]
            or not isinstance(image.get("base_layer"), str)
            or not image["base_layer"]
            or not isinstance(image.get("dependencies"), dict)
            or not image["dependencies"]
            or not isinstance(image.get("layers"), list)
            or not image["layers"]
        ):
            raise FootprintError(f"missing or malformed runtime metadata.images.{name}")
    jobs = report.get("jobs")
    limits = budgets.get("runtime", {}).get(str(jobs))
    if not isinstance(limits, dict):
        raise FootprintError(f"runtime budgets are missing for {jobs} jobs")
    direct = {
        "minimum_vm_available_percent": report.get("minimum_vm_available_percent"),
        "maximum_elapsed_seconds": report.get("elapsed_seconds"),
        "minimum_files_per_second": report.get("throughput", {}).get("files_per_second"),
        "minimum_write_mib_per_second": report.get("throughput", {}).get("write_mib_per_second"),
        "maximum_children_cpu_seconds": report.get("rusage", {}).get("children_cpu_seconds"),
        "maximum_self_cpu_seconds": report.get("rusage", {}).get("self_cpu_seconds"),
    }
    _number(report.get("rusage", {}).get("self_cpu_seconds"), "self_cpu_seconds")
    daemon = report.get("native_daemon_cpu")
    if not isinstance(daemon, dict) or daemon.get("status") not in {
        "measured",
        "unavailable_docker_desktop",
    }:
        raise FootprintError("missing or malformed native_daemon_cpu")
    if daemon["status"] == "measured":
        daemon_cpu = _number(daemon.get("cpu_seconds"), "native_daemon_cpu.cpu_seconds")
        if enforce_budgets and daemon_cpu > _number(
            limits.get("maximum_native_daemon_cpu_seconds"),
            f"runtime.{jobs}.maximum_native_daemon_cpu_seconds",
        ):
            raise FootprintError("native daemon CPU exceeds budget")
    elif daemon.get("cpu_seconds") is not None:
        raise FootprintError("unavailable native daemon CPU must be null")
    for budget_name, measured in direct.items():
        value = _number(measured, budget_name.removeprefix("minimum_").removeprefix("maximum_"))
        limit = _number(limits.get(budget_name), f"runtime.{jobs}.{budget_name}")
        if enforce_budgets and budget_name.startswith("minimum_") and value < limit:
            raise FootprintError(f"{budget_name.removeprefix('minimum_')} is below budget")
        if enforce_budgets and budget_name.startswith("maximum_") and value > limit:
            raise FootprintError(f"{budget_name.removeprefix('maximum_')} exceeds budget")
    scopes = report.get("scopes")
    if not isinstance(scopes, dict):
        raise FootprintError("missing runtime scopes")
    for name in ("task", "proxy", "helper"):
        metrics = scopes.get(name)
        scope_limits = limits.get("scopes", {}).get(name)
        if not isinstance(metrics, dict) or not isinstance(scope_limits, dict):
            raise FootprintError(f"missing {name} metrics or budget")
        if name == "helper" and metrics.get("status") == "unavailable_historical":
            if not allow_historical_helper or any(
                metrics.get(key) is not None
                for key in ("cpu_seconds", "peak_mib", "oom_kills", "peak_kind")
            ):
                raise FootprintError("malformed historical helper metrics")
            continue
        cpu = _number(metrics.get("cpu_seconds"), f"{name}.cpu_seconds")
        peak = _number(metrics.get("peak_mib"), f"{name}.peak_mib")
        oom = _number(metrics.get("oom_kills"), f"{name}.oom_kills")
        if metrics.get("peak_kind") not in {"cgroup", "sampled"}:
            raise FootprintError(f"missing or malformed {name}.peak_kind")
        if oom:
            raise FootprintError(f"{name} OOM")
        if enforce_budgets and cpu > _number(
            scope_limits.get("maximum_cpu_seconds"), f"{name} CPU budget"
        ):
            raise FootprintError(f"{name} CPU exceeds budget")
        if enforce_budgets and peak > _number(
            scope_limits.get("maximum_peak_mib"), f"{name} memory budget"
        ):
            raise FootprintError(f"{name} memory exceeds budget")


def compare_runtime_reports(base: list[dict], head: list[dict], budgets: dict) -> dict:
    repeats = int(budgets.get("cpu", {}).get("minimum_repeats", 3))
    if len(base) != len(head) or len(base) < repeats:
        raise FootprintError(f"runtime comparison requires at least {repeats} repeats")
    for report in base:
        check_runtime(
            report,
            budgets,
            allow_historical_helper=True,
            enforce_budgets=False,
        )
    for report in head:
        check_runtime(report, budgets)
    # Reports arrive from several runners; pair drift would silently compare the wrong runs.
    if len({report["jobs"] for report in base + head}) != 1:
        raise FootprintError("job count drift makes runtime comparison unresolved")
    for side, reports in (("base", base), ("head", head)):
        if len({report["metadata"]["source_sha"] for report in reports}) != 1:
            raise FootprintError(f"{side} source drift makes runtime comparison unresolved")
    sandboxes = [report["metadata"]["sandbox"] for report in base + head]
    signatures = {
        (
            sandbox["mode"],
            tuple(sorted(sandbox["profile_sha256"].items())),
            tuple(sorted(sandbox["apparmor"].items())),
        )
        for sandbox in sandboxes
    }
    if len(signatures) != 1:
        raise FootprintError("shared security profile drift makes runtime comparison unresolved")
    if len({report["metadata"]["sandbox"]["compatibility_applied"] for report in base}) != 1:
        raise FootprintError(
            "base compatibility metadata drift makes runtime comparison unresolved"
        )
    if any(report["metadata"]["sandbox"]["compatibility_applied"] for report in head):
        raise FootprintError("head runtime required compatibility adaptation")
    for base_report, head_report in zip(base, head, strict=True):
        if base_report["metadata"]["architecture"] != head_report["metadata"]["architecture"]:
            raise FootprintError("architecture drift makes runtime comparison unresolved")
        if base_report["metadata"]["python"] != head_report["metadata"]["python"]:
            raise FootprintError("Python drift makes runtime comparison unresolved")
        for image in ("task", "proxy"):
            before = base_report["metadata"]["images"][image]
            after = head_report["metadata"]["images"][image]
            if (
                before["dependencies"] != after["dependencies"]
                or before["base_layer"] != after["base_layer"]
            ):
                raise FootprintError(
                    f"{image} version or base-layer drift makes runtime comparison unresolved"
                )
    ratio_limit = _number(budgets["cpu"].get("maximum_ratio"), "cpu.maximum_ratio")
    delta_limit = _number(budgets["cpu"].get("minimum_delta_seconds"), "cpu.minimum_delta_seconds")
    extractors = {
        "task": lambda report: report["scopes"]["task"]["cpu_seconds"],
        "proxy": lambda report: report["scopes"]["proxy"]["cpu_seconds"],
        "self": lambda report: report["rusage"]["self_cpu_seconds"],
        "children": lambda report: report["rusage"]["children_cpu_seconds"],
    }
    if all(report["native_daemon_cpu"]["status"] == "measured" for report in base + head):
        extractors["native_daemon"] = lambda report: report["native_daemon_cpu"]["cpu_seconds"]
    results = {"helper": {"status": "absolute_only"}}
    if all(report["scopes"]["helper"].get("status") != "unavailable_historical" for report in base):
        extractors["helper"] = lambda report: report["scopes"]["helper"]["cpu_seconds"]
    # base[i] and head[i] ran on the same runner. Paired medians cancel runner speed.
    for name, extract in extractors.items():
        pairs = [(extract(b), extract(h)) for b, h in zip(base, head, strict=True)]
        base_cpu = statistics.median(b for b, _ in pairs)
        head_cpu = statistics.median(h for _, h in pairs)
        delta = statistics.median(h - b for b, h in pairs)
        ratio = statistics.median(h / b if b else math.inf for b, h in pairs)
        if delta > delta_limit and ratio > ratio_limit:
            raise FootprintError(f"{name} CPU regressed: {base_cpu:.6f}s -> {head_cpu:.6f}s")
        results[name] = {
            "status": "pass",
            "base_cpu_seconds": base_cpu,
            "head_cpu_seconds": head_cpu,
            "ratio": ratio,
            "delta_seconds": delta,
        }
    return results


def _rusage() -> dict[str, float]:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "self_cpu_seconds": own.ru_utime + own.ru_stime,
        "children_cpu_seconds": children.ru_utime + children.ru_stime,
    }


def _maxrss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024**2 if platform.system() == "Darwin" else peak / 1024


def _metadata(root: Path, source_sha: str | None = None) -> dict:
    dependencies = {}
    for name in ("click", "tomli"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "stdlib"
    sha = (
        source_sha
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return {
        "source_sha": sha,
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "dependencies": dependencies,
        "fixture": FIXTURE_VERSION,
    }


def _load_modules(root: Path):
    sys.path.insert(0, str(root))
    from franky.config import redact
    from franky.economics import parse_usage
    from franky.engine import PiEngine
    from franky.profile import ProfileSpec, build_bundle
    from franky.snapshot import verify_no_secrets

    return redact, parse_usage, PiEngine, ProfileSpec, build_bundle, verify_no_secrets


def benchmark_host(root: Path, repeats: int = 3, source_sha: str | None = None) -> dict:
    """Run fixed, credential-free production-code fixtures."""
    redact, parse_usage, PiEngine, ProfileSpec, build_bundle, verify_no_secrets = _load_modules(
        root
    )
    parser_data = (
        "ordinary output without a URL\n" * 250_000
        + '{"type":"log","message":"no URL"}\n' * 50_000
        + '{"type":"result","url":"https://github.com/smoke/example/pull/42"}\n'
    )
    redaction_data = ("plain text token-safe value\n" * 100_000) + "known-secret"
    with tempfile.TemporaryDirectory(prefix="franky-footprint-") as temp:
        temp_root = Path(temp)
        profile_files = []
        snapshot_root = temp_root / "snapshot"
        snapshot_root.mkdir()
        for index in range(1_000):
            profile_path = temp_root / f"profile-{index}.txt"
            profile_path.write_text("safe operator profile\n", encoding="utf-8")
            profile_files.append(profile_path)
            (snapshot_root / f"source-{index}.txt").write_text(
                "safe source data\n", encoding="utf-8"
            )
        workloads = {
            "parser": (
                lambda: (
                    parse_usage(parser_data),
                    PiEngine().parse_pr_url(parser_data, repo="smoke/example"),
                ),
                300_001,
            ),
            "redaction": (lambda: redact(redaction_data, ["known-secret"]), 100_001),
            "profile": (lambda: build_bundle(ProfileSpec(instructions=profile_files)), 1_000),
            "snapshot": (lambda: verify_no_secrets(snapshot_root, ["known-secret"]), 1_000),
        }
        measured = {}
        overall_before = _rusage()
        for name, (workload, units) in workloads.items():
            cpu_values = []
            elapsed_values = []
            throughput = []
            process_peak = 0.0
            for _ in range(repeats):
                before = time.process_time()
                started = time.perf_counter()
                workload()
                elapsed = time.perf_counter() - started
                cpu = time.process_time() - before
                cpu_values.append(cpu)
                elapsed_values.append(elapsed)
                throughput.append(units / elapsed)
                process_peak = max(process_peak, _maxrss_mib())
            measured[name] = {
                "cpu_seconds": cpu_values,
                "elapsed_seconds": elapsed_values,
                "throughput_per_second": throughput,
                "peak_mib": process_peak,
            }
        overall_after = _rusage()
    return {
        "schema": 1,
        "metadata": _metadata(root, source_sha),
        "host": measured,
        "rusage": {key: overall_after[key] - overall_before[key] for key in overall_before},
    }


def probe_image(image: str, command: list[str] | tuple[str, ...]):
    """Bound version probes, including the container after a client timeout."""
    name = f"franky-footprint-version-{uuid.uuid4().hex[:12]}"
    try:
        return subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--security-opt=seccomp={DEFAULT_SECCOMP}",
                "--pids-limit=64",
                "--memory=256m",
                "--memory-swap=256m",
                "--tmpfs",
                "/tmp:size=16m",
                "--tmpfs",
                "/home/franky:size=16m,uid=1001,gid=1001",
                f"--entrypoint={command[0]}",
                image,
                *command[1:],
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    finally:
        reaped = subprocess.run(
            ["docker", "rm", "-f", "-v", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if reaped.returncode and f"no such container: {name}" not in reaped.stderr.lower():
            raise FootprintError(f"could not remove version probe {name}")


def measure_images(images: dict[str, str]) -> dict:
    measured = {}
    architectures = set()
    version_commands = {
        "pi": ("pi", "--version"),
        "claude": ("claude", "--version"),
        "codex": ("codex", "--version"),
        "opencode": ("opencode", "--version"),
        "proxy": ("squid", "-v"),
    }
    for name, tag in images.items():
        raw = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout
        inspected = json.loads(raw)[0]
        architectures.add(inspected["Architecture"])
        commands = (
            ((tool, command) for tool, command in version_commands.items() if tool != "proxy")
            if name == "all"
            else [(name, version_commands[name])]
        )
        versions = {}
        for tool, command in commands:
            proc = probe_image(tag, command)
            versions[tool] = (proc.stdout or proc.stderr).splitlines()[0].strip()
        measured[name] = {
            "tag": tag,
            "id": inspected["Id"],
            "bytes": inspected["Size"],
            "layers": inspected["RootFS"]["Layers"],
            # Current final images have one base-image layer. Keep full layers for audit.
            "base_layer": inspected["RootFS"]["Layers"][0],
            "versions": versions,
        }
    layer_owners = {}
    for name, image in measured.items():
        for layer in image["layers"]:
            layer_owners.setdefault(layer, []).append(name)
    if len(architectures) != 1:
        raise FootprintError("image architecture drift makes size comparison unresolved")
    metadata = _metadata(ROOT)
    metadata["architecture"] = architectures.pop()
    return {
        "schema": 1,
        "metadata": metadata,
        "images": measured,
        "layer_accounting": {
            "logical_sizes_are_not_host_disk_usage": True,
            "unique_layer_ids": len(layer_owners),
            "shared_layers": {
                layer: owners for layer, owners in layer_owners.items() if len(owners) > 1
            },
        },
    }


def _read_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FootprintError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FootprintError(f"{path} must contain a JSON object")
    return value


def _write_json(path: str, value: dict) -> None:
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("paths", nargs="*")
    variants = sub.add_parser("variants")
    variants.add_argument("paths", nargs="*")
    host = sub.add_parser("host")
    host.add_argument("--repo", type=Path, default=ROOT)
    host.add_argument("--repeats", type=int, default=3)
    host.add_argument("--source-sha")
    host.add_argument("--output", required=True)
    compare = sub.add_parser("compare-host")
    compare.add_argument("--base", required=True)
    compare.add_argument("--head", required=True)
    compare.add_argument("--budgets", required=True)
    images = sub.add_parser("images")
    images.add_argument("--image", action="append", required=True, metavar="NAME=TAG")
    images.add_argument("--budgets", required=True)
    images.add_argument("--output", required=True)
    runtime = sub.add_parser("check-runtime")
    runtime.add_argument("--report", action="append", required=True)
    runtime.add_argument("--budgets", required=True)
    runtime_compare = sub.add_parser("compare-runtime")
    runtime_compare.add_argument("--base", action="append", required=True)
    runtime_compare.add_argument("--head", action="append", required=True)
    runtime_compare.add_argument("--budgets", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            print(",".join(sorted(select_checks(args.paths))))
        elif args.command == "variants":
            print(",".join(sorted(select_variants(args.paths))))
        elif args.command == "host":
            if args.repeats < 3:
                raise FootprintError("host benchmarks require at least 3 repeats")
            _write_json(
                args.output,
                benchmark_host(args.repo.resolve(), args.repeats, args.source_sha),
            )
        elif args.command == "compare-host":
            result = compare_host_reports(
                _read_json(args.base), _read_json(args.head), _read_json(args.budgets)
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "images":
            mapping = dict(item.split("=", 1) for item in args.image)
            result = measure_images(mapping)
            check_images(result, _read_json(args.budgets))
            _write_json(args.output, result)
        elif args.command == "check-runtime":
            budgets = _read_json(args.budgets)
            for path in args.report:
                check_runtime(_read_json(path), budgets)
        elif args.command == "compare-runtime":
            result = compare_runtime_reports(
                [_read_json(path) for path in args.base],
                [_read_json(path) for path in args.head],
                _read_json(args.budgets),
            )
            print(json.dumps(result, indent=2, sort_keys=True))
    except (FootprintError, ValueError, subprocess.SubprocessError) as exc:
        print(f"footprint: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
