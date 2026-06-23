"""Tests for franky._install: classify_executable and detect_install."""

import importlib.metadata
import json

import franky
from franky._install import DIST_NAME, _is_editable, classify_executable, detect_install


# ---------------------------------------------------------------------------
# classify_executable
# ---------------------------------------------------------------------------


def test_classify_uv_tools():
    assert classify_executable("/home/user/.local/share/uv/tools/franky/bin/python") == "uv tool"


def test_classify_uv_tools_windows_style():
    assert (
        classify_executable(
            "C:\\Users\\user\\AppData\\Roaming\\uv\\tools\\franky\\Scripts\\python.exe"
        )
        == "uv tool"
    )


def test_classify_pipx():
    assert classify_executable("/home/user/.local/pipx/venvs/franky/bin/python") == "pipx"


def test_classify_pipx_dot_local():
    assert classify_executable("/home/user/.local/pipx/venvs/franky/bin/python3") == "pipx"


def test_classify_plain_python():
    assert classify_executable("/usr/bin/python3") == "pip"


def test_classify_venv_python():
    assert classify_executable("/repo/.venv/bin/python") == "pip"


def test_classify_uv_tools_beats_pipx_when_both_present():
    # A path that contains both tokens: uv/tools must win (it is checked first).
    assert (
        classify_executable("/home/user/.local/share/uv/tools/franky/pipx/bin/python") == "uv tool"
    )


# ---------------------------------------------------------------------------
# _is_editable
# ---------------------------------------------------------------------------


class _FakeDist:
    """Minimal Distribution-like object for _is_editable tests."""

    def __init__(self, direct_url_json: str | None):
        self._raw = direct_url_json

    def read_text(self, name: str) -> str | None:
        return self._raw


def test_is_editable_true():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": True}, "url": "file:///repo"}))
    assert _is_editable(dist) is True


def test_is_editable_false_when_not_editable():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": False}, "url": "file:///repo"}))
    assert _is_editable(dist) is False


def test_is_editable_false_when_no_file():
    dist = _FakeDist(None)
    assert _is_editable(dist) is False


def test_is_editable_false_on_bad_json():
    dist = _FakeDist("not json at all")
    assert _is_editable(dist) is False


def test_is_editable_false_when_read_text_raises():
    class BadDist:
        def read_text(self, name):
            raise OSError("disk error")

    assert _is_editable(BadDist()) is False


# ---------------------------------------------------------------------------
# detect_install
# ---------------------------------------------------------------------------


def _pkg_not_found(_name):
    raise importlib.metadata.PackageNotFoundError("franky")


def test_detect_install_not_installed_is_dev_checkout():
    result = detect_install(dist_lookup=_pkg_not_found)
    assert result.kind == "dev checkout"
    # Path must point at a real directory that contains "franky".
    from pathlib import Path

    p = Path(result.path)
    assert p.exists() and p.is_dir(), f"expected real dir, got {result.path}"
    assert "franky" in result.path.lower()


def test_detect_install_editable_is_dev_checkout():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": True}, "url": "file:///repo"}))
    result = detect_install(dist_lookup=lambda _: dist)
    assert result.kind == "dev checkout"
    from pathlib import Path

    p = Path(result.path)
    assert p.exists() and p.is_dir()


def test_detect_install_non_editable_uv_tool():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": False}}))
    result = detect_install(
        executable="/home/user/.local/share/uv/tools/franky/bin/python",
        dist_lookup=lambda _: dist,
    )
    assert result.kind == "uv tool"
    assert result.path == "/home/user/.local/share/uv/tools/franky/bin/python"


def test_detect_install_non_editable_pipx():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": False}}))
    result = detect_install(
        executable="/home/user/.local/pipx/venvs/franky/bin/python",
        dist_lookup=lambda _: dist,
    )
    assert result.kind == "pipx"


def test_detect_install_non_editable_pip():
    dist = _FakeDist(json.dumps({"dir_info": {"editable": False}}))
    result = detect_install(
        executable="/usr/bin/python3",
        dist_lookup=lambda _: dist,
    )
    assert result.kind == "pip"
    assert result.path == "/usr/bin/python3"


# ---------------------------------------------------------------------------
# franky_version: reads the franky-agent distribution metadata, not "franky"
# ---------------------------------------------------------------------------


def test_dist_name_is_franky_agent():
    # The import package is `franky`, the published distribution is `franky-agent`.
    assert DIST_NAME == "franky-agent"


def test_franky_version_reads_franky_agent_metadata(monkeypatch):
    # The distribution is `franky-agent`; a lookup of "franky" would PackageNotFoundError and
    # silently fall back to __version__, defeating metadata precedence + skew detection.
    captured = {}

    def fake_version(name):
        captured["name"] = name
        return "9.9.9"

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    assert franky.franky_version() == "9.9.9"  # metadata wins over the baked-in __version__
    assert captured["name"] == DIST_NAME


def test_franky_version_falls_back_to_dunder_when_metadata_absent(monkeypatch):
    def boom(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", boom)
    assert franky.franky_version() == franky.__version__
