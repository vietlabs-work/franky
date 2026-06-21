"""Engine abstraction: the inner coding-agent CLI that runs inside the container.

WHY a class per engine instead of a config table: each engine differs on three axes
that do not factor cleanly into data - the headless argv shape, how it streams its
output (so how we dig the PR URL back out), and which creds it needs. Keeping those
three together per engine is what lets the rest of Franky stay engine-agnostic.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

# Fallback when structured parsing finds nothing: agents print the PR URL near the end,
# so the LAST match is the real one (earlier matches may be a quoted issue link etc).
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")

# pi is BYOK and reads whichever provider var is present. This subset is enough to gate
# on; if NONE of these are set, that is the fail-closed condition for the pi engine.
PI_PROVIDER_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OLLAMA_HOST",
)

CLAUDE_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _fallback_pr_url(output: str) -> str | None:
    """Return the LAST github PR URL in plain text, or None. Used when structured
    per-line parsing yields nothing."""
    matches = PR_URL_RE.findall(output)
    return matches[-1] if matches else None


def _scan_jsonl_for_pr_url(output: str) -> str | None:
    """Both engines emit one JSON object per line. Walk lines, json.loads each (skip any
    non-JSON line, never raise), and return the last PR URL found in the stringified event
    values. Falls back to the plain-text regex over the whole output if nothing is found.

    WHY scan stringified values rather than a known key: the PR URL can surface in a tool
    result, an assistant text block, or a final summary - the key varies by engine version,
    the URL shape does not.
    """
    found: str | None = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # not JSON (banner, log line) - skip, never raise
        match = PR_URL_RE.search(json.dumps(event))
        if match:
            found = match.group(0)  # keep walking; last wins
    return found or _fallback_pr_url(output)


class Engine:
    """Base engine. Subclasses set `name` and implement the three behaviours."""

    name: str = ""

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        raise NotImplementedError

    def parse_pr_url(self, output: str) -> str | None:
        raise NotImplementedError

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        raise NotImplementedError


class PiEngine(Engine):
    name = "pi"

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        argv = ["pi", "-p", prompt, "--mode", "json"]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str) -> str | None:
        return _scan_jsonl_for_pr_url(output)

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        """The provider vars that ARE set (non-empty) in `env` (defaults to os.environ).
        Empty list => no creds => fail-closed. Takes `env` so load_config gates against the
        same mapping it resolves the rest of the config from."""
        env = os.environ if env is None else env
        return [v for v in PI_PROVIDER_VARS if env.get(v)]


class ClaudeEngine(Engine):
    name = "claude"

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        argv = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str) -> str | None:
        return _scan_jsonl_for_pr_url(output)

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        return [CLAUDE_TOKEN_VAR]


ENGINES: dict[str, type[Engine]] = {"pi": PiEngine, "claude": ClaudeEngine}
DEFAULT_ENGINE = "pi"


def resolve_engine(flag: str | None, env: Mapping[str, str]) -> Engine:
    """flag > env['FRANKY_ENGINE'] > DEFAULT_ENGINE. Unknown name is a hard error so a
    typo never silently falls back to the default engine."""
    name = flag or env.get("FRANKY_ENGINE") or DEFAULT_ENGINE
    cls = ENGINES.get(name)
    if cls is None:
        known = ", ".join(sorted(ENGINES))
        raise ValueError(f"unknown engine '{name}' (known engines: {known})")
    return cls()
