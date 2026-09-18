"""User-level config file: ~/.franky/config (TOML format).

WHY a separate module instead of bolting this onto config.py:
- config.py is the fail-closed runtime config (env vars -> Config dataclass).
  It must stay pure and fast (no I/O, no file discovery).
- userconfig.py owns the on-disk persistence layer: read/write/set helpers and
  the `load_config_file` call that injects file values into the environment BEFORE
  load_config runs. Keeping them separate means `franky config` subcommands can
  import userconfig without pulling in the full runtime stack, and `config.py`
  stays importable in unit tests with no file-system side effects.

WHY TOML basic strings for ALL values (no arrays):
  FRANKY_ALLOWED_REPOS is comma-joined downstream (config.py already splits on ","),
  so representing it as a plain string keeps the writer single-type and avoids any
  schema migration if we ever add a new list-valued key.

WHY env-var names as keys (not camel-case aliases):
  The file is the authoritative alternative to shell exports.  Using the EXACT env-var
  names means the mapping from file -> env is trivial (one setdefault per key) and the
  user can cross-reference `franky --help` with the file without a mental namespace
  translation.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

# Lazy tomllib import: tomllib was added to stdlib in Python 3.11.
# For 3.10 we require the `tomli` backport (added to pyproject.toml dependencies).
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from .config import DISK_MB_VAR, GH_TOKEN_VAR, MEMORY_MB_VAR, MODEL_VAR
from .engine import (
    CLAUDE_TOKEN_VAR,
    CODEX_PROVIDER_VARS,
    CODEX_SUBSCRIPTION_VAR,
    FRANKY_CODEX_AUTH_VOLUME_VAR,
    OPENCODE_PROVIDERS,
    PI_PROVIDER_VARS,
)
from .jira import JIRA_API_TOKEN_VAR

# The table name used in the TOML file.
_TABLE = "franky"

# OLLAMA_HOST lives in PI_PROVIDER_VARS but is a host URL (e.g. http://box:11434),
# not a credential, so it is excluded from the secret set below: masking a URL as
# ***REDACTED*** in `config list` is confusing and it is harmless on argv. It stays
# settable via _NON_SECRET_SETTABLE_KEYS.
_OLLAMA_HOST = "OLLAMA_HOST"

# Keys that ARE secrets (mask in `list`, refuse as positional CLI args).
# This is the single source of truth for "is this key a secret".  It is derived
# entirely from imported constants (minus the OLLAMA_HOST URL) so it can never drift
# from the real var names. JIRA_EMAIL is NOT here - it is shown unmasked in `list`
# and allowed on argv.
SECRET_KEYS: frozenset[str] = frozenset(
    (
        {GH_TOKEN_VAR}
        | set(PI_PROVIDER_VARS)
        | set(CODEX_PROVIDER_VARS)
        | {credential for credential, _host in OPENCODE_PROVIDERS.values()}
        | {CLAUDE_TOKEN_VAR}
        | {JIRA_API_TOKEN_VAR}
    )
    - {_OLLAMA_HOST}
)

# Full set of keys that `franky config set` accepts. Partitioned as:
#   SECRET_KEYS  - stored but masked on display, refused as positional argv
#   _NON_SECRET_SETTABLE_KEYS  - stored and shown in plain text
#
# Unknown keys are refused with a helpful error (not silently stored) to catch
# typos and prevent config-file pollution from mistyped var names.
_NON_SECRET_SETTABLE_KEYS: frozenset[str] = frozenset(
    {
        "FRANKY_ENGINE",
        MODEL_VAR,
        MEMORY_MB_VAR,
        DISK_MB_VAR,
        "FRANKY_ALLOWED_REPOS",
        "FRANKY_EXTRA_ALLOWED_DOMAINS",
        _OLLAMA_HOST,
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "FRANKY_IMAGE",
        "FRANKY_PROXY_IMAGE",
        "FRANKY_GHCR_REPO",
        "FRANKY_NO_UPDATE_CHECK",
        "FRANKY_AUTO_UPDATE",
        "FRANKY_PROFILE_PATH",
        CODEX_SUBSCRIPTION_VAR,
        FRANKY_CODEX_AUTH_VOLUME_VAR,
    }
)

SETTABLE_KEYS: frozenset[str] = SECRET_KEYS | _NON_SECRET_SETTABLE_KEYS


def config_file_path(env: dict[str, str] | None = None) -> Path:
    """Return the resolved config file path.

    Override with FRANKY_CONFIG_FILE for hermetic tests - the override is an
    absolute path string that is used as-is (no ~/.franky/ prefix).  Absent
    override -> ~/.franky/config.

    WHY inject env here rather than reading os.environ directly: makes the
    path deterministic in tests without monkeypatching module globals.
    """
    if env is None:
        env = dict(os.environ)
    override = env.get("FRANKY_CONFIG_FILE")
    if override:
        return Path(override)
    return Path.home() / ".franky" / "config"


def read_config_file(path: Path) -> dict[str, str]:
    """Read the TOML config at `path` and return the [franky] table as a str->str dict.

    - Absent file -> empty dict (not an error: the file is optional).
    - Malformed TOML -> raises ValueError with a descriptive message.
    - Missing [franky] table -> empty dict.
    - All values are coerced to str (they should already be strings per our writer,
      but we guard defensively so a hand-edited file with an integer value does not
      crash the CLI).
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read config file {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"malformed TOML in {path}: {exc}") from exc
    table = data.get(_TABLE, {})
    if not isinstance(table, dict):
        raise ValueError(f"[franky] in {path} must be a TOML table, got {type(table).__name__}")
    return {str(k): str(v) for k, v in table.items()}


