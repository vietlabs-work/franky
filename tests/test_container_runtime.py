import json
import subprocess

import pytest

import franky.container as container


class _StopAfterSamples:
    def __init__(self, count):
        self.remaining = count
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        if timeout == 5:
            self.remaining -= 1
            self.stopped = self.remaining == 0
        return self.stopped


def _mounts():
    return json.dumps(
        [
            {"Type": "volume", "Name": "work-volume", "Destination": "/work"},
            {"Type": "volume", "Name": "home-volume", "Destination": "/home/franky"},
            {"Type": "volume", "Name": "tmp-volume", "Destination": "/tmp"},
            {
                "Type": "volume",
                "Name": "franky-codex-auth",
                "Destination": "/home/franky/.codex",
            },
        ]
    )


def _storage_runner(*, samples=("10\n2097152\n",), inspect_failures=0, task_running=False):
    calls = []
    sample_outputs = iter(samples)
    inspect_attempts = 0

    def runner(argv, **kwargs):
        nonlocal inspect_attempts
        calls.append((argv, kwargs))
        if argv[:4] == ["docker", "inspect", "-f", "{{json .Mounts}}"]:
            inspect_attempts += 1
            if inspect_attempts <= inspect_failures:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_mounts(), stderr="")
        if argv[:4] == ["docker", "inspect", "-f", "{{.State.Running}}"]:
            value = "true\n" if task_running else "false\n"
            return subprocess.CompletedProcess(argv, 0, stdout=value, stderr="")
        if argv[:3] == ["docker", "run", "-d"]:
            return subprocess.CompletedProcess(argv, 0, stdout="helper-id\n", stderr="")
        if argv[:3] == ["docker", "exec", "franky-run-test-disk"]:
            value = next(sample_outputs)
            if isinstance(value, BaseException):
                raise value
            return subprocess.CompletedProcess(argv, 0, stdout=value, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return runner, calls


def test_storage_watchdog_reuses_one_hardened_helper_across_samples():
    runner, calls = _storage_runner(samples=("10\n2097152\n", "20\n2097152\n"))
    failures = []

    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(2), failures, 60
    )

    helper_runs = [call for call, _ in calls if call[:3] == ["docker", "run", "-d"]]
    samples = [call for call, _ in calls if call[:3] == ["docker", "exec", "franky-run-test-disk"]]
    assert len(helper_runs) == 1
    assert len(samples) == 2
    helper = helper_runs[0]
    assert "--network=none" in helper
    assert "--read-only" in helper
    assert "--cap-drop=ALL" in helper
    assert "--cap-add=DAC_READ_SEARCH" in helper
    assert "--security-opt=no-new-privileges" in helper
    assert "--user=0" in helper
    assert "--pids-limit=32" in helper
    assert "--memory=64m" in helper
    assert "--memory-swap=64m" in helper
    assert helper[-3:] == ["--entrypoint=sleep", "trusted-image", "60"]
    mounts = [helper[i + 1] for i, value in enumerate(helper) if value == "--mount"]
    assert mounts == [
        "type=volume,src=work-volume,dst=/data/work,readonly",
        "type=volume,src=home-volume,dst=/data/home,readonly",
        "type=volume,src=tmp-volume,dst=/data/tmp,readonly",
    ]
    assert "franky-codex-auth" not in " ".join(helper)
    assert failures == []


def test_storage_watchdog_retries_initial_task_creation_race():
    runner, calls = _storage_runner(inspect_failures=1)
    failures = []

    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(1), failures, 60
    )

    mount_inspects = [
        call for call, _ in calls if call[:4] == ["docker", "inspect", "-f", "{{json .Mounts}}"]
    ]
    assert len(mount_inspects) == 2
    assert failures == []


def test_storage_watchdog_mount_discovery_has_ten_second_total_deadline(monkeypatch):
    now = [0.0]
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:4] == ["docker", "inspect", "-f", "{{json .Mounts}}"]:
            now[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if argv[:4] == ["docker", "inspect", "-f", "{{.State.Running}}"]:
            return subprocess.CompletedProcess(argv, 0, stdout="false\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(container.time, "monotonic", lambda: now[0])
    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(1), [], 60
    )

    assert now[0] <= 10
    assert all(
        kwargs["timeout"] <= 3
        for argv, kwargs in calls
        if argv[:4] == ["docker", "inspect", "-f", "{{json .Mounts}}"]
    )


@pytest.mark.parametrize(
    "sample",
    [
        "malformed\n",
        subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=10),
    ],
)
def test_storage_watchdog_stops_task_when_sample_cannot_be_verified(sample):
    runner, calls = _storage_runner(samples=(sample,), task_running=True)
    failures = []

    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(1), failures, 60
    )

    reaps = [call for call, _ in calls if call[:3] == ["docker", "rm", "-f"]]
    assert failures == ["franky: could not verify task disk usage - stopping the run"]
    assert reaps.index(["docker", "rm", "-f", "-v", "franky-run-test-disk"]) < reaps.index(
        ["docker", "rm", "-f", "-v", "franky-run-test"]
    )


