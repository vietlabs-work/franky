"""Bounded, per-task daemon startup without Docker or network access."""

import fcntl
import importlib.util
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def starter():
    spec = importlib.util.spec_from_file_location("docker_start", ROOT / "franky-docker-start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake(monkeypatch, starter, *, ready=False, dead=False, hanging=False):
    state = SimpleNamespace(ready=ready, launches=0, signals=[], probes=[], waited=False)
    clock = [0.0]
    monkeypatch.setattr(starter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        starter.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def probe(argv, **kwargs):
        state.probes.append(kwargs)
        assert argv == ["docker", "version"]
        assert 0 < kwargs["timeout"] <= 1
        if hanging:
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=0 if state.ready else 1)

    def launch(argv, **kwargs):
        assert argv == ["dockerd-rootless.sh"] and kwargs["start_new_session"]
        state.launches += 1
        return SimpleNamespace(pid=54321, poll=lambda: 1 if dead else None, wait=wait)

    def wait(**kwargs):
        state.waited = True

    monkeypatch.setattr(starter.subprocess, "run", probe)
    monkeypatch.setattr(starter.subprocess, "Popen", launch)
    monkeypatch.setattr(starter.os, "killpg", lambda pid, sig: state.signals.append((pid, sig)))
    return state


def test_ready_daemon_needs_no_launch(monkeypatch, starter, tmp_path):
    state = _fake(monkeypatch, starter, ready=True)
    assert starter.start(tmp_path, log_path=tmp_path / "daemon.log")
    assert state.launches == 0 and not state.signals
    assert state.probes[0]["env"]["DOCKER_HOST"] == f"unix://{tmp_path}/docker.sock"
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("hanging", [False, True])
def test_deadline_and_hanging_probe_clean_only_owned_group(monkeypatch, starter, tmp_path, hanging):
    state = _fake(monkeypatch, starter, hanging=hanging)
    assert not starter.start(tmp_path, timeout=2, log_path=tmp_path / "daemon.log")
    assert starter.time.monotonic() <= 2
    assert state.signals == [(54321, signal.SIGTERM), (54321, signal.SIGKILL)]
    assert state.waited and state.launches == 1


def test_early_daemon_death_fails_without_waiting_for_deadline(monkeypatch, starter, tmp_path):
    state = _fake(monkeypatch, starter, dead=True)
    assert not starter.start(tmp_path, log_path=tmp_path / "daemon.log")
    assert starter.time.monotonic() < 1 and state.waited


def test_successful_launch_is_not_terminated(monkeypatch, starter, tmp_path):
    state = _fake(monkeypatch, starter)
    monkeypatch.setattr(starter.time, "sleep", lambda _: setattr(state, "ready", True))
    assert starter.start(tmp_path, log_path=tmp_path / "daemon.log")
    assert state.launches == 1 and not state.signals and not state.waited


def test_cleanup_escalates_and_reaps_after_term_timeout(monkeypatch, starter):
    signals, waits = [], []

    def wait(*, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("daemon", timeout)

    monkeypatch.setattr(starter.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    starter._stop(SimpleNamespace(pid=54321, wait=wait))
    assert signals == [(54321, signal.SIGTERM), (54321, signal.SIGKILL)]
    assert waits == [0.5, 0.5]


def test_locked_starter_times_out_without_launch_or_kill(monkeypatch, starter, tmp_path):
    state = _fake(monkeypatch, starter)
    with (tmp_path / "franky-docker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert not starter.start(tmp_path, timeout=2, log_path=tmp_path / "daemon.log")
    assert starter.time.monotonic() == pytest.approx(2)
    assert state.launches == 0 and not state.signals


def test_concurrent_starters_launch_once(monkeypatch, starter, tmp_path):
    ready = threading.Event()
    launches = []

    def launch(*args, **kwargs):
        launches.append(kwargs)
        ready.set()
        return SimpleNamespace(pid=54321, poll=lambda: None)

    monkeypatch.setattr(starter.subprocess, "Popen", launch)
    monkeypatch.setattr(
        starter.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0 if ready.is_set() else 1),
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: starter.start(tmp_path, log_path=tmp_path / "daemon.log"), range(4))
        )
    assert results == [True] * 4 and len(launches) == 1
