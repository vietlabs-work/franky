#!/usr/bin/env python3
"""Concurrent production runners, without credentials, model calls, or PRs.

Each holds 256 MiB of process memory, 512 MiB of build data, and 10,000 files. This tests
the runner budget, not the memory needs of an arbitrary repository's test suite.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from footprint import probe_image  # noqa: E402

WORKLOAD = r"""
import json, os, pathlib, time
block = b'x' * (1024 * 1024)
os.mkdir('/work/sources')
for index in range(10000):
    pathlib.Path('/work/sources', str(index)).write_bytes(b'example source file\n')
with open('/work/build-data', 'wb') as target:
    for _ in range(512):
        target.write(block)
    target.flush()
    os.fsync(target.fileno())
    os.posix_fadvise(target.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
held = bytearray(256 * 1024 * 1024)
for _ in range(10):
    root = pathlib.Path('/sys/fs/cgroup')
    stat = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
    events = dict(line.split() for line in (root / 'memory.events').read_text().splitlines())
    cpu = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
    peak_path = root / 'memory.peak'
    peak = int(peak_path.read_text()) if peak_path.exists() else None
    info = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
    print('MEMORY ' + json.dumps({
        'bytes': int((root / 'memory.current').read_text()),
        'anon': int(stat['anon']), 'shmem': int(stat['shmem']),
        'oom_kill': int(events['oom_kill']),
        'cpu_usage_usec': int(cpu['usage_usec']),
        'peak_bytes': peak,
        'peak_kind': 'cgroup' if peak is not None else 'sampled',
        'vm_total_kib': int(info['MemTotal'].split()[0]),
        'vm_available_kib': int(info['MemAvailable'].split()[0]),
        'time': time.time(),
    }), flush=True)
    time.sleep(2)
assert len(held) == 256 * 1024 * 1024
print('WORKLOAD PASSED', flush=True)
"""


def parse_cgroup_metrics(text):
    """Parse the fixed cgroup metric payload. Missing memory.peak is explicit."""
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
        elif len(parts) == 3 and parts[0] == "memory.events" and parts[2].isdigit():
            values[f"memory.events.{parts[1]}"] = int(parts[2])
    required = ("usage_usec", "memory.current", "memory.events.oom_kill")
    if any(key not in values for key in required):
        raise ValueError("malformed cgroup metrics")
    peak = values.get("memory.peak", values["memory.current"])
    return {
        "cpu_usage_usec": values["usage_usec"],
        "memory_current": values["memory.current"],
        "memory_peak": peak,
        "peak_kind": "cgroup" if "memory.peak" in values else "sampled",
        "oom_kill": values["memory.events.oom_kill"],
    }


def read_container_metrics(name):
    script = (
        "cat /sys/fs/cgroup/cpu.stat; "
        "printf 'memory.current '; cat /sys/fs/cgroup/memory.current; "
        "test ! -e /sys/fs/cgroup/memory.peak || { printf 'memory.peak '; "
        "cat /sys/fs/cgroup/memory.peak; }; "
        'awk \'$1 == "oom_kill" {print "memory.events " $1 " " $2}\' '
        "/sys/fs/cgroup/memory.events"
    )
    proc = subprocess.run(
        ["docker", "exec", name, "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode:
        return None
    try:
        return parse_cgroup_metrics(proc.stdout)
    except ValueError:
        return None


def _scope_summary(scope_results, allow_missing_helper=False):
    summary = {}
    for scope in ("task", "proxy", "helper"):
        per_container = [samples[scope] for samples in scope_results]
        complete = all(
            rows and len(rows) == len(samples["task"])
            for rows, samples in zip(per_container, scope_results, strict=True)
        )
        if scope == "helper" and allow_missing_helper and not complete:
            summary[scope] = {
                "status": "unavailable_historical",
                "cpu_seconds": None,
                "peak_mib": None,
                "oom_kills": None,
                "peak_kind": None,
            }
            continue
        assert complete, f"{scope} metrics missing"
        oom = sum(max(sample["oom_kill"] for sample in rows) for rows in per_container)
        assert oom == 0, f"{scope} OOM"
        summary[scope] = {
            "containers": len(per_container),
            "cpu_seconds": round(
                sum(max(sample["cpu_usage_usec"] for sample in rows) for rows in per_container)
                / 1_000_000,
                6,
            ),
            "peak_mib": round(
                max(sample["memory_peak"] for rows in per_container for sample in rows) / 1024**2,
                1,
            ),
            "oom_kills": oom,
            "peak_kind": (
                "sampled"
                if any(
                    sample["peak_kind"] == "sampled" for rows in per_container for sample in rows
                )
                else "cgroup"
            ),
        }
    return summary


def summarize_results(results, scope_results=None, allow_missing_helper=False):
    """Validate all job samples and return the smoke summary."""
    if len(results) > 1:
        assert max(rows[0]["time"] for rows in results) < min(
            rows[-1]["time"] for rows in results
        ), "jobs did not overlap"
    samples = [sample for rows in results for sample in rows]
    assert all(sample["oom_kill"] == 0 for sample in samples), "task OOM"
    assert max(sample["shmem"] for sample in samples) < 64 * 1024 * 1024, "build data uses RAM"
    available = min(sample["vm_available_kib"] / sample["vm_total_kib"] for sample in samples)
    assert available >= 0.2, (
        f"{available * 100:.1f}% VM headroom is less than 20%; "
        "increase Docker memory or reduce workloads"
    )
    summary = {
        "jobs": len(results),
        "peak_task_mib": [
            round(max(sample["bytes"] for sample in rows) / 1024**2, 1) for rows in results
        ],
        "minimum_vm_available_percent": round(available * 100, 1),
        "oom_kills": 0,
    }
    if scope_results is not None:
        summary["scopes"] = _scope_summary(scope_results, allow_missing_helper)
    return summary


def _rusage():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "self_cpu_seconds": own.ru_utime + own.ru_stime,
        "children_cpu_seconds": children.ru_utime + children.ru_stime,
    }


def _native_daemon_cpu():
    if platform.system() != "Linux":
        return {"status": "unavailable_docker_desktop", "cpu_seconds": None}
    ticks = os.sysconf("SC_CLK_TCK")
    total = 0
    found = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = path.read_text(encoding="utf-8").strip()
            if name not in {"dockerd", "containerd"}:
                continue
            fields = path.with_name("stat").read_text(encoding="utf-8").split()
            total += int(fields[13]) + int(fields[14])
            found.append(name)
        except (OSError, ValueError, IndexError):
            continue
    return {
        "status": "measured" if found else "unavailable_no_native_daemon",
        "cpu_seconds": total / ticks if found else None,
        "processes": sorted(found),
    }


def _image_metadata(image, proxy=False):
    inspected = json.loads(
        subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout
    )[0]
    emit = (
        'set -eu; emit() { key=$1; shift; value=$("$@" 2>&1); '
        "test -n \"$value\"; value=$(printf '%s\\n' \"$value\" | sed -n '1p'); "
        'printf \'%s=%s\\n\' "$key" "$value"; }; '
    )
    script = (
        emit + "emit os cat /etc/alpine-release; emit squid squid -v"
        if proxy
        else (
            emit + "emit os cat /etc/debian_version; emit docker docker --version; "
            "emit rootlesskit rootlesskit --version; emit slirp4netns slirp4netns --version; "
            "emit python python3 --version; emit node node --version; emit pi pi --version"
        )
    )
    versions = probe_image(image, ["sh", "-c", script])
    dependencies = dict(line.split("=", 1) for line in versions.stdout.splitlines())
    required = (
        {"os", "squid"}
        if proxy
        else {
            "os",
            "docker",
            "rootlesskit",
            "slirp4netns",
            "python",
            "node",
            "pi",
        }
    )
    if set(dependencies) != required or not all(dependencies.values()):
        raise ValueError("missing image dependency version")
    return {
        "id": inspected["Id"],
        "layers": inspected["RootFS"]["Layers"],
        # The final images use one base-image layer. Application COPY/RUN layers can change.
        "base_layer": inspected["RootFS"]["Layers"][0],
        "dependencies": dependencies,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--source-sha")
    parser.add_argument("--allow-missing-helper", action="store_true")
    parser.add_argument("--image", default="franky")
    parser.add_argument("--proxy-image", default="franky-proxy")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--output")
    args = parser.parse_args()
    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    from franky.config import Config
    from franky.container import run_in_container, run_names
    from franky.engine import PiEngine
    from franky.transcript import Transcript

    ids = [uuid.uuid4().hex[:12] for _ in range(args.jobs)]
    usage_before = _rusage()
    daemon_before = _native_daemon_cpu()
    source_sha = (
        args.source_sha
        or subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    task_image = _image_metadata(args.image)
    proxy_image = _image_metadata(args.proxy_image, proxy=True)
    started = time.monotonic()

    def run(job_id):
        samples = []
        net, proxy, task = run_names(job_id)
        del net
        scopes = {"task": [], "proxy": [], "helper": []}

        def progress(line):
            if line.startswith("MEMORY "):
                sample = json.loads(line.removeprefix("MEMORY "))
                if sample["peak_bytes"] is None:
                    sample["peak_bytes"] = sample["bytes"]
                samples.append(sample)
                scopes["task"].append(
                    {
                        "cpu_usage_usec": sample["cpu_usage_usec"],
                        "memory_current": sample["bytes"],
                        "memory_peak": sample["peak_bytes"],
                        "peak_kind": sample["peak_kind"],
                        "oom_kill": sample["oom_kill"],
                    }
                )
                for scope, name in (("proxy", proxy), ("helper", f"{task}-disk")):
                    metric = read_container_metrics(name)
                    if metric is not None:
                        scopes[scope].append(metric)

        code, output = run_in_container(
            Config(engine=PiEngine(), allowed_repos=["smoke/example"]),
            ["python3", "-c", WORKLOAD],
            image=args.image,
            proxy_image=args.proxy_image,
            timeout=90,
            progress=progress,
            run_id=job_id,
        )
        try:
            assert code == 0, output.tail(2000) if isinstance(output, Transcript) else output
            assert len(samples) == 10, "workload did not complete its samples"
            return samples, scopes
        finally:
            if isinstance(output, Transcript):
                output.close()

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        completed = list(pool.map(run, ids))
    results = [item[0] for item in completed]
    scopes = [item[1] for item in completed]
    elapsed = time.monotonic() - started
    usage_after = _rusage()
    daemon_after = _native_daemon_cpu()
    summary = summarize_results(results, scopes, args.allow_missing_helper)
    summary.update(
        {
            "schema": 1,
            "metadata": {
                "architecture": platform.machine().lower(),
                "python": platform.python_version(),
                "source_sha": source_sha,
                "task_image": args.image,
                "proxy_image": args.proxy_image,
                "images": {"task": task_image, "proxy": proxy_image},
            },
            "elapsed_seconds": elapsed,
            "throughput": {
                "files_per_second": args.jobs * 10_000 / elapsed,
                "write_mib_per_second": args.jobs * 512 / elapsed,
            },
            "rusage": {key: usage_after[key] - usage_before[key] for key in usage_before},
            "native_daemon_cpu": {
                "status": daemon_after["status"],
                "cpu_seconds": (
                    daemon_after["cpu_seconds"] - daemon_before["cpu_seconds"]
                    if daemon_before["cpu_seconds"] is not None
                    and daemon_after["cpu_seconds"] is not None
                    else None
                ),
                "processes": daemon_after.get("processes", []),
            },
        }
    )
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.splitlines()
    owned = {
        name for job_id in ids for name in (*run_names(job_id)[1:], f"{run_names(job_id)[2]}-disk")
    }
    assert not set(names).intersection(owned), "a smoke container remains"
    print(f"SMOKE PASS: {args.jobs} jobs completed with disk-backed data and no task OOM.")


if __name__ == "__main__":
    main()
