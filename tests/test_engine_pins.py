import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("engine_pins", ROOT / "scripts/engine_pins.py")
engine_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine_pins)

DOCKERFILE = """FROM x
ARG PI_VERSION=1.6.0
ARG CLAUDE_CODE_VERSION=2.1.331
ARG CODEX_VERSION=0.172.0
ARG OPENCODE_VERSION=1.19.6
RUN true
"""
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n- Existing entry.\n\n## [0.4.3] - 2026-11-24\n"
CURRENT = {
    "@earendil-works/pi-coding-agent": "1.6.0",
    "@anthropic-ai/claude-code": "2.1.331",
    "@openai/codex": "0.172.0",
    "opencode-ai": "1.19.6",
}


def _repo(tmp_path):
    (tmp_path / "Dockerfile").write_text(DOCKERFILE)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def test_bump_rewrites_changed_pins_and_adds_one_changelog_bullet(tmp_path):
    repo = _repo(tmp_path)
    latest = CURRENT | {"@anthropic-ai/claude-code": "2.1.340", "opencode-ai": "1.20.0"}

    changes = engine_pins.bump(repo, latest.__getitem__)

    assert changes == [
        "@anthropic-ai/claude-code 2.1.331 -> 2.1.340",
        "opencode-ai 1.19.6 -> 1.20.0",
    ]
    dockerfile = (repo / "Dockerfile").read_text()
    assert "ARG CLAUDE_CODE_VERSION=2.1.340\n" in dockerfile
    assert "ARG OPENCODE_VERSION=1.20.0\n" in dockerfile
    assert "ARG PI_VERSION=1.6.0\n" in dockerfile
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG.replace(
        "## [Unreleased]\n",
        "## [Unreleased]\n\n- Engine updates: @anthropic-ai/claude-code 2.1.331 -> 2.1.340, "
        "opencode-ai 1.19.6 -> 1.20.0.\n",
    )


def test_bump_changes_nothing_when_every_pin_is_current(tmp_path):
    repo = _repo(tmp_path)
    assert engine_pins.bump(repo, CURRENT.__getitem__) == []
    assert (repo / "Dockerfile").read_text() == DOCKERFILE
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG


@pytest.mark.parametrize(
    "bad", ["2.2.0-beta.1", "latest", "2.2.0\nRUN curl evil", "", "\u0661.2.3", "1.02.3"]
)
def test_bump_refuses_a_version_that_is_not_plain_and_writes_nothing(tmp_path, bad):
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit):
        engine_pins.bump(repo, (CURRENT | {"@openai/codex": bad}).__getitem__)
    assert (repo / "Dockerfile").read_text() == DOCKERFILE
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG


def test_bump_skips_a_downgrade(tmp_path):
    repo = _repo(tmp_path)
    assert engine_pins.bump(repo, (CURRENT | {"@openai/codex": "0.171.9"}).__getitem__) == []
    assert (repo / "Dockerfile").read_text() == DOCKERFILE


def test_bump_refuses_a_pin_that_appears_twice(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Dockerfile").write_text(DOCKERFILE + "ARG CODEX_VERSION=0.172.0\n")
    with pytest.raises(SystemExit):
        engine_pins.bump(repo, (CURRENT | {"@openai/codex": "0.173.0"}).__getitem__)


def test_bump_refuses_a_dockerfile_without_every_pin(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Dockerfile").write_text(DOCKERFILE.replace("ARG CODEX_VERSION=0.172.0\n", ""))
    with pytest.raises(SystemExit):
        engine_pins.bump(repo, CURRENT.__getitem__)


def test_pins_match_the_real_dockerfile():
    pins = engine_pins.current_pins((ROOT / "Dockerfile").read_text())
    assert set(pins) == set(engine_pins.PINS)
    assert all(engine_pins.VERSION.fullmatch(version) for version in pins.values())