def test_storage_watchdog_cleans_helper_and_exact_volumes_after_task_death():
    timeout = subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=10)
    runner, calls = _storage_runner(samples=(timeout,), task_running=False)
    failures = []

    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(1), failures, 60
    )

    argv = [call for call, _ in calls]
    assert ["docker", "rm", "-f", "-v", "franky-run-test-disk"] in argv
    assert ["docker", "volume", "rm", "work-volume", "home-volume", "tmp-volume"] in argv
    assert ["docker", "rm", "-f", "-v", "franky-run-test"] not in argv
    assert failures == []
    running_inspect = next(
        kwargs
        for call, kwargs in calls
        if call[:4] == ["docker", "inspect", "-f", "{{.State.Running}}"]
    )
    assert running_inspect["timeout"] == 3


@pytest.mark.parametrize("state", ["timeout", "invalid", "daemon-error"])
def test_storage_watchdog_stops_task_when_liveness_is_unknown(state):
    base_runner, calls = _storage_runner(samples=("malformed",))

    def runner(argv, **kwargs):
        if argv[:4] == ["docker", "inspect", "-f", "{{.State.Running}}"]:
            if state == "timeout":
                raise subprocess.TimeoutExpired(argv, 3)
            return subprocess.CompletedProcess(
                argv, int(state == "daemon-error"), stdout="invalid", stderr="daemon unavailable"
            )
        return base_runner(argv, **kwargs)

    failures = []
    container._watch_storage(
        "franky-run-test", "trusted-image", 1024, runner, _StopAfterSamples(1), failures, 60
    )
    assert failures == ["franky: could not verify task disk usage - stopping the run"]
    assert any(argv == ["docker", "rm", "-f", "-v", "franky-run-test"] for argv, _ in calls)


def test_network_cleanup_is_bounded():
    def runner(argv, **kwargs):
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(argv, 0)

    assert container._reap_network("owned-network", runner)


def test_reap_run_removes_disk_helper_before_task():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert container.reap_run("abc123", runner=runner)
    assert calls.index(["docker", "rm", "-f", "-v", "franky-run-abc123-disk"]) < calls.index(
        ["docker", "rm", "-f", "-v", "franky-run-abc123"]
    )


def test_proxy_readiness_accepts_refused_https_connect_status():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 56, stdout="403", stderr="CONNECT refused")

    assert container.wait_proxy_ready("proxy-test", runner, lambda _seconds: None)
    argv, kwargs = calls[0]
    assert argv[:3] == ["docker", "exec", "proxy-test"]
    assert "%{http_connect}" in argv
    assert "%{http_code}" not in argv
    assert "--noproxy" in argv and argv[argv.index("--noproxy") + 1] == ""
    assert argv[-1] == "https://denied.invalid:443"
    assert 0 < kwargs["timeout"] <= 3


@pytest.mark.parametrize("status", ["200", "500", "000", "403 extra", ""])
def test_proxy_readiness_rejects_non_denial_status(status):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=status, stderr="")

    assert not container.wait_proxy_ready("proxy-test", runner, lambda _seconds: None)


def test_proxy_readiness_stops_when_proxy_container_dies():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not running")
        return subprocess.CompletedProcess(argv, 0, stdout="false\n", stderr="")

    assert not container.wait_proxy_ready("proxy-test", runner, lambda _seconds: None)
    assert sum(argv[:2] == ["docker", "exec"] for argv in calls) == 1


def test_proxy_readiness_has_fifteen_second_total_deadline(monkeypatch):
    now = [0.0]

    def runner(argv, **kwargs):
        now[0] += kwargs["timeout"]
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not ready")
        return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")

    def sleeper(seconds):
        now[0] += seconds

    monkeypatch.setattr(container.time, "monotonic", lambda: now[0])
    assert not container.wait_proxy_ready("proxy-test", runner, sleeper)
    assert now[0] <= 15
