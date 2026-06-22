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
from urllib.parse import urlparse

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

# codex authenticates with an API key. BOTH vars work: CODEX_API_KEY is the automation-
# recommended one, OPENAI_API_KEY also authenticates (it doubles as the codex key). Gate on
# whichever is present, BYOK-style like pi - so a keyless codex run is the fail-closed case.
# OPENAI_API_KEY is deliberately shared with PI_PROVIDER_VARS: it is a different engine, gated
# independently, and build_allowlist only ever consults the ONE resolved engine's hosts.
CODEX_PROVIDER_VARS = ("CODEX_API_KEY", "OPENAI_API_KEY")
CODEX_PROVIDER_HOST = "api.openai.com"

# Which network host each provider cred talks to. WHY this is SEPARATE from required_env:
# required_env is about cred gating (do we have a key at all), this is about the egress
# allowlist (which host must the proxy let through so the engine can reach its provider).
# OLLAMA_HOST is deliberately absent here - its value is a URL, not a fixed host, so it is
# parsed at runtime (see PiEngine.provider_hosts).
PI_PROVIDER_HOSTS = {
    "ANTHROPIC_API_KEY": "api.anthropic.com",
    "ANTHROPIC_OAUTH_TOKEN": "api.anthropic.com",
    "OPENROUTER_API_KEY": "openrouter.ai",
    "OPENAI_API_KEY": "api.openai.com",
    "GEMINI_API_KEY": "generativelanguage.googleapis.com",
    "GROQ_API_KEY": "api.groq.com",
    "MISTRAL_API_KEY": "api.mistral.ai",
}


def _ollama_host(value: str) -> str | None:
    """Parse the host out of an OLLAMA_HOST value. The value is a URL (e.g.
    http://host:11434); urlparse gives us its netloc host. If there is no scheme, urlparse
    treats the whole thing as a path, so fall back to the bare value as a host. Returns None
    for an empty value (nothing to allow)."""
    value = value.strip()
    if not value:
        return None
    host = urlparse(value).hostname
    if host:
        return host
    # No scheme -> urlparse parsed it as a path. Re-parse with a dummy `//` prefix so netloc
    # parsing handles host[:port] AND bracketed IPv6 ([::1]:11434 -> ::1) correctly.
    return urlparse(f"//{value}").hostname


def _pr_url_pattern(repo: str | None) -> re.Pattern[str]:
    """A PR-URL regex scoped to `repo` (owner/repo) when given, else the generic one.
    WHY scope it: the agent echoes issue/comment bodies, so a hostile issue can plant a PR
    URL for an attacker repo. Anchoring to the task's own repo means we never report a URL
    that points somewhere we were not asked to act on."""
    if not repo:
        return PR_URL_RE
    return re.compile(r"https://github\.com/" + re.escape(repo) + r"/pull/\d+")


def _fallback_pr_url(output: str, pattern: re.Pattern[str]) -> str | None:
    """Return the LAST PR URL matching `pattern` in plain text, or None. Used when
    structured per-line parsing yields nothing."""
    matches = pattern.findall(output)
    return matches[-1] if matches else None


def _scan_jsonl_for_pr_url(output: str, repo: str | None = None) -> str | None:
    """Both engines emit one JSON object per line. Walk lines, json.loads each (skip any
    non-JSON line, never raise), and return the last PR URL found in the stringified event
    values. Falls back to the plain-text regex over the whole output if nothing is found.
    Matches are scoped to `repo` when given (see _pr_url_pattern).

    WHY scan stringified values rather than a known key: the PR URL can surface in a tool
    result, an assistant text block, or a final summary - the key varies by engine version,
    the URL shape does not.
    """
    pattern = _pr_url_pattern(repo)
    found: str | None = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # not JSON (banner, log line) - skip, never raise
        match = pattern.search(json.dumps(event))
        if match:
            found = match.group(0)  # keep walking; last wins
    return found or _fallback_pr_url(output, pattern)


class Engine:
    """Base engine. Subclasses set `name` and implement the required behaviours."""

    name: str = ""

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        raise NotImplementedError

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        raise NotImplementedError

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        raise NotImplementedError

    def provider_hosts(self, env: Mapping[str, str] | None = None) -> list[str]:
        """The network hosts the engine must reach to talk to its model provider. Feeds the
        egress allowlist. SEPARATE from required_env (cred gating) on purpose - same source
        data, different concern."""
        raise NotImplementedError

    def cred_hint(self) -> str:
        """Operator-facing description of which cred var(s) to set when NONE are present, for
        load_config's fail-closed refusal. Each engine owns its own hint so the refusal names
        THIS engine's vars - shared config never hardcodes one engine's vars. Only called when
        `required_env` returns [] (no creds at all), so it answers "what should the operator
        set" rather than "which creds are usable now"."""
        raise NotImplementedError


