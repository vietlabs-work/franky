"""Operator profile: inject the operator's agentic-coding setup + MCP config into the container.

WHY this is safe: the bundle is assembled on the host from a bounded allowlist (explicit
file lists, or a whole setup dir expanded through `setups.py`'s per-kind manifest - never
"tar the directory"), secret-scanned before packing (fail-closed on any credential hit), and
streamed into the container as a gzip tar that is unpacked into the HOME tmpfs before the
engine starts.  No bind mount, no live config dir, no credential-bearing file ever enters the
bundle.

Injection mechanism: the tar bytes are piped into the started container over
`docker exec -i` stdin (see container.deliver_profile) and extracted into HOME as uid 1001;
the entrypoint blocks on a ready marker until that lands, then execs the engine.  This
preserves the no-bind-mount invariant and the secret-by-name discipline:
  - No new bind mount (no host-FS path is ever mounted into the container).
  - Nothing rides the argv: the bundle content never appears in `ps`, and there is no
    argv-size ceiling (a swept setup is ~1 MB - a single `-e` value would break
    Linux's 128 KB MAX_ARG_STRLEN, which is why the earlier by-value env var is gone).
  - The hardening flags (_HARDENING) are unchanged; a `docker cp` INTO the `--read-only`
    task container is refused by the daemon, so stdin is also the only mechanism that
    works here - the same one `snapshot.restore_into_container` uses for `job resume`.

Security argument:
  - A malicious issue body cannot reach the profile bundle; the operator assembles it
    host-side before the task container ever starts.
  - Secret scanning runs fail-closed: a single detected credential pattern aborts the
    entire run before any container is started.  The operator must explicitly review and
    fix the file before it can be injected.
  - A setup sweep NEVER auto-enables MCP: each server would add an egress host to a
    default-deny proxy and forward a credential, so that stays an explicit declaration.

Tier-2 MCP files use the same bundle, but their named credentials and egress hosts are
validated separately before the bundle is built.
"""

from __future__ import annotations

import fnmatch
import io
import json
import math
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from . import setups

# The environment variable that puts the entrypoint into profile-wait mode: it blocks until the
# host has streamed the bundle in and touched the ready marker. Name only carries policy, never
# content (the bundle itself never touches the argv or the environment).
PROFILE_WAIT_VAR = "FRANKY_PROFILE_WAIT"

# HOME inside the task container: bundle members are HOME-relative, so this is the prefix that
# turns a host path into the in-container path the prompt can name literally.
CONTAINER_HOME = "/home/franky"

# Override the default profile path (~/.franky/profile.toml) for testing and CI.
PROFILE_PATH_VAR = "FRANKY_PROFILE_PATH"

# Default profile location relative to the user home directory.
_DEFAULT_PROFILE_RELATIVE = Path(".franky") / "profile.toml"

# The categories a profile.toml [profile] table may declare, in canonical order.
PROFILE_FILE_CATEGORIES = ("skills", "instructions", "knowledge", "mcp_configs")
PROFILE_VALUE_CATEGORIES = ("mcp_credentials", "mcp_domains")
PROFILE_CATEGORIES = PROFILE_FILE_CATEGORIES + PROFILE_VALUE_CATEGORIES

# The second top-level table: whole agentic-coding setups declared by directory,
# `[setups] claude = "~/.claude"`. Expanded through setups.SETUP_MANIFESTS.
SETUPS_TABLE = "setups"

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_ANY_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CODEX_MCP_CONFIG = Path(".codex/franky-mcp.config.toml")
_CLAUDE_MCP_CONFIG = Path(".claude/franky-mcp.json")
CLAUDE_MCP_CONTAINER_PATH = "/home/franky/.claude/franky-mcp.json"
_RESERVED_MCP_CREDENTIALS = frozenset(
    {
        "HOME",
        "PATH",
        "SHELL",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "CODEX_HOME",
    }
)

