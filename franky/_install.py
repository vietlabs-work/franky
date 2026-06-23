"""Install-provenance detection for Franky.

Shared utility used by `franky version` (and future updater commands) to determine
how Franky was installed on the current machine. Stdlib only.

Precedence for classify_executable:
1. Check for '/uv/tools/' FIRST - uv is more specific; a uv-managed tool venv may also
   contain other tokens (e.g. a path under .local could contain both uv/tools and pipx).
2. Check for '/pipx/' next.
3. Fall back to 'pip' for anything else (plain venv, system Python, etc.).
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# The PyPI distribution name. The import package + the installed command are both `franky`,
# but the published distribution is `franky-agent` (`franky` was taken). This is the single
# source of truth for the name: `franky.__init__.franky_version` reads its metadata,
# `detect_install` looks it up, and `update_check` reinstalls by it.
DIST_NAME = "franky-agent"


@dataclass
class Install:
    kind: str  # "dev checkout" | "uv tool" | "pipx" | "pip"
    path: str  # source dir for dev checkout, else the interpreter path


def classify_executable(executable: str) -> str:
    """Classify the install manager from the interpreter path.

    Normalizes backslashes to '/' and lowercases before matching so the check is
    platform-independent. Precedence: '/uv/tools/' > '/pipx/' > 'pip'.

    Args:
        executable: Path to the Python interpreter (e.g. sys.executable).

    Returns:
        One of: "uv tool", "pipx", "pip".
    """
    norm = executable.replace("\\", "/").lower()
    if "/uv/tools/" in norm:
        return "uv tool"
    if "/pipx/" in norm:
        return "pipx"
    return "pip"


def _is_editable(dist) -> bool:
    """Return True iff the distribution was installed in editable mode.

    Reads the direct_url.json file from the distribution metadata and checks for
    ``dir_info.editable == true``. Never raises - any exception (missing file, bad
    JSON, None dist) returns False.
    """
    try:
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return False
        data = json.loads(raw)
        return bool(data.get("dir_info", {}).get("editable"))
    except Exception:
        return False


def detect_install(
    package: str = DIST_NAME,
    executable: str | None = None,
    dist_lookup=importlib.metadata.distribution,
) -> Install:
    """Detect how `package` was installed on this machine.

    Args:
        package: The PyPI distribution name to look up (default: "franky-agent"; the
            import package is "franky" but the published distribution is "franky-agent").
        executable: The Python interpreter path to classify (default: sys.executable).
        dist_lookup: Callable ``(package_name: str) -> Distribution``-like object with
            a ``read_text(filename: str) -> str | None`` method. Defaults to
            ``importlib.metadata.distribution``. Inject a fake in tests.

    Returns:
        Install with kind and path.

    Fallback order / decision table:
    - PackageNotFoundError from dist_lookup -> "dev checkout" (source dir).
    - dist found AND editable -> "dev checkout" (source dir).
    - dist found AND not editable -> classify_executable(executable).

    The "dev checkout" source path is ``Path(__file__).resolve().parent.parent`` -
    the repo root that contains the ``franky/`` package directory. This is the
    canonical way to point at the checkout without relying on any metadata that may
    be absent or stale.
    """
    if executable is None:
        executable = sys.executable

    # The directory containing the franky package source (i.e. the repo root).
    source_path = str(Path(__file__).resolve().parent.parent)

    try:
        dist = dist_lookup(package)
    except importlib.metadata.PackageNotFoundError:
        return Install("dev checkout", source_path)

    if _is_editable(dist):
        return Install("dev checkout", source_path)

    return Install(classify_executable(executable), executable)
