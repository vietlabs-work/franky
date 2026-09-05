import json
import subprocess
from types import SimpleNamespace

from franky import container


def test_reap_accepts_an_already_auto_removed_container():
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stderr=f"No such container: {argv[-1]}")

    assert container._reap("finished-task", runner)


def test_disk_helper_cleans_volumes_if_task_exits_during_sampling(monkeypatch):
    calls = []
    mounts = [
        {"Destination": dest, "Type": "volume", "Name": name}
        for dest, name in (("/work", "work-vol"), ("/home/franky", "home-vol"), ("/tmp", "tmp-vol"))
    ]

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(mounts) if argv[1] == "inspect" else "10 9999999"
        )

    monkeypatch.setattr(container, "container_running", lambda *args: False)
    container._storage_sample("finished-task", "image", runner)
    assert ["docker", "volume", "rm", "work-vol", "home-vol", "tmp-vol"] in calls


def test_disk_sample_reads_only_this_tasks_data_volumes():
    mounts = [
        {"Type": "volume", "Name": "abc", "Destination": "/work"},
        {"Type": "volume", "Name": "def", "Destination": "/home/franky"},
        {"Type": "volume", "Name": "ghi", "Destination": "/tmp"},
        {"Type": "volume", "Name": "auth", "Destination": "/home/franky/.codex"},
    ]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        result = json.dumps(mounts) if argv[1] == "inspect" else "1024\n4194304\n"
        return subprocess.CompletedProcess(argv, 0, stdout=result)

    assert container._storage_sample("task", "franky", runner) == (1024, 4194304)
    argv = calls[1]
    assert "--network=none" in argv and "--read-only" in argv
    assert "--cap-drop=ALL" in argv and "--cap-add=DAC_READ_SEARCH" in argv
    assert "--memory=64m" in argv and "--memory-swap=64m" in argv
    assert "-e" not in argv and "auth" not in " ".join(argv)
    assert "type=volume,src=abc,dst=/data/work,readonly" in argv
    assert "type=volume,src=def,dst=/data/home,readonly" in argv
    assert "type=volume,src=ghi,dst=/data/tmp,readonly" in argv


def test_disk_guard_stops_only_its_task_on_budget_exhaustion(monkeypatch):
    monkeypatch.setattr(container, "_storage_sample", lambda *args: (9000 * 1024, 10**9))
    removed = []
    monkeypatch.setattr(container, "_reap", lambda name, runner: removed.append(name))
    failures = []
    stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)
    container._watch_storage("task-one", "franky", 8192, None, stop, failures)
    assert removed == ["task-one"]
    assert "disk budget" in failures[0]


def test_disk_guard_stops_when_daemon_disk_is_almost_full(monkeypatch):
    monkeypatch.setattr(container, "_storage_sample", lambda *args: (1, 1024))
    removed = []
    monkeypatch.setattr(container, "_reap", lambda name, runner: removed.append(name))
    failures = []
    stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)
    container._watch_storage("task-one", "franky", 8192, None, stop, failures)
    assert removed == ["task-one"]
    assert "free disk" in failures[0]