# Credential patterns to detect in profile files.  A match causes a fail-closed
# refusal.  Patterns are deliberately conservative (high-confidence) to avoid
# false positives on legitimate technical prose.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "PEM private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "GitHub personal/OAuth/app token",
        re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    ),
    (
        "Anthropic API key (sk-ant-...)",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b"),
    ),
    (
        "OpenRouter API key (sk-or-...)",
        re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{30,}\b"),
    ),
    (
        "OpenAI-style API key (sk-...)",
        re.compile(r"\bsk-[A-Za-z0-9_-]{48,}\b"),
    ),
    (
        "env-var assignment of a known secret variable",
        re.compile(
            r"(?m)^[ \t]*"
            r"(GH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_OAUTH_TOKEN|MOONSHOT_API_KEY"
            r"|OPENROUTER_API_KEY"
            r"|OPENAI_API_KEY|CODEX_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|GEMINI_API_KEY"
            r"|GROQ_API_KEY|MISTRAL_API_KEY|JIRA_API_TOKEN)"
            r"\s*=\s*[^\s#\n]"
        ),
    ),
]


@dataclass
class ProfileSpec:
    """The resolved, expanded file list from a profile.toml.

    Each list contains concrete, existing Path objects (no globs, no ~).
    Populated by load_profile(); build_bundle() consumes it.

    `setup_scans` holds the per-kind result of sweeping a declared `[setups]` directory (see
    setups.py). Its files join `all_files()`, so they go through the SAME fail-closed secret
    scan and the same HOME-relative packing as an explicitly listed file - a directory
    declaration is a shorthand for a file list, never a second, laxer path.
    """

    skills: list[Path] = field(default_factory=list)
    instructions: list[Path] = field(default_factory=list)
    knowledge: list[Path] = field(default_factory=list)
    mcp_configs: list[Path] = field(default_factory=list)
    mcp_credentials: list[str] = field(default_factory=list)
    mcp_domains: list[str] = field(default_factory=list)
    setup_scans: dict[str, setups.SetupScan] = field(default_factory=dict)

    @property
    def setup_files(self) -> list[Path]:
        """Every file swept from a declared setup, deduplicated, in kind-declaration order."""
        seen: dict[Path, None] = {}
        for scan in self.setup_scans.values():
            for path in scan.files:
                seen.setdefault(path, None)
        return list(seen)

    def setup_roots(self) -> dict[str, Path]:
        """The declared setup roots by kind, for prompt text and `profile check` reporting."""
        return {kind: scan.root for kind, scan in self.setup_scans.items()}

    def pr_spec(self) -> Path | None:
        """The operator's PR-description spec discovered in a swept setup, if any.

        Deliberately derived from the setup rather than declared as its own profile key: the PR
        spec is one file among the operator's commands, so it follows whatever setup they feed
        Franky instead of being configured twice.
        """
        return setups.find_pr_spec(self.setup_files)

    def all_files(self) -> list[Path]:
        """All files across all categories, in declaration order.

        Explicit lists come first so that when a file is BOTH explicitly listed and swept from
        a setup, the dedup below keeps one copy (a tar with two members at one path would
        extract twice).
        """
        ordered = self.skills + self.instructions + self.knowledge + self.setup_files
        seen: dict[Path, None] = {}
        for path in ordered:
            seen.setdefault(path, None)
        # mcp_configs stay last and unmerged: they are packed at their own reserved paths.
        return [p for p in seen if p not in set(self.mcp_configs)] + self.mcp_configs


def profile_path(env: dict[str, str] | None = None) -> Path | None:
    """Return the resolved profile TOML path, or None if none is configured.

    Priority: FRANKY_PROFILE_PATH env override > default ~/.franky/profile.toml.
    When the override is set, it is returned even if the file does not exist (so the
    caller can emit a clear "not found" error rather than silently treating it as absent).
    When the default is checked, None is returned if the file does not exist (the profile
    is opt-in; an absent default is not an error).
    """
    if env is None:
        import os

        env = dict(os.environ)
    override = env.get(PROFILE_PATH_VAR)
    if override:
        return Path(override)
    default = Path.home() / _DEFAULT_PROFILE_RELATIVE
    return default if default.exists() else None


