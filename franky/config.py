"""Config + secret redaction. Fail-closed by design: a missing allowlist, a missing
GitHub token, or missing engine creds all refuse rather than run degraded.

WHY redaction lives here and is load-bearing: the engine cred and GH_TOKEN values pass
through the container env, and the agent's captured stdout/stderr is logged to disk and
printed. A secret value must never survive into a log file or the terminal.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .engine import (
    CODEX_AUTH_VOLUME,
    CODEX_SUBSCRIPTION_VAR,
    Engine,
    opencode_provider,
    resolve_engine,
)
from .result import AuthError, ConfigError

REDACT_TOKEN = "***REDACTED***"

GH_TOKEN_VAR = "GH_TOKEN"
ALLOWED_REPOS_VAR = "FRANKY_ALLOWED_REPOS"
EXTRA_ALLOWED_DOMAINS_VAR = "FRANKY_EXTRA_ALLOWED_DOMAINS"
MODEL_VAR = "FRANKY_MODEL"

# Valid allowlist entry pattern: the literal "*" (match any repo) OR exactly one "/"
# with GitHub-compatible owner/name segments (alphanumeric, dash, underscore, dot).
# The NAME segment also accepts "*" as a glob wildcard (so "my-org/*" and "my-org/team-*"
# work); the OWNER segment does NOT - an owner glob like "*/repo" would silently match
# every owner, a surprising breadth for a security boundary, so it is rejected (the only
# way to match across owners is the deliberate bare "*"). Empty segments and extra
# slashes are rejected.
# WHY validate strictly: a malformed entry is almost certainly a typo (e.g. a bare
# "owner" with no slash, or a triple-component "org/team/repo") and silently accepting
# it would produce confusing behaviour at runtime.
_ALLOWLIST_ENTRY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.*-]+$")

# A concrete repo is always exactly "owner/name": two non-empty, slash-free segments.
# This guards repo_allowed against a multi-slash repo (e.g. "owner/sub/path") sneaking
# past an "owner/*" pattern by matching the trailing "sub/path" against the name glob.
_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret VALUE occurrence with the redaction token.

    Longest-first so a secret that is a substring of another is masked completely (the
    shorter one would otherwise leave a tail). Empty/None secrets are skipped - replacing
    "" would corrupt the whole string.
    """
    if not text:
        return text
    real = sorted({s for s in secrets if s}, key=len, reverse=True)
    for secret in real:
        text = text.replace(secret, REDACT_TOKEN)
    return text


@dataclass
class Config:
    engine: Engine
    allowed_repos: list[str]
    passthrough_env: dict[str, str] = field(default_factory=dict)
    extra_allowed_domains: list[str] = field(default_factory=list)
    auth_volume: str | None = None
    codex_mcp_overrides: list[str] = field(default_factory=list)
    claude_mcp_config_path: str | None = None
    model: str | None = None

    def secret_values(self) -> list[str]:
        """Secret strings known to the host and therefore available for output redaction.

        Subscription auth.json never crosses the Docker volume boundary, so its contents are
        intentionally neither read nor returned here.
        """
        return [v for v in self.passthrough_env.values() if v]


def validate_allowlist_entry(entry: str) -> None:
    """Raise ValueError if `entry` is not a valid allowlist pattern.

    Valid forms:
      - "*"  (literal asterisk - matches any repo the token can reach)
      - "owner/repo" style: GitHub charset (A-Za-z0-9_.-) in the owner segment, plus
        an optional glob "*" in the NAME segment only ("my-org/*", "my-org/team-*").
        An owner glob ("*/repo") is rejected - the only cross-owner match is bare "*".

    Malformed entries (bare owner, triple-slash, empty segment, whitespace, owner
    glob) are rejected so load_config surfaces them immediately rather than silently
    producing a surprising or non-matching allowlist.
    """
    if entry == "*":
        return
    if not _ALLOWLIST_ENTRY_RE.match(entry):
        raise ConfigError(
            f"invalid allowlist entry {entry!r}: expected 'owner/repo' or 'owner/*' "
            "or '*' (each segment: alphanumeric/dash/underscore/dot/asterisk, "
            "exactly one slash)"
        )


