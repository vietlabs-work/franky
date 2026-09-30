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

# codex exec reads CODEX_API_KEY for one non-interactive run. OPENAI_API_KEY remains a pi
# provider credential; codex only uses it when persisted through `codex login --with-api-key`,
# which Franky's fresh container deliberately does not do.
CODEX_PROVIDER_VARS = ("CODEX_API_KEY",)
CODEX_PROVIDER_HOST = "api.openai.com"
CODEX_SUBSCRIPTION_HOSTS = ("chatgpt.com", "auth.openai.com")
CODEX_SUBSCRIPTION_VAR = "FRANKY_CODEX_SUBSCRIPTION"
CODEX_AUTH_VOLUME = "franky-codex-auth"
FRANKY_CODEX_AUTH_VOLUME_VAR = "FRANKY_CODEX_AUTH_VOLUME"

# Docker's own volume-name rule (`docker volume create` accepts this shape; anything else is
# rejected by the daemon before it ever runs). fullmatch (not search/match) so a trailing
# newline or any other trailing junk cannot sneak an otherwise-valid-looking prefix past it.
_VOLUME_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")


def codex_auth_volume(env: Mapping[str, str]) -> str:
    """Resolve the Codex subscription auth volume name (default `franky-codex-auth`).

    Every reader of the volume name - the container mount, `auth login`/`status`/`logout`, and
    the pre-run scrub - MUST call this instead of touching CODEX_AUTH_VOLUME directly, so two
    Franky instances on one machine can each point FRANKY_CODEX_AUTH_VOLUME at their own volume
    and never touch each other's live Codex session. Fail-closed: an override that does not
    match Docker's own volume-name rule raises rather than reaching `docker volume create` with
    a name Docker itself would refuse. Unset -> the default; set-but-empty or set-but-malformed
    (including a stray trailing newline from a sourced env file) both raise - only a genuinely
    absent var falls back.
    """
    raw = env.get(FRANKY_CODEX_AUTH_VOLUME_VAR)
    if raw is None:
        return CODEX_AUTH_VOLUME
    if not _VOLUME_NAME_RE.fullmatch(raw):
        raise ValueError(
            f"{FRANKY_CODEX_AUTH_VOLUME_VAR}={raw!r} is not a valid Docker volume name "
            "(must match ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$)"
        )
    return raw


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

OPENCODE_PROVIDERS: dict[str, tuple[str, str]] = {
    "moonshotai": ("MOONSHOT_API_KEY", "api.moonshot.ai"),
    "openrouter": ("OPENROUTER_API_KEY", "openrouter.ai"),
}
_OPENCODE_MODEL_RE = re.compile(r"^[^/\s]+(?:/[^/\s]+)+$")


def opencode_provider(model: str | None) -> tuple[str, str] | None:
    """Return the credential variable and API host selected by an OpenCode model."""
    if not model or not _OPENCODE_MODEL_RE.fullmatch(model):
        return None
    prefix = model.split("/", 1)[0]
    if prefix == "moonshotai" and model != "moonshotai/kimi-k3":
        return None
    return OPENCODE_PROVIDERS.get(prefix)


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


def _scan_jsonl_for_pr_url(output: str, repo: str | None = None) -> str | None:
    """Return the first PR URL in the last matching JSON event, else the last plain URL.
    Collect both candidates in one bounded pass. Skip JSON decoding for ordinary logs.
    Matches are scoped to `repo` when given (see _pr_url_pattern).

    WHY scan stringified values rather than a known key: the PR URL can surface in a tool
    result, an assistant text block, or a final summary - the key varies by engine version,
    the URL shape does not.
    """
    pattern = _pr_url_pattern(repo)
    found: str | None = None
    fallback: str | None = None
    from .transcript import lines

    for line in lines(output):
        if line is None:
            return None
        has_url = "https://github.com/" in line
        if found is None and has_url:
            for match in pattern.finditer(line):
                fallback = match.group(0)
        line = line.strip()
        # Objects, arrays, and strings can contain URLs, including JSON-escaped URLs.
        if not line.startswith(("{", "[", '"')):
            continue
        # A JSON escape can hide any URL character, so escaped events still need decoding.
        if not has_url and "\\" not in line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            continue  # not JSON (banner, log line) - skip, never raise
        match = pattern.search(json.dumps(event))
        if match:
            found = match.group(0)  # keep walking; last wins
    return found or fallback