def profile_file_path(env: dict[str, str] | None = None) -> Path:
    """Return the resolved profile path, whether or not it exists.

    Priority: FRANKY_PROFILE_PATH override > default ~/.franky/profile.toml.  Unlike
    profile_path() (which returns None for an absent default so `build` treats it as
    "no profile"), this ALWAYS returns a path - it is the path the `franky profile`
    subcommands print and write, the same contract as userconfig.config_file_path().
    """
    if env is None:
        import os

        env = dict(os.environ)
    override = env.get(PROFILE_PATH_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_PROFILE_RELATIVE


def _expand_glob(raw: str, budget: setups.ReadBudget) -> list[Path]:
    """Expand a single path or glob pattern (after ~ expansion) into existing Paths."""
    expanded = Path(raw).expanduser()
    pattern = str(expanded)
    if any(c in pattern for c in ("*", "?", "[")):
        matches: list[Path] = []
        parts = expanded.parts
        if expanded.is_absolute():
            root = Path(expanded.anchor)
            parts = parts[1:]
        else:
            root = Path()
        pending = [(root, 0)]
        while pending:
            directory, index = pending.pop()
            segment = parts[index]
            last = index == len(parts) - 1
            if any(character in segment for character in ("*", "?", "[")):
                child_dirs: list[Path] = []
                for entry in setups._scandir_bounded(directory, budget):
                    if entry.name.startswith(".") and not segment.startswith("."):
                        continue
                    if not fnmatch.fnmatchcase(entry.name, segment):
                        continue
                    path = Path(entry.path)
                    if last:
                        setups.claim_file(path, budget)
                        matches.append(path)
                    else:
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                child_dirs.append(path)
                        except OSError:
                            continue
                if len(pending) + len(child_dirs) > budget.max_files:
                    raise ValueError(
                        f"profile glob traversal exceeds entry limit {budget.max_files}"
                    )
                pending.extend((path, index + 1) for path in reversed(child_dirs))
                continue

            path = directory / segment
            if last:
                if path.exists():
                    setups.claim_file(path, budget)
                    matches.append(path)
            elif path.is_dir():
                pending.append((path, index + 1))
        return sorted(matches)
    if not expanded.exists():
        return []
    setups.claim_file(expanded, budget)
    return [expanded]


def container_path(host_path: Path) -> str:
    """Where a bundled host file lands inside the container.

    Members are packed HOME-relative, so a host path under HOME maps onto CONTAINER_HOME.
    Anything outside HOME falls back to the flat `profile/<name>` layout build_bundle uses.
    """
    try:
        rel = host_path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return f"{CONTAINER_HOME}/profile/{host_path.name}"
    return f"{CONTAINER_HOME}/{rel}"


def _load_setups(raw, path: Path, budget: setups.ReadBudget) -> dict[str, setups.SetupScan]:
    """Parse and sweep the `[setups]` table: {kind: directory} -> {kind: SetupScan}.

    Fail-closed on an unknown kind (a typo'd `cluade = ...` must not silently inject nothing),
    a missing root, or a root outside HOME. The HOME rule is not cosmetic: bundle members are
    packed HOME-relative so the engine finds them where it would locally, and a root elsewhere
    has no such relative path.
    """
    if not isinstance(raw, dict):
        raise ValueError(f'[{SETUPS_TABLE}] in {path} must be a TOML table of kind = "dir"')

    home = Path.home().resolve()
    scans: dict[str, setups.SetupScan] = {}
    for kind, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(
                f"{SETUPS_TABLE}.{kind} must be a directory path string in {path}, got {value!r}"
            )
        if kind not in setups.SETUP_MANIFESTS:
            raise ValueError(
                f"unknown setup kind {kind!r} in {path} - supported kinds: "
                f"{', '.join(setups.SETUP_KINDS)}"
            )
        root = Path(value).expanduser()
        if not root.exists():
            raise ValueError(f"{SETUPS_TABLE}.{kind} directory not found: {value!r} (in {path})")
        try:
            root.resolve().relative_to(home)
        except ValueError as exc:
            raise ValueError(
                f"{SETUPS_TABLE}.{kind} directory {root} must be under HOME ({home}) - the "
                "bundle is unpacked relative to HOME inside the container"
            ) from exc
        scans[kind] = setups.expand_setup(kind, root, budget)
    return scans


def load_profile(path: Path) -> ProfileSpec:
    """Parse a profile.toml and return the expanded, concrete file list.

    Raises ValueError on:
    - TOML parse error
    - A literal path that does not exist (glob non-matches are silently empty)
    - A non-string or non-list entry in the TOML
    - An unknown `[setups]` kind, a root that is missing / not a directory / outside HOME,
      or a sweep over the size guards in setups.py
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    try:
        text = _read_profile_text(path)
    except OSError as exc:
        raise ValueError(f"could not read profile {path}: {exc}") from exc

    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise ValueError(f"malformed TOML in profile {path}: {exc}") from exc

    table = data.get("profile", {})
    if not isinstance(table, dict):
        raise ValueError(f"[profile] in {path} must be a TOML table, not {type(table).__name__}")

    spec = ProfileSpec()
    budget = setups.profile_budget()
    for category in PROFILE_FILE_CATEGORIES:
        raw_list = table.get(category, [])
        if not isinstance(raw_list, list):
            raise ValueError(f"profile.{category} must be a list of strings in {path}")
        resolved: list[Path] = []
        for entry in raw_list:
            if not isinstance(entry, str):
                raise ValueError(
                    f"profile.{category} entries must be strings, got {entry!r} in {path}"
                )
            is_glob = any(c in entry for c in ("*", "?", "["))
            if category == "mcp_configs" and is_glob:
                raise ValueError("profile.mcp_configs entries must be explicit files, not globs")
            try:
                paths = _expand_glob(entry, budget)
            except OSError as exc:
                raise ValueError(f"could not inspect profile file {entry!r}: {exc}") from exc
            if not paths and not is_glob:
                raise ValueError(f"profile file not found: {entry!r} (listed in {path})")
            resolved.extend(paths)
        setattr(spec, category, resolved)

    for category in PROFILE_VALUE_CATEGORIES:
        raw_list = table.get(category, [])
        if not isinstance(raw_list, list):
            raise ValueError(f"profile.{category} must be a list of strings in {path}")
        if not all(isinstance(entry, str) for entry in raw_list):
            raise ValueError(f"profile.{category} entries must be strings in {path}")
        setattr(spec, category, list(raw_list))

    spec.setup_scans = _load_setups(data.get(SETUPS_TABLE, {}), path, budget)

    for name in spec.mcp_credentials:
        if name.startswith("FRANKY_") or name in _RESERVED_MCP_CREDENTIALS:
            raise ValueError(f"MCP credential name {name!r} is a reserved runtime variable")
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid MCP credential name {name!r}: expected [A-Z_][A-Z0-9_]*")
    for domain in spec.mcp_domains:
        if not _valid_hostname(domain):
            raise ValueError(f"invalid MCP domain {domain!r}: expected a hostname only")

    home = Path.home().resolve()
    for config_path in spec.mcp_configs:
        try:
            config_path.resolve().relative_to(home)
        except ValueError as exc:
            raise ValueError(f"MCP config {config_path} must be under HOME ({home})") from exc
        if config_path.suffix.lower() not in {".json", ".toml"}:
            raise ValueError(f"MCP config {config_path} must be JSON or TOML")
    validate_mcp_configs(spec)

    return spec


def _valid_hostname(value: str) -> bool:
    if not value or len(value) > 253 or value.endswith(".") or any(c in value for c in "/:* "):
        return False
    labels = value.split(".")
    return all(
        0 < len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9-]+", label) is not None
        and label[0].isalnum()
        and label[-1].isalnum()
        for label in labels
    )


def _parse_mcp_config(path: Path, budget: setups.ReadBudget) -> dict:
    try:
        text = _read_profile_text(path, budget)
    except OSError as exc:
        raise ValueError(f"could not parse MCP config {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            parsed = json.loads(text)
        elif path.suffix.lower() == ".toml":
            try:
                import tomllib  # type: ignore[import-not-found]
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore[import-not-found,no-redef]
            parsed = tomllib.loads(text)
        else:
            raise ValueError(f"MCP config {path} must be JSON or TOML")
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse MCP config {path}: {exc}") from exc
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("MCP config"):
            raise
        raise ValueError(f"could not parse MCP config {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"MCP config {path} must contain a JSON object or TOML table")
    return parsed


def _credential_like_key(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return (
        normalized
        in {
            "token",
            "secret",
            "password",
            "credential",
            "credentials",
            "authorization",
            "bearer",
            "api_key",
            "apikey",
        }
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_api_key")
        or normalized.endswith("_credential")
        or normalized.endswith("_credentials")
        or normalized.endswith("_token_env_var")
        or normalized.endswith("_secret_env_var")
        or normalized.endswith("_password_env_var")
        or normalized.endswith("_api_key_env_var")
        or normalized.endswith("_credential_env_var")
    )


def _declared_credential_reference(value, credentials: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    if value in credentials:
        return True
    placeholder = _PLACEHOLDER_RE.fullmatch(value)
    return placeholder is not None and placeholder.group(1) in credentials


def _validate_mcp_value(
    value, *, credentials: set[str], domains: set[str], referenced: set[str], path: Path
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in credentials:
                if child != f"${{{key}}}":
                    raise ValueError(
                        f"MCP config {path} assigns a literal value to credential {key}"
                    )
                referenced.add(key)
            elif (
                _credential_like_key(str(key))
                and not (isinstance(child, str) and _ANY_PLACEHOLDER_RE.fullmatch(child))
                and not _declared_credential_reference(child, credentials)
            ):
                raise ValueError(
                    f"MCP config {path} credential-like field {key} must reference "
                    "a declared credential name"
                )
            _validate_mcp_value(
                child,
                credentials=credentials,
                domains=domains,
                referenced=referenced,
                path=path,
            )
        return
    if isinstance(value, list):
        for child in value:
            _validate_mcp_value(
                child,
                credentials=credentials,
                domains=domains,
                referenced=referenced,
                path=path,
            )
        return
    if not isinstance(value, str):
        return

    placeholder = _PLACEHOLDER_RE.fullmatch(value)
    if placeholder:
        name = placeholder.group(1)
        if name not in credentials:
            raise ValueError(f"MCP config {path} references undeclared credential {name}")
        referenced.add(name)
    elif _ANY_PLACEHOLDER_RE.search(value):
        raise ValueError(f"MCP config {path} credential references must be exact placeholders")
    elif value in credentials:
        referenced.add(value)

    from urllib.parse import urlsplit

    for raw_url in _URL_RE.findall(value):
        host = urlsplit(raw_url).hostname
        if not host or host.lower() not in domains:
            raise ValueError(
                f"MCP config {path} URL host {host or '<invalid>'!r} is not declared in mcp_domains"
            )


def validate_mcp_configs(spec: ProfileSpec) -> dict[Path, dict]:
    """Parse and validate every MCP config, returning its parsed object."""
    credentials = set(spec.mcp_credentials)
    domains = {domain.lower() for domain in spec.mcp_domains}
    referenced: set[str] = set()
    parsed: dict[Path, dict] = {}
    codex_path = (Path.home() / _CODEX_MCP_CONFIG).resolve()
    claude_path = (Path.home() / _CLAUDE_MCP_CONFIG).resolve()
    budget = setups.profile_budget()
    for config_path in spec.mcp_configs:
        document = _parse_mcp_config(config_path, budget)
        if config_path.resolve() == codex_path:
            if set(document) != {"mcp_servers"}:
                raise ValueError(
                    f"Codex MCP config {config_path} may contain only the top-level mcp_servers key"
                )
            if not isinstance(document["mcp_servers"], dict):
                raise ValueError(f"Codex MCP config {config_path} mcp_servers must be a table")
            for name, server in document["mcp_servers"].items():
                if not isinstance(server, dict):
                    raise ValueError(
                        f"Codex MCP config {config_path} server {name!r} must be a table"
                    )
                _toml_literal(server)
        if config_path.resolve() == claude_path:
            if set(document) != {"mcpServers"}:
                raise ValueError(
                    f"Claude MCP config {config_path} may contain only the top-level mcpServers key"
                )
            if not isinstance(document["mcpServers"], dict):
                raise ValueError(f"Claude MCP config {config_path} mcpServers must be an object")
        _validate_mcp_value(
            document,
            credentials=credentials,
            domains=domains,
            referenced=referenced,
            path=config_path,
        )
        parsed[config_path] = document
    missing = credentials - referenced
    if missing:
        raise ValueError(
            "MCP credential(s) not referenced by any config: " + ", ".join(sorted(missing))
        )
    return parsed


def resolve_mcp_credentials(spec: ProfileSpec, env: dict[str, str]) -> dict[str, str]:
    """Resolve declared MCP credential names from the process environment."""
    missing = [name for name in spec.mcp_credentials if not env.get(name)]
    if missing:
        raise ValueError(
            "MCP credential(s) missing from the process environment: " + ", ".join(missing)
        )
    resolved = {name: env[name] for name in spec.mcp_credentials}
    budget = setups.profile_budget()
    for config_path in spec.mcp_configs:
        try:
            text = _read_profile_text(config_path, budget)
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read MCP config {config_path}: {exc}") from exc
        for name, value in resolved.items():
            if value in text:
                raise ValueError(
                    f"MCP config {config_path} contains the literal value of credential {name}"
                )
    return resolved


def _toml_key(value: str) -> str:
    return value if _TOML_BARE_KEY_RE.fullmatch(value) else json.dumps(value)


def _toml_literal(value) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{_toml_key(str(key))} = {_toml_literal(child)}" for key, child in value.items()
            )
            + " }"
        )
    raise ValueError(f"unsupported Codex MCP config value type: {type(value).__name__}")


def codex_mcp_overrides(spec: ProfileSpec) -> list[str]:
    """Return safe `codex -c` values for the isolated Franky MCP config."""
    target = (Path.home() / _CODEX_MCP_CONFIG).resolve()
    parsed = validate_mcp_configs(spec)
    for path, document in parsed.items():
        if path.resolve() != target:
            continue
        return [
            f"mcp_servers.{_toml_key(str(name))}={_toml_literal(server)}"
            for name, server in document["mcp_servers"].items()
        ]
    return []


def claude_mcp_config_path(spec: ProfileSpec) -> str | None:
    """Return the fixed in-container path when the reserved Claude config is present."""
    target = (Path.home() / _CLAUDE_MCP_CONFIG).resolve()
    return (
        CLAUDE_MCP_CONTAINER_PATH
        if any(path.resolve() == target for path in spec.mcp_configs)
        else None
    )


def validate_mcp_engine(spec: ProfileSpec, engine_name: str) -> None:
    """Require a selected engine's MCP config to use its explicit load path."""
    if not spec.mcp_configs:
        return
    resolved = {path.resolve() for path in spec.mcp_configs}
    if engine_name == "codex":
        expected = (Path.home() / _CODEX_MCP_CONFIG).resolve()
        if expected not in resolved:
            raise ValueError(f"Codex MCP config must use {expected}")
    elif engine_name == "claude":
        expected = (Path.home() / _CLAUDE_MCP_CONFIG).resolve()
        if expected not in resolved:
            raise ValueError(f"Claude MCP config must use {expected}")


def read_profile_raw(path: Path) -> dict[str, list[str]]:
    """Return the RAW declared string lists from a profile.toml's [profile] table.

    Unlike load_profile(), this performs NO glob expansion and NO existence check - it
    returns exactly the strings the operator wrote, which is what `profile init` needs to
    merge-not-clobber an existing file.  Absent file -> {} (not an error).  Raises
    ValueError on malformed TOML, a non-table [profile], or a non-string-list category.
    Empty categories are omitted from the returned dict.
    """
    if not path.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    text = _read_profile_text(path)
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise ValueError(f"malformed TOML in profile {path}: {exc}") from exc

    table = data.get("profile", {})
    if not isinstance(table, dict):
        raise ValueError(f"[profile] in {path} must be a TOML table, not {type(table).__name__}")

    raw: dict[str, list[str]] = {}
    for category in PROFILE_CATEGORIES:
        entries = table.get(category, [])
        if not isinstance(entries, list):
            raise ValueError(f"profile.{category} must be a list of strings in {path}")
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError(
                    f"profile.{category} entries must be strings, got {entry!r} in {path}"
                )
        if entries:
            raw[category] = list(entries)
    return raw


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string: backslash and quote only.

    Control characters are rejected by write_profile() before this is called.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def read_setups_raw(path: Path) -> dict[str, str]:
    """Return the RAW `[setups]` table (kind -> declared directory string), unexpanded.

    The `[setups]` counterpart to read_profile_raw: no sweep, no existence check, so
    `profile init` can merge-not-clobber what the operator already declared. Absent file or
    absent table -> {}. Raises ValueError on malformed TOML or a non-string value.
    """
    if not path.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    text = _read_profile_text(path)
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise ValueError(f"malformed TOML in profile {path}: {exc}") from exc

    table = data.get(SETUPS_TABLE, {})
    if not isinstance(table, dict):
        raise ValueError(f"[{SETUPS_TABLE}] in {path} must be a TOML table")
    out: dict[str, str] = {}
    for kind, value in table.items():
        if not isinstance(value, str):
            raise ValueError(f"{SETUPS_TABLE}.{kind} must be a string path in {path}")
        out[kind] = value
    return out


def write_profile(
    path: Path, table: dict[str, list[str]], setup_dirs: dict[str, str] | None = None
) -> None:
    """Atomically write `table` as a [profile] TOML table to `path`, plus an optional [setups].

    Mirrors userconfig.write_config_file but emits string ARRAYS (skills / instructions /
    knowledge) in the canonical PROFILE_CATEGORIES order so output is deterministic.
    Empty categories are omitted.  Rejects control chars in any entry (would corrupt the
    file).  `setup_dirs` becomes the `[setups]` table (kind = "dir"), written in
    setups.SETUP_KINDS order for the same determinism.  The file holds curated path lists,
    not credentials, so the final mode is 0644 (the parent ~/.franky is created 0700).
    """
    import os
    import tempfile

    for category, entries in table.items():
        for entry in entries:
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in entry):
                raise ValueError(
                    f"profile entry {entry!r} in {category!r} contains a control character - "
                    "refusing to write (would corrupt the file)"
                )
    for kind, value in (setup_dirs or {}).items():
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError(
                f"setup dir {value!r} for {kind!r} contains a control character - "
                "refusing to write (would corrupt the file)"
            )

    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lines = ["[profile]"]
    for category in PROFILE_CATEGORIES:
        entries = table.get(category)
        if not entries:
            continue
        lines.append(f"{category} = [")
        for entry in entries:
            lines.append(f'    "{_toml_escape(entry)}",')
        lines.append("]")
    if setup_dirs:
        lines += ["", f"[{SETUPS_TABLE}]"]
        for kind in setups.SETUP_KINDS:
            if kind in setup_dirs:
                lines.append(f'{kind} = "{_toml_escape(setup_dirs[kind])}"')
        for kind in sorted(k for k in setup_dirs if k not in setups.SETUP_KINDS):
            lines.append(f'{kind} = "{_toml_escape(setup_dirs[kind])}"')
    content = "\n".join(lines) + "\n"

    # Write atomically: temp file in the same dir -> os.replace, so the file is never
    # half-written for a concurrent reader.
    fd, tmp_path_str = tempfile.mkstemp(dir=parent, prefix=".franky-profile-")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    path.chmod(0o644)


