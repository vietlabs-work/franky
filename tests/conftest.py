"""Shared pytest fixtures.

WHY autouse for FRANKY_CONFIG_FILE:
  franky/userconfig.py reads ~/.franky/config by default.  Without this fixture,
  running the test suite on a machine that has a real ~/.franky/config would inject
  production values into tests that call `build`, `iterate`, or any code path that
  calls `load_config_file`.  The autouse fixture points every test at a non-existent
  tmp path so the config file is always an unconditional no-op regardless of the
  developer's real home config.
"""

from pathlib import Path

import pytest


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
