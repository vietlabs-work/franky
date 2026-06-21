"""Config + secret redaction. Fail-closed by design: a missing allowlist, a missing
GitHub token, or missing engine creds all refuse rather than run degraded.

WHY redaction lives here and is load-bearing: the engine cred and GH_TOKEN values pass
through the container env, and the agent's captured stdout/stderr is logged to disk and
printed. A secret value must never survive into a log file or the terminal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .engine import PI_PROVIDER_VARS, Engine, resolve_engine

REDACT_TOKEN = "***REDACTED***"

GH_TOKEN_VAR = "GH_TOKEN"
ALLOWED_REPOS_VAR = "FRANKY_ALLOWED_REPOS"
EXTRA_ALLOWED_DOMAINS_VAR = "FRANKY_EXTRA_ALLOWED_DOMAINS"


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

    def secret_values(self) -> list[str]:
        """The actual secret strings to scrub from any output. Just the values of the
        env we pass through - those are the only secrets that reach the container."""
        return [v for v in self.passthrough_env.values() if v]


def load_config(flag_engine: str | None, env: Mapping[str, str]) -> Config:
    """Resolve engine + build the fail-closed passthrough env.

    Order of refusals (all fail-closed; no exception message ever contains a secret VALUE,
    only the VAR name):
      1. allowlist unset/blank -> refuse (we will not act on an open set of repos)
      2. GH_TOKEN missing -> refuse (cannot clone or open a PR without it)
      3. engine creds missing -> refuse (pi: no provider var set; claude: token missing)
    """
    engine = resolve_engine(flag_engine, env)

    raw_allowed = env.get(ALLOWED_REPOS_VAR, "") or ""
    allowed = [r.strip() for r in raw_allowed.split(",") if r.strip()]
    if not allowed:
        raise ValueError(
            f"{ALLOWED_REPOS_VAR} is unset or empty - refusing (set a comma-separated "
            "owner/repo allowlist)"
        )

    gh_token = env.get(GH_TOKEN_VAR)
    if not gh_token:
        raise ValueError(
            f"{GH_TOKEN_VAR} is unset or empty - refusing (needed to clone + open the PR)"
        )

    cred_vars = engine.required_env(env)
    if not cred_vars:
        raise ValueError(
            f"no creds present for engine '{engine.name}' - refusing "
            f"(set one of: {', '.join(PI_PROVIDER_VARS)})"
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
        raise ValueError(
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
    )


def repo_allowed(repo: str, allowed: list[str]) -> bool:
    return repo in allowed