def _toml_escape(value: str) -> str:
    """Escape a string for use inside a TOML basic string (double-quoted).

    TOML basic strings allow backslash escapes.  We only need to escape:
      \\ -> \\\\   (backslash)
      "  -> \\"    (double quote, which would close the string)

    Control characters (including \\n, \\r, and other ASCII 0-31 / 127) are
    rejected BEFORE this function is called by write_config_file.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_config_file(path: Path, data: dict[str, str]) -> None:
    """Atomically write `data` as a TOML [franky] table to `path` at mode 0600.

    - Creates parent directory ~/.franky at mode 0700 if absent.
    - Writes to a sibling temp file first, then os.replace -> atomic.
    - Rejects any value containing control characters (\\n, \\r, etc.) to prevent
      config-file corruption.
    - Final file is 0600 (owner read/write only - secrets live here).

    WHY hand-rolled TOML writer instead of tomli-w / tomlkit:
      Our schema is intentionally constrained: one [franky] table, all string values.
      Adding a write dependency for ~10 lines of output would be disproportionate and
      would require another pyproject.toml entry.  Escaping rules for TOML basic strings
      are trivial (see _toml_escape) and the constraint (strings only) is enforced here.
    """
    for key, value in data.items():
        # Guard against control chars (includes newline, carriage-return, tab, null, etc.)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError(
                f"config value for {key!r} contains a control character - "
                "refusing to write (would corrupt the file)"
            )

    # Create ~/.franky at 0700 so the containing dir is not world-readable.
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lines = [f"[{_TABLE}]"]
    for key, value in sorted(data.items()):
        lines.append(f'{key} = "{_toml_escape(value)}"')
    content = "\n".join(lines) + "\n"

    # Write atomically: temp file in same dir -> os.replace so the file is never
    # half-written (safe for concurrent readers that open -> read -> close).
    fd, tmp_path_str = tempfile.mkstemp(dir=parent, prefix=".franky-cfg-")
    tmp_path = Path(tmp_path_str)
    try:
        os.chmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before writing
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file if replace failed; ignore secondary errors.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # Ensure mode is 0600 on the final file (os.replace preserves the mode we set on
    # the temp file, but be explicit in case the umask was overly permissive).
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def set_value(path: Path, key: str, value: str) -> None:
    """Read the file, set key=value, and write back.  Preserves all other keys."""
    data = read_config_file(path)
    data[key] = value
    write_config_file(path, data)


def unset_value(path: Path, key: str) -> None:
    """Remove one key while preserving the rest. Missing keys are a no-op."""
    data = read_config_file(path)
    if key in data:
        del data[key]
        write_config_file(path, data)


def load_config_file(env: dict[str, str] | None = None, path: Path | None = None) -> None:
    """Read the user config file and inject each key into `env` via setdefault.

    WHY setdefault (not override): the process environment ALWAYS wins.  A user who
    exports GH_TOKEN=... in their shell should not be surprised that the config file
    overrides it.  This is the same precedence as most CLI tools (env > config file).

    - `env` defaults to os.environ (mutated in-place; the caller passes os.environ).
    - `path` defaults to config_file_path(env) if None.
    - Absent file -> silent no-op.
    - Malformed file -> raises ValueError (the `build` command converts this to a
      clean ClickException so the operator sees a helpful message, not a traceback).
    - Never called from the `config` subcommands (see cli.py WHY comment): they must stay
      usable when the file itself is malformed.
    - `version` DOES call it, on a copy of the environment and with the error swallowed, so
      the engine and image it reports match what a real run resolves.
    """
    if env is None:
        env = os.environ  # type: ignore[assignment]
    if path is None:
        path = config_file_path(env)
    file_values = read_config_file(path)  # raises ValueError on malformed TOML
    for key, value in file_values.items():
        env.setdefault(key, value)


def mask_value(key: str, value: str) -> str:
    """Return the masked representation of `value` if `key` is in SECRET_KEYS.

    Uses the canonical REDACT_TOKEN from config.py so the masking token is
    consistent everywhere (logs, `config list`, error messages).
    """
    from .config import REDACT_TOKEN

    if key in SECRET_KEYS and value:
        return REDACT_TOKEN
    return value
