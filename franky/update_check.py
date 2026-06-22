"""Self-update for Franky: fetch the latest release, reinstall via the detected manager.

`franky update` is the deliberate, interactive sibling of the future best-effort
auto-update (#11); both share THIS module (and the installer detection in `_install.py`),
so the fetch + version-compare + reinstall logic lives in one place.

Stdlib only (subprocess + urllib + json + re) plus `franky._install`. No third-party deps.

Shape (replicated from the reference tool's `force_update`, not imported):
- Always a FRESH release fetch (no cache) - the operator is waiting, so a generous ~10s
  budget is fine.
- Fetch precedence: `gh api .../releases/latest` first (reuses the operator's `gh` auth,
  and the repo is private), then the REST API with a `GH_TOKEN` fallback, then unauth
  (dormant until the repo is public).
- Installer detected from the resolved interpreter path (via `_install.detect_install`):
  `uv tool` -> `uv tool install --force`, `pipx` -> `pipx install --force`, else the
  running env's `python -m pip install --upgrade`. Undetectable -> manual hint + exit 1
  (never a blind install into the wrong env).
- Dev checkout -> "update via git", exit 0 (no-op: the checkout IS the source of truth).
- `0` = current/upgraded/reinstalled, `1` = could not update. Failure surfaces a nonzero
  exit plus the install stderr tail.
- Does NOT re-exec: the only job is to install; the next `franky` invocation runs new code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping

from . import franky_version
from ._install import Install, detect_install

# The install source while the repo is private (mirrors README's install incantation);
# `@<tag>` is appended to pin the exact release.
REPO_GIT_URL = "git+ssh://git@github.com/vietlabs-work/franky"
_RELEASES_LATEST_PATH = "repos/vietlabs-work/franky/releases/latest"
_RELEASES_LATEST_URL = f"https://api.github.com/{_RELEASES_LATEST_PATH}"

# A generous budget for the gh/REST fetch: the operator is waiting on a single fetch.
_FETCH_TIMEOUT = 10.0

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """Raised when the latest release tag cannot be fetched (network/auth/parse)."""


def parse_version(s: str) -> tuple[int, int, int] | None:
    """Parse a plain `X.Y.Z` (optionally `vX.Y.Z`) tag into a comparable tuple, else None."""
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest_tag: str, current: str) -> bool:
    """True iff `latest_tag` should trigger an install relative to `current`.

    Both parse as X.Y.Z -> numeric tuple ordering. Otherwise (an unparseable tag on either
    side) fall back to an exact-string compare: differ -> treat as newer. This mirrors
    the reference tool's caveat - we never silently swallow an update just because a tag is non-semver.
    """
    lv = parse_version(latest_tag)
    cv = parse_version(current)
    if lv is not None and cv is not None:
        return lv > cv
    return latest_tag.strip().lstrip("v") != str(current).strip().lstrip("v")


def _spec(tag: str) -> str:
    """The pip/uv/pipx install spec pinning the given release tag."""
    return f"{REPO_GIT_URL}@{tag}"


def _try_gh(runner: Callable, timeout: float) -> str | None:
    """Fetch the latest tag via `gh api` (reuses the operator's gh auth). None on any failure."""
    try:
        proc = runner(
            ["gh", "api", _RELEASES_LATEST_PATH, "--jq", ".tag_name"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    tag = (proc.stdout or "").strip()
    return tag or None


def _try_rest(opener: Callable, env: Mapping[str, str], timeout: float) -> str:
    """Fetch the latest tag via the REST API. Adds a Bearer header if GH_TOKEN is set
    (private repo); without it the call is unauth and dormant until the repo is public.
    Raises on any failure - the caller wraps it in UpdateError."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "franky-update",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_RELEASES_LATEST_URL, headers=headers)
    with opener(req, timeout=timeout) as resp:
        data = json.load(resp)
    tag = data.get("tag_name")
    if not tag:
        raise UpdateError("release response had no tag_name")
    return tag


def fetch_latest_tag(
    *,
    runner: Callable = subprocess.run,
    opener: Callable = urllib.request.urlopen,
    env: Mapping[str, str] | None = None,
    timeout: float = _FETCH_TIMEOUT,
) -> str:
    """Fetch the latest release tag, fresh (no cache). Tries `gh` first, then REST.

    Raises UpdateError with an operator-readable message if neither path yields a tag.
    """
    if env is None:
        import os

        env = os.environ

    tag = _try_gh(runner, timeout)
    if tag:
        return tag
    try:
        return _try_rest(opener, env, timeout)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(
            f"could not reach GitHub to check for the latest release ({exc})"
        ) from exc


def _install_command(install: Install, tag: str) -> list[str] | None:
    """Build the reinstall argv for the detected manager, or None if undetectable.

    pip targets `install.path` (the resolved interpreter) so the upgrade lands in the exact
    running env - never a blind install into the wrong one.
    """
    spec = _spec(tag)
    if install.kind == "uv tool":
        return ["uv", "tool", "install", "--force", spec]
    if install.kind == "pipx":
        return ["pipx", "install", "--force", spec]
    if install.kind == "pip":
        return [install.path or sys.executable, "-m", "pip", "install", "--upgrade", spec]
    return None


def _tail(text: str, lines: int = 15) -> str:
    """Last `lines` non-empty-trimmed lines of `text` (the install stderr tail)."""
    return "\n".join(text.strip().splitlines()[-lines:])


def force_update(
    *,
    force: bool = False,
    install: Install | None = None,
    current: str | None = None,
    fetch: Callable[[], str] = fetch_latest_tag,
    runner: Callable = subprocess.run,
    out: Callable[[str], None] = print,
) -> int:
    """Install the latest published release. Returns 0 (current/upgraded/reinstalled) or 1.

    Args:
        force: Reinstall even when already on the latest release.
        install: Injectable install provenance (default: detect_install()).
        current: Injectable running version (default: franky_version()).
        fetch: Injectable latest-tag fetcher (default: fetch_latest_tag).
        runner: Injectable subprocess.run-compatible callable for the install command.
        out: Injectable line printer (default: print).
    """
    if install is None:
        install = detect_install()
    if current is None:
        current = franky_version()

    # Dev checkout: there is nothing to install TO - the checkout is the source of truth.
    if install.kind == "dev checkout":
        out("franky: dev checkout - update via git (e.g. `git pull`), not `franky update`")
        return 0

    try:
        tag = fetch()
    except UpdateError as exc:
        out(f"franky: {exc}")
        return 1

    newer = is_newer(tag, current)
    if not newer and not force:
        out(f"franky: already on the latest release ({current})")
        return 0

    argv = _install_command(install, tag)
    if argv is None:
        out(
            "franky: could not detect how franky was installed - update manually:\n"
            f"  uv tool install --force {_spec(tag)}\n"
            f"  # or: pipx install --force {_spec(tag)}"
        )
        return 1

    out(f"franky: {'reinstalling' if (not newer and force) else f'updating to {tag}'} ...")
    try:
        proc = runner(argv, capture_output=True, text=True)
    except OSError as exc:
        out(f"franky: install command failed to start ({exc})")
        return 1
    if proc.returncode != 0:
        tail = _tail(proc.stderr or proc.stdout or "")
        out(f"franky: update failed (exit {proc.returncode})" + (f":\n{tail}" if tail else ""))
        return 1

    out(f"franky: {'reinstalled' if (not newer and force) else 'updated to'} {tag}")
    return 0
