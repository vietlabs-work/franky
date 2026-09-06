#!/usr/bin/env python3
"""Concurrent production runners, without credentials, model calls, or PRs.

Each holds 256 MiB of process memory, 512 MiB of build data, and 10,000 files. This tests
the runner budget, not the memory needs of an arbitrary repository's test suite.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from franky.config import Config  # noqa: E402
from franky.container import run_in_container, run_names  # noqa: E402
from franky.engine import PiEngine  # noqa: E402
from franky.transcript import Transcript  # noqa: E402

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
    info = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
    print('MEMORY ' + json.dumps({
        'bytes': int((root / 'memory.current').read_text()),
        'anon': int(stat['anon']), 'shmem': int(stat['shmem']),
        'oom_kill': int(events['oom_kill']),
        'vm_total_kib': int(info['MemTotal'].split()[0]),
        'vm_available_kib': int(info['MemAvailable'].split()[0]),
        'time': time.time(),
    }), flush=True)
    time.sleep(2)
assert len(held) == 256 * 1024 * 1024
print('WORKLOAD PASSED', flush=True)
"""


def summarize_results(results):
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
    return {
        "jobs": len(results),
        "peak_task_mib": [
            round(max(sample["bytes"] for sample in rows) / 1024**2, 1) for rows in results
        ],
        "minimum_vm_available_percent": round(available * 100, 1),
        "oom_kills": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="franky")
    parser.add_argument("--proxy-image", default="franky-proxy")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    args = parser.parse_args()
    ids = [uuid.uuid4().hex[:12] for _ in range(args.jobs)]

    def run(job_id):
        samples = []

        def progress(line):
            if line.startswith("MEMORY "):
                samples.append(json.loads(line.removeprefix("MEMORY ")))

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
            return samples
        finally:
            if isinstance(output, Transcript):
                output.close()

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(run, ids))
    print(json.dumps(summarize_results(results), indent=2))
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.splitlines()
    assert not set(names).intersection(name for job_id in ids for name in run_names(job_id)), (
        "a smoke container remains"
    )
    print(f"SMOKE PASS: {args.jobs} jobs completed with disk-backed data and no task OOM.")


if __name__ == "__main__":
    main()
