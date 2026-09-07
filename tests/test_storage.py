import json
import subprocess
from types import SimpleNamespace

import pytest

from franky import container


def test_reap_accepts_an_already_auto_removed_container():
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stderr=f"No such container: {argv[-1]}")

    assert container._reap("finished-task", runner)


def test_disk_preflight_checks_daemon_free_space_once():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        output = "0\n4194304\n" if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    assert container._storage_sample(None, "franky", runner) == (0, 4194304)
    assert len([argv for argv in calls if argv[:2] == ["docker", "run"]]) == 1
    assert any(argv[:3] == ["docker", "rm", "-f"] for argv in calls)


@pytest.mark.parametrize(
    "mounts",
    [
        [],
        [
            {"Type": "volume", "Name": "abc", "Destination": "/work"},
            {"Type": "volume", "Name": "def", "Destination": "/home/franky"},
        ],
        [
            {"Type": "bind", "Name": "abc", "Destination": "/work"},
            {"Type": "volume", "Name": "def", "Destination": "/home/franky"},
            {"Type": "volume", "Name": "ghi", "Destination": "/tmp"},
        ],
        [
            {"Type": "volume", "Name": "bad/name", "Destination": "/work"},
            {"Type": "volume", "Name": "def", "Destination": "/home/franky"},
            {"Type": "volume", "Name": "ghi", "Destination": "/tmp"},
        ],
    ],
)
def test_disk_helper_rejects_invalid_task_mounts(mounts):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(mounts), stderr="")

    with pytest.raises(ValueError):
        container._task_storage_volumes("task", runner)


def _watch_runner(sample):
    mounts = [
        {"Type": "volume", "Name": "abc", "Destination": "/work"},
        {"Type": "volume", "Name": "def", "Destination": "/home/franky"},
        {"Type": "volume", "Name": "ghi", "Destination": "/tmp"},
    ]

    def runner(argv, **kwargs):
        output = json.dumps(mounts) if argv[1] == "inspect" else sample if argv[1] == "exec" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    return runner


def test_disk_guard_stops_only_its_task_on_budget_exhaustion(monkeypatch):
    removed = []
    monkeypatch.setattr(container, "_reap", lambda name, runner: removed.append(name))
    failures = []
    stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)
    container._watch_storage(
        "task-one", "franky", 8192, _watch_runner(f"{9000 * 1024}\n{10**9}\n"), stop, failures, 60
    )
    assert removed == ["task-one-disk", "task-one"]
    assert "disk budget" in failures[0]


def test_disk_guard_stops_when_daemon_disk_is_almost_full(monkeypatch):
    removed = []
    monkeypatch.setattr(container, "_reap", lambda name, runner: removed.append(name))
    failures = []
    stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)
    container._watch_storage(
        "task-one", "franky", 8192, _watch_runner("1\n1024\n"), stop, failures, 60
    )
    assert removed == ["task-one-disk", "task-one"]
    assert "free disk" in failures[0]
