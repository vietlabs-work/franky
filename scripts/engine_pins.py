#!/usr/bin/env python3
"""Bump the engine version pins in the Dockerfile to the npm `latest` versions.

Usage: python3 scripts/engine_pins.py bump

Rewrites each changed `ARG <NAME>_VERSION=` line in the Dockerfile, adds one bullet under
`## [Unreleased]` in CHANGELOG.md, and prints one `package old -> new` line per change.
Prints nothing and changes nothing when every pin is current. The scheduled
`engine-pins` workflow turns the change into a pull request. Stdlib only.
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Dockerfile ARG -> npm package. tests/test_footprint.py checks the Dockerfile side.
PINS = {
    "PI_VERSION": "@earendil-works/pi-coding-agent",
    "CLAUDE_CODE_VERSION": "@anthropic-ai/claude-code",
    "CODEX_VERSION": "@openai/codex",
    "OPENCODE_VERSION": "opencode-ai",
}
# Plain ASCII releases only: the registry answer is untrusted input written into the Dockerfile.
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
UNRELEASED = "## [Unreleased]\n"


def _key(version: str) -> tuple[int, ...]:
    match = VERSION.fullmatch(version)
    if not match:
        raise SystemExit(f"engine_pins.py: not a plain x.y.z version: {version!r}")
    return tuple(int(part) for part in match.groups())


def npm_latest(package: str) -> str:
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}/latest"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)["version"]


def current_pins(dockerfile: str) -> dict[str, str]:
    found = dict(re.findall(r"^ARG (\w+_VERSION)=(\S+)$", dockerfile, re.M))
    missing = sorted(set(PINS) - set(found))
    if missing:
        raise SystemExit(f"engine_pins.py: Dockerfile has no ARG for {', '.join(missing)}")
    return {arg: found[arg] for arg in PINS}


def bump(root: Path, fetch: Callable[[str], str] = npm_latest) -> list[str]:
    dockerfile_path = root / "Dockerfile"
    dockerfile = dockerfile_path.read_text()
    changes = []
    for arg, old in current_pins(dockerfile).items():
        new = fetch(PINS[arg])
        if _key(new) <= _key(old):
            continue  # never propose a downgrade when npm moves `latest` back
        line = f"ARG {arg}={old}\n"
        if dockerfile.count(line) != 1:
            raise SystemExit(f"engine_pins.py: Dockerfile needs exactly one {line.strip()!r}")
        dockerfile = dockerfile.replace(line, f"ARG {arg}={new}\n")
        changes.append(f"{PINS[arg]} {old} -> {new}")
    if not changes:
        return []
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text()
    if changelog.count(UNRELEASED) != 1:
        raise SystemExit("engine_pins.py: CHANGELOG.md needs exactly one ## [Unreleased] heading")
    bullet = f"- Engine updates: {', '.join(changes)}.\n"
    dockerfile_path.write_text(dockerfile)
    changelog_path.write_text(changelog.replace(UNRELEASED, f"{UNRELEASED}\n{bullet}"))
    return changes


def main(argv: list[str]) -> int:
    if argv != ["bump"]:
        print(__doc__, file=sys.stderr)
        return 2
    for line in bump(ROOT):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