def _read_profile_text(file_path: Path, budget: setups.ReadBudget | None = None) -> str:
    """Read a profile file as bounded, strict UTF-8 text.

    The single read path shared by build_bundle() (the real fail-closed gate) and
    scan_profile_files() (the `franky profile check` dry-run), so the dry-run can never
    read a file differently from the build and thus never diverge on what it scans.
    """
    raw = setups.read_bounded(file_path, budget or setups.profile_budget())
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"profile file {file_path} is not valid UTF-8") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def scan_for_secrets(text: str) -> list[str]:
    """Return a list of detection descriptions if `text` contains credential patterns.

    Returns an empty list when no patterns match.  Fail-closed callers treat any
    non-empty result as a refusal trigger.  The returned strings describe the pattern
    that matched WITHOUT including the matched secret value.
    """
    findings: list[str] = []
    for description, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(description)
    return findings


def _scan_mcp_text(text: str, credentials: list[str]) -> list[str]:
    """Scan MCP text while exempting only validated TOML `${NAME}` assignments."""
    sanitized = text
    for name in credentials:
        safe_assignment = re.compile(
            rf"(?m)^([ \t]*){re.escape(name)}([ \t]*=[ \t]*)(['\"])"
            rf"\$\{{{re.escape(name)}\}}\3([ \t]*(?:#.*)?)$"
        )
        sanitized = safe_assignment.sub(
            lambda match: (
                f"{match.group(1)}FRANKY_DECLARED_CREDENTIAL{match.group(2)}"
                f"{match.group(3)}${{FRANKY_DECLARED_CREDENTIAL}}{match.group(3)}"
                f"{match.group(4)}"
            ),
            sanitized,
        )
    return scan_for_secrets(sanitized)


