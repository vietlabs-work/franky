"""Operator profile: inject curated skills/instructions/knowledge into the container.

WHY this is safe: the bundle is assembled on the host from an explicit allowlist
(no auto-discovery), secret-scanned before packing (fail-closed on any credential
hit), and transmitted as a base64-encoded gzip tar that the entrypoint unpacks into
the HOME tmpfs at container start.  No bind mount, no live config dir, no credential-
bearing file ever enters the bundle.

Injection mechanism: Option A (tmpfs-seed via entrypoint).  The base64 bundle is
passed as a by-value environment variable FRANKY_PROFILE_BUNDLE; the DinD entrypoint
decodes and extracts it into $HOME BEFORE exec-ing the engine.  This preserves the
no-bind-mount invariant and the secret-by-name discipline:
  - No new bind mount (no host-FS path is ever mounted into the container).
  - The bundle content is curated prose (Tier-1: static markdown / text only), already
    secret-scrubbed.  It is NOT a secret value itself, so passing it by value (inline
    -e KEY=VALUE) is correct - the same policy as proxy_url in container.py.
  - The hardening flags (_HARDENING) are unchanged.

Security argument:
  - A malicious issue body cannot reach the profile bundle; the operator assembles it
    host-side from an explicit allowlist before the task container ever starts.
  - Secret scanning runs fail-closed: a single detected credential pattern aborts the
    entire run before any container is started.  The operator must explicitly review and
    fix the file before it can be injected.
  - The bundle is NOT added to passthrough_env (which would make it a name-only -e flag
    that the docker process inherits).  It is added by value, like proxy config.

Tier-1 (this epic): static prose only - skills, CLAUDE.md-style instructions, knowledge
docs.  Tier-2 (MCP servers, future epic) requires separate handling of egress and creds.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

# The environment variable the DinD entrypoint reads to unpack the profile.
PROFILE_BUNDLE_VAR = "FRANKY_PROFILE_BUNDLE"

# Override the default profile path (~/.franky/profile.toml) for testing and CI.
PROFILE_PATH_VAR = "FRANKY_PROFILE_PATH"

# Default profile location relative to the user home directory.
_DEFAULT_PROFILE_RELATIVE = Path(".franky") / "profile.toml"

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
            r"(GH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_OAUTH_TOKEN|OPENROUTER_API_KEY"
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
    """

    skills: list[Path] = field(default_factory=list)
    instructions: list[Path] = field(default_factory=list)
    knowledge: list[Path] = field(default_factory=list)

    def all_files(self) -> list[Path]:
        """All files across all categories, in declaration order."""
        return self.skills + self.instructions + self.knowledge


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


def _expand_glob(raw: str) -> list[Path]:
    """Expand a single path or glob pattern (after ~ expansion) into existing Paths."""
    import glob as _glob

    expanded = Path(raw).expanduser()
    pattern = str(expanded)
    if any(c in pattern for c in ("*", "?", "[")):
        return sorted(Path(p) for p in _glob.glob(pattern))
    return [expanded] if expanded.exists() else []


def load_profile(path: Path) -> ProfileSpec:
    """Parse a profile.toml and return the expanded, concrete file list.

    Raises ValueError on:
    - TOML parse error
    - A literal path that does not exist (glob non-matches are silently empty)
    - A non-string or non-list entry in the TOML
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read profile {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"malformed TOML in profile {path}: {exc}") from exc

    table = data.get("profile", {})
    if not isinstance(table, dict):
        raise ValueError(f"[profile] in {path} must be a TOML table, not {type(table).__name__}")

    spec = ProfileSpec()
    for category in ("skills", "instructions", "knowledge"):
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
            paths = _expand_glob(entry)
            if not paths and not is_glob:
                raise ValueError(f"profile file not found: {entry!r} (listed in {path})")
            resolved.extend(paths)
        setattr(spec, category, resolved)

    return spec


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


def build_bundle(spec: ProfileSpec) -> str:
    """Collect, secret-scan, and pack the profile into a base64-encoded gzip tar.

    The tar archive uses paths relative to HOME so the entrypoint can extract them
    with `tar -xz -C $HOME` and they land in the same relative location as they are
    on the operator's machine (e.g. ~/.claude/CLAUDE.md -> .claude/CLAUDE.md).

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

    home = Path.home().resolve()
    buf = io.BytesIO()

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ValueError(f"could not read profile file {file_path}: {exc}") from exc

            findings = scan_for_secrets(text)
            if findings:
                raise ValueError(
                    f"profile file {file_path} contains a potential credential "
                    f"({findings[0]}) - refusing to inject (fail-closed). "
                    "Remove the credential from the file before adding it to the profile."
                )

            try:
                arcname = str(file_path.resolve().relative_to(home))
            except ValueError:
                arcname = f"profile/{file_path.name}"

            encoded = text.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(encoded)
            tf.addfile(info, io.BytesIO(encoded))

    return base64.b64encode(buf.getvalue()).decode("ascii")
