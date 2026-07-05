"""Shared pytest fixtures.

WHY autouse for FRANKY_CONFIG_FILE:
  franky/userconfig.py reads ~/.franky/config by default.  Without this fixture,
  running the test suite on a machine that has a real ~/.franky/config would inject
  production values into tests that call `build`, `iterate`, or any code path that
  calls `load_config_file`.  The autouse fixture points every test at a non-existent
  tmp path so the config file is always an unconditional no-op regardless of the
  developer's real home config.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test write run records to the developer's real ~/.franky/runs (issue #63).

    `build`/`iterate` call jobs.write_record via _record_run_start, which defaults to os.environ;
    a test that replaces cli.os.environ with a dict lacking FRANKY_RUNS_DIR would otherwise fall
    back to the real home dir. Redirect jobs.runs_dir to a tmp path so no test can pollute home,
    while still honoring an explicit FRANKY_RUNS_DIR (the test_jobs.py tests that pass their own
    env dict keep working). Mirrors the FRANKY_CONFIG_FILE guarantee below.
    """
    import franky.jobs as jobs

    default = tmp_path / "test-franky-runs"

    def _safe_runs_dir(env=None):
        source = os.environ if env is None else env
        override = source.get("FRANKY_RUNS_DIR")
        return Path(override) if override else default

    monkeypatch.setattr(jobs, "runs_dir", _safe_runs_dir)


@pytest.fixture(autouse=True)
def _hermetic_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point FRANKY_CONFIG_FILE at a non-existent path for every test.

    This ensures load_config_file() is always a silent no-op in the test suite;
    no real ~/.franky/config can bleed in.  Tests that need a real config file
    should set os.environ["FRANKY_CONFIG_FILE"] (or the env dict they pass to
    config_file_path) to a specific tmp path after this fixture runs.
    """
    fake_path = tmp_path / "test-franky-config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(fake_path))


@pytest.fixture(autouse=True)
def _no_idempotency_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the idempotency pre-check to "no open PR" for every test (no real network).

    `franky build` calls find_open_pr (issue #50) against the GitHub REST API. Default it to
    None so the existing build tests never hit the network and proceed to the container path;
    tests that exercise the already_open short-circuit override this symbol explicitly.
    """
    import franky.cli as cli

    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: None)