def _tool_use_summary(name: object, inp: dict) -> str:
    """Return a compact `franky: <verb> <detail>` line for a tool-use event.

    Shared by all engine distillers so the wording is consistent across engines.
    """
    name = name if isinstance(name, str) else ""
    lower_name = name.lower()
    if lower_name in ("edit", "multiedit"):
        path = str(inp.get("file_path", "") or inp.get("filePath", "") or inp.get("path", ""))[:60]
        return f"franky: editing {path}" if path else "franky: editing file"
    if lower_name == "write":
        path = str(inp.get("file_path", "") or inp.get("filePath", "") or inp.get("path", ""))[:60]
        return f"franky: writing {path}" if path else "franky: writing file"
    if lower_name == "read":
        path = str(inp.get("file_path", "") or inp.get("filePath", "") or inp.get("path", ""))[:60]
        return f"franky: reading {path}" if path else "franky: reading file"
    if lower_name in ("bash", "execute_bash"):
        cmd = str(inp.get("command", "") or inp.get("cmd", "")).split("\n")[0][:60]
        return f"franky: running: {cmd}" if cmd else "franky: running command"
    if lower_name in ("glob", "globtool", "grep", "search"):
        pat = str(inp.get("pattern", "") or inp.get("query", ""))[:60]
        return f"franky: searching: {pat}" if pat else "franky: searching"
    if name:
        return f"franky: {lower_name[:60]}"
    return "franky: tool call"