@dataclass
class FileScanResult:
    """Per-file result of the `franky profile check` dry-run."""

    path: Path
    size: int  # byte length of the UTF-8-encoded text (0 when unreadable)
    findings: list[str]  # scan_for_secrets descriptions; empty when clean
    error: str | None  # read-error message, or None when the file was read


def scan_profile_files(spec: ProfileSpec) -> list[FileScanResult]:
    """Dry-run twin of build_bundle's per-file loop, for `franky profile check`.

    Reads each file in `spec` via the SAME _read_profile_text() and scans it with the
    SAME scan_for_secrets() that build_bundle uses, so a profile that scans clean here
    cannot fail the build's fail-closed secret gate (and vice-versa).  Unlike build_bundle
    it never raises: it accumulates per-file results (size, findings, read errors) so
    `check` can report every problem in one pass instead of aborting on the first.
    """
    results: list[FileScanResult] = []
    try:
        validate_mcp_configs(spec)
    except ValueError as exc:
        return [
            FileScanResult(path=path, size=0, findings=[], error=str(exc))
            for path in spec.mcp_configs
        ]
    budget = setups.profile_budget()
    for file_path in spec.all_files():
        try:
            text = _read_profile_text(file_path, budget)
        except (OSError, ValueError) as exc:
            results.append(FileScanResult(path=file_path, size=0, findings=[], error=str(exc)))
            continue
        findings = (
            _scan_mcp_text(text, spec.mcp_credentials)
            if file_path in spec.mcp_configs
            else scan_for_secrets(text)
        )
        results.append(
            FileScanResult(
                path=file_path,
                size=len(text.encode("utf-8")),
                findings=findings,
                error=None,
            )
        )
    return results