def load_config(flag_engine: str | None, env: Mapping[str, str]) -> Config:
    """Resolve engine + build the fail-closed passthrough env.

    Order of refusals (all fail-closed; no exception message ever contains a secret VALUE,
    only the VAR name):
      1. allowlist unset/blank -> refuse (we will not act on an open set of repos)
      2. GH_TOKEN missing -> refuse (cannot clone or open a PR without it)
      3. OpenCode model missing/invalid -> refuse
      4. engine creds missing -> refuse (pi: no provider var set; claude: token missing;
         codex: CODEX_API_KEY unset; opencode: selected provider key unset)
    """
    # resolve_engine raises a plain ValueError for an unknown FRANKY_ENGINE; rewrap as a
    # ConfigError so it gets exit code 3 + a JSON error, keeping the message identical.
    try:
        engine = resolve_engine(flag_engine, env)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    raw_allowed = env.get(ALLOWED_REPOS_VAR, "") or ""
    allowed = [r.strip() for r in raw_allowed.split(",") if r.strip()]
    if not allowed:
        raise ConfigError(
            f"{ALLOWED_REPOS_VAR} is unset or empty - refusing (set a comma-separated "
            "owner/repo allowlist)"
        )
    # Validate each pattern now, at load time, so a malformed entry surfaces as a
    # clean ClickException rather than silently failing to match anything at runtime.
    for entry in allowed:
        validate_allowlist_entry(entry)

    gh_token = env.get(GH_TOKEN_VAR)
    if not gh_token:
        raise AuthError(
            f"{GH_TOKEN_VAR} is unset or empty - refusing (needed to clone + open the PR)"
        )

    model = env.get(MODEL_VAR) or None
    opencode_credential: str | None = None
    if engine.name == "opencode":
        provider = opencode_provider(model)
        if provider is None:
            raise ConfigError(
                f"{MODEL_VAR} must select a supported OpenCode provider, for example "
                "'moonshotai/kimi-k3' or 'openrouter/<model-id>'"
            )
        opencode_credential = provider[0]

    cred_vars = engine.required_env(env, model)
    subscription_auth = (
        engine.name == "codex" and env.get(CODEX_SUBSCRIPTION_VAR) == "1" and not cred_vars
    )
    if not cred_vars and not subscription_auth:
        if opencode_credential:
            raise AuthError(
                f"{opencode_credential} is unset or empty - refusing "
                f"(required by {MODEL_VAR}={model})"
            )
        # The hint comes from the engine itself so the refusal names THIS engine's vars -
        # shared config stays engine-agnostic (no hardcoded pi vars).
        raise AuthError(
            f"no creds present for engine '{engine.name}' - refusing ({engine.cred_hint()})"
        )

    passthrough: dict[str, str] = {GH_TOKEN_VAR: gh_token}
    missing: list[str] = []
    for var in cred_vars:
        value = env.get(var)
        if not value:
            missing.append(var)
            continue
        passthrough[var] = value
    if missing:
        raise AuthError(
            f"engine '{engine.name}' requires {', '.join(missing)} but they are unset - refusing"
        )

    # Extra egress-allowlist domains are OPTIONAL (not fail-closed): the default allowlist
    # already covers the provider + GitHub + registries; this is for the occasional extra
    # host a specific task needs. Domains are policy, not secrets, so no redaction concern.
    raw_extra = env.get(EXTRA_ALLOWED_DOMAINS_VAR, "") or ""
    extra_domains = [d.strip() for d in raw_extra.split(",") if d.strip()]

    return Config(
        engine=engine,
        allowed_repos=allowed,
        passthrough_env=passthrough,
        extra_allowed_domains=extra_domains,
        auth_volume=CODEX_AUTH_VOLUME if subscription_auth else None,
        model=model,
    )


def repo_allowed(repo: str, allowed: list[str]) -> bool:
    """Return True if `repo` matches any pattern in `allowed`.

    Matching rules (segment-wise, case-insensitive):
    - Pattern "*" -> matches ANY repo unconditionally.
    - Otherwise split BOTH repo and pattern on the FIRST "/" into (owner, name),
      then fnmatch.fnmatchcase on lowercased owner and name independently.

    WHY segment-wise instead of a single fnmatch over "owner/repo":
      A pattern like "owner*" with plain fnmatch would match "owner-evil/repo" AND
      "owner/repo" - both segments must match independently.  More importantly,
      "owner*" must NOT match "owner-evil/anything" by crossing the "/" boundary.
      Splitting on "/" and matching each segment in isolation is the fix.

    WHY case-insensitive: GitHub org/repo names are case-preserving but case-
    insensitive in practice (github.com/OWNER/REPO and github.com/owner/repo
    resolve to the same thing).
    """
    if not _REPO_RE.match(repo):
        # repo must be exactly "owner/name" (two non-empty, slash-free segments). A
        # multi-slash repo like "owner/sub/path" must NOT satisfy an "owner/*" pattern
        # by matching "sub/path" against the name glob.
        return False
    r_owner, r_name = repo.lower().split("/", 1)

    for pattern in allowed:
        if pattern == "*":
            return True
        p_parts = pattern.lower().split("/", 1)
        if len(p_parts) != 2:
            continue  # should have been caught by validate_allowlist_entry; skip
        p_owner, p_name = p_parts
        if fnmatch.fnmatchcase(r_owner, p_owner) and fnmatch.fnmatchcase(r_name, p_name):
            return True
    return False