def tool_name(line: str) -> str | None:
    """The tool NAME a JSONL event calls (any engine's shape), or None.

    Feeds the liveness heartbeat. Reads only the name, never the arguments, because arguments
    can carry decoded secret fragments that must not reach a stored file.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") == "tool_use":
        part = event.get("part")
        name = event.get("name") or (part.get("tool") if isinstance(part, dict) else None)
        return name if isinstance(name, str) and name else None
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "command_execution":
        return "command_execution"  # codex
    message = event.get("message")
    for content in (
        event.get("content"),
        message.get("content") if isinstance(message, dict) else None,
    ):
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                return name if isinstance(name, str) and name else None
    return None


class Engine:
    """Base engine. Subclasses set `name` and implement the required behaviours."""

    name: str = ""

    # Whether an operator can inject a mid-run correction via the steer-file mailbox (issue
    # #72, `franky job attach`). True means the engine's PROMPT tells it to poll the steer
    # file - not that the engine natively polls a filesystem path itself. The polling behavior
    # comes entirely from the conventions block build_prompt/build_replay_prompt/
    # build_resume_prompt/build_iterate_prompt append (see prompt.py's _STEER_CONVENTION); an
    # engine that ignores its own prompt instructions would not actually pick up a correction
    # even with this flag True, but every current engine follows its system prompt closely
    # enough for this best-effort channel to work.
    supports_steering: bool = False

    # HOME-relative directory holding the engine's resumable session files (`review-pr
    # --thread`, threads.py). Empty means the engine has no native resume, so a thread seeds a
    # fresh session from its stored handoff instead.
    session_dir: str = ""

    def inner_argv(
        self, prompt: str, model: str | None, *, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
        """Headless argv. `session_id`/`resume` matter only when `session_dir` is set; every
        other engine ignores them and returns the same argv as without them."""
        raise NotImplementedError

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        raise NotImplementedError

    def required_env(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        raise NotImplementedError

    def provider_hosts(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
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

    def distill_line(self, line: str) -> str | None:
        """Return a compact progress summary for one redacted output line, or None to skip.

        Called for each streamed line in distilled-progress mode (the default, without
        --verbose). Return None to suppress the line entirely; return a short string to
        print it to stderr. The base implementation always returns None; subclasses override
        for their specific JSONL event schemas.
        """
        return None


class PiEngine(Engine):
    name = "pi"
    supports_steering = True

    def inner_argv(
        self, prompt: str, model: str | None, *, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
        argv = ["pi", "-p", prompt, "--mode", "json"]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        """The provider vars that ARE set (non-empty) in `env` (defaults to os.environ).
        Empty list => no creds => fail-closed. Takes `env` so load_config gates against the
        same mapping it resolves the rest of the config from."""
        env = os.environ if env is None else env
        return [v for v in PI_PROVIDER_VARS if env.get(v)]

    def provider_hosts(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
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

    def distill_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        # Top-level tool_use event (direct pi format)
        if event_type == "tool_use":
            return _tool_use_summary(
                event.get("name", ""), event.get("input") or event.get("arguments") or {}
            )
        # Tool use nested in an assistant/message content block
        if event_type == "message":
            for item in event.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    return _tool_use_summary(item.get("name", ""), item.get("input") or {})
        if event_type == "done":
            return "franky: agent complete"
        return None


class ClaudeEngine(Engine):
    name = "claude"
    supports_steering = True
    # Claude keys sessions by the slugged cwd; the engine always runs in /work.
    session_dir = ".claude/projects/-work"

    def inner_argv(
        self, prompt: str, model: str | None, *, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            # claude rejects -p + stream-json without --verbose ("requires --verbose").
            # We parse the JSONL stream for the PR URL, so stream-json is mandatory.
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            # Plain --resume keeps the id (only --fork-session mints a new one).
            argv += ["--resume" if resume else "--session-id", session_id]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        # env is part of the Engine interface but unused here: claude always needs exactly
        # this one token, regardless of what else is in the environment.
        return [CLAUDE_TOKEN_VAR]

    def provider_hosts(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        # Like required_env, claude ignores env: it always talks to exactly one host.
        return ["api.anthropic.com"]

    def cred_hint(self) -> str:
        return f"set {CLAUDE_TOKEN_VAR}"

    def distill_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        # Tool-use events arrive inside the assistant message content list
        if event_type == "assistant":
            msg = event.get("message")
            if isinstance(msg, dict):
                for item in msg.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        return _tool_use_summary(item.get("name", ""), item.get("input") or {})
        if event_type == "result":
            return "franky: agent complete"
        return None


class CodexEngine(Engine):
    name = "codex"
    supports_steering = True

    def inner_argv(
        self, prompt: str, model: str | None, *, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
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
            "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        """Return CODEX_API_KEY when set, else empty so config fails closed."""
        env = os.environ if env is None else env
        return [v for v in CODEX_PROVIDER_VARS if env.get(v)]

    def provider_hosts(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        env = os.environ if env is None else env
        if any(env.get(v) for v in CODEX_PROVIDER_VARS):
            return [CODEX_PROVIDER_HOST]
        if env.get(CODEX_SUBSCRIPTION_VAR) == "1":
            return list(CODEX_SUBSCRIPTION_HOSTS)
        return []

    def cred_hint(self) -> str:
        return f"set {CODEX_PROVIDER_VARS[0]} or run `franky auth login codex`"

    def distill_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        item = event.get("item")
        if isinstance(item, dict):
            if event_type == "item.started" and item.get("type") == "command_execution":
                cmd = str(item.get("command", "")).split("\n")[0][:60]
                return f"franky: running: {cmd}" if cmd else "franky: running command"
            if event_type == "item.completed" and item.get("type") == "file_change":
                path = item.get("path", "")
                changes = item.get("changes")
                if not path and isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict) and change.get("path"):
                            path = change["path"]
                            break
                return f"franky: writing {path}" if path else None
        if event_type == "turn.completed":
            return "franky: agent complete"
        if event_type == "turn.failed":
            return "franky: agent failed"
        if event_type == "error":
            return "franky: agent error"
        return None


class OpenCodeEngine(Engine):
    name = "opencode"

    def inner_argv(
        self, prompt: str, model: str | None, *, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
        return [
            "opencode",
            "run",
            "--format",
            "json",
            "--auto",
            "--pure",
            "--model",
            model or "",
            prompt,
        ]

    def parse_pr_url(self, output: str, repo: str | None = None) -> str | None:
        return _scan_jsonl_for_pr_url(output, repo)

    def required_env(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        env = os.environ if env is None else env
        provider = opencode_provider(model)
        return [provider[0]] if provider and env.get(provider[0]) else []

    def provider_hosts(
        self, env: Mapping[str, str] | None = None, model: str | None = None
    ) -> list[str]:
        provider = opencode_provider(model)
        return [provider[1]] if provider else []

    def cred_hint(self) -> str:
        return "set MOONSHOT_API_KEY or OPENROUTER_API_KEY for the selected FRANKY_MODEL"

    def distill_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type == "error":
            return "franky: agent error"
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        if event_type == "step_finish":
            return "franky: step complete"
        if event_type != "tool_use":
            return None
        state = part.get("state")
        if not isinstance(state, dict):
            return None
        inp = state.get("input")
        tool = part.get("tool")
        if not isinstance(inp, dict):
            return None
        return _tool_use_summary(tool, inp)


ENGINES: dict[str, type[Engine]] = {
    "pi": PiEngine,
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "opencode": OpenCodeEngine,
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