def build_bundle(spec: ProfileSpec) -> bytes:
    """Collect, secret-scan, and pack the profile into gzip-tar BYTES.

    Raw bytes, not base64: the bundle is streamed into the container over `docker exec -i`
    stdin (container.deliver_profile), so there is nothing to text-encode for an argv - and a
    swept setup is ~1 MB, far past what a single `-e` value can carry on Linux.

    The tar archive uses paths relative to HOME so the extraction (`tar -xzf - -C $HOME`)
    lands each file in the same relative location as on the operator's machine
    (e.g. ~/.claude/CLAUDE.md -> .claude/CLAUDE.md).

    Files whose path cannot be made relative to HOME fall back to a flat layout
    under a top-level `profile/` directory inside HOME.

    Raises ValueError (fail-closed) if:
    - Any file cannot be read.
    - Any file matches a secret pattern.
    - spec.all_files() is empty (nothing to inject; caller should skip the call).
    """
    files = spec.all_files()
    if not files:
        raise ValueError("profile bundle is empty - no files to inject")

    metadata_budget = setups.profile_budget()
    for file_path in files:
        try:
            setups.claim_file(file_path, metadata_budget)
        except OSError as exc:
            raise ValueError(f"could not inspect profile file {file_path}: {exc}") from exc

    validate_mcp_configs(spec)
    home = Path.home().resolve()
    declared_home = Path.home().absolute()
    buf = io.BytesIO()
    read_budget = setups.profile_budget()

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in files:
            try:
                text = _read_profile_text(file_path, read_budget)
            except (OSError, ValueError) as exc:
                raise ValueError(f"could not read profile file {file_path}: {exc}") from exc

            findings = (
                _scan_mcp_text(text, spec.mcp_credentials)
                if file_path in spec.mcp_configs
                else scan_for_secrets(text)
            )
            if findings:
                raise ValueError(
                    f"profile file {file_path} contains a potential credential "
                    f"({findings[0]}) - refusing to inject (fail-closed). "
                    "Remove the credential from the file before adding it to the profile."
                )

            try:
                archive_path = (
                    file_path.absolute() if file_path in spec.mcp_configs else file_path.resolve()
                )
                archive_home = declared_home if file_path in spec.mcp_configs else home
                arcname = str(archive_path.relative_to(archive_home))
            except ValueError:
                arcname = f"profile/{file_path.name}"

            encoded = text.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(encoded)
            tf.addfile(info, io.BytesIO(encoded))

    return buf.getvalue()