class PiEngine(Engine):
    name = "pi"

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        argv = ["pi", "-p", prompt, "--mode", "json"]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        """The provider vars that ARE set (non-empty) in `env` (defaults to os.environ).
        Empty list => no creds => fail-closed. Takes `env` so load_config gates against the
        same mapping it resolves the rest of the config from."""
        env = os.environ if env is None else env
        return [v for v in PI_PROVIDER_VARS if env.get(v)]

    def provider_hosts(self, env: Mapping[str, str] | None = None) -> list[str]:
        """For each provider var actually set in `env` (defaults to os.environ), the host it
        talks to. OLLAMA_HOST is parsed from its URL value; the rest map via
        PI_PROVIDER_HOSTS. Deduped, empties dropped, sorted for a deterministic allowlist."""
        env = os.environ if env is None else env
        hosts: set[str] = set()
        # Iterate the KNOWN provider vars (not env.items()) so an unrelated env var that
        # happens to collide with a map key can never widen the allowlist. Mirrors required_env.
        for var in PI_PROVIDER_VARS:
            value = env.get(var)
            if not value:
                continue
            if var == "OLLAMA_HOST":
                host = _ollama_host(value)
                if host:
                    hosts.add(host)
            elif var in PI_PROVIDER_HOSTS:
                hosts.add(PI_PROVIDER_HOSTS[var])
        return sorted(hosts)

    def cred_hint(self) -> str:
        # pi is BYOK: any one of the provider vars is enough, so list them all.
        return f"set one of: {', '.join(PI_PROVIDER_VARS)}"


class ClaudeEngine(Engine):
    name = "claude"

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        # env is part of the Engine interface but unused here: claude always needs exactly
        # this one token, regardless of what else is in the environment.
        return [CLAUDE_TOKEN_VAR]

    def provider_hosts(self, env: Mapping[str, str] | None = None) -> list[str]:
        # Like required_env, claude ignores env: it always talks to exactly one host.
        return ["api.anthropic.com"]

    def cred_hint(self) -> str:
        return f"set {CLAUDE_TOKEN_VAR}"


class CodexEngine(Engine):
    name = "codex"

    def inner_argv(self, prompt: str, model: str | None) -> list[str]:
        # --dangerously-bypass-approvals-and-sandbox is load-bearing, not a convenience: codex
        # both prompts for approval AND self-sandboxes (Landlock/seccomp). A headless run must
        # have approvals off, and the nested self-sandbox is redundant-and-fragile inside
        # Franky's already-hardened container, so we bypass it and trust the container - the
        # same bargain claude makes with --dangerously-skip-permissions. See the per-engine
        # guardrail-bypass invariant in AGENTS.md.
        argv = [
            "codex",
            "exec",
            prompt,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(self, env: Mapping[str, str] | None = None) -> list[str]:
        """The codex provider vars that ARE set (non-empty) in `env` (defaults to os.environ).
        Empty list => no creds => fail-closed. Mirrors PiEngine: gate on whichever key the
        operator actually set."""
        env = os.environ if env is None else env
        return [v for v in CODEX_PROVIDER_VARS if env.get(v)]

    def provider_hosts(self, env: Mapping[str, str] | None = None) -> list[str]:
        # Both codex keys talk to the same host, so this is all-or-nothing: the host opens iff
        # ANY codex key is present. Gating on key presence (rather than claude's unconditional
        # return) keeps required_env and provider_hosts parallel and never opens api.openai.com
        # for a keyless run.
        env = os.environ if env is None else env
        if any(env.get(v) for v in CODEX_PROVIDER_VARS):
            return [CODEX_PROVIDER_HOST]
        return []

    def cred_hint(self) -> str:
        # codex accepts either var; CODEX_API_KEY is the automation-recommended one.
        return f"set one of: {', '.join(CODEX_PROVIDER_VARS)}"


ENGINES: dict[str, type[Engine]] = {
    "pi": PiEngine,
    "claude": ClaudeEngine,
    "codex": CodexEngine,
}
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
