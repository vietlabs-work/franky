"""Self-update for Franky: fetch the latest release, reinstall via the detected manager.

`franky update` (force_update) is the deliberate, interactive command; `maybe_auto_update`
is its passive, best-effort sibling run at the top of `franky build`. Both share THIS module
(fetch + version-compare + installer detection in `_install.py`), so the logic lives once.

Stdlib only (subprocess + urllib + json + re + time + pathlib) plus `franky._install`.

`force_update` shape:
- Always a FRESH version fetch (no cache) - the operator is waiting, so a generous ~10s
  budget is fine.
- Fetch source: the public PyPI JSON API (`https://pypi.org/pypi/franky-agent/json`,
  `info.version`). No auth, no `gh`, no `GH_TOKEN` - the package is public even though the
  source repo is private, so a single unauthenticated HTTPS GET is all it takes.
- Installer detected from the resolved interpreter path (via `_install.detect_install`):
  `uv tool` -> `uv tool install --force`, `pipx` -> `pipx install --force`, else the
  running env's `python -m pip install --upgrade`. Undetectable -> manual hint + exit 1
  (never a blind install into the wrong env).
- Dev checkout -> "update via git", exit 0 (no-op: the checkout IS the source of truth).
- `0` = current/upgraded/reinstalled, `1` = could not update. Failure surfaces a nonzero
  exit plus the install stderr tail.
- Does NOT re-exec: the only job is to install; the next `franky` invocation runs new code.

`maybe_auto_update` shape:
- HINT ONLY by default - Franky has NO version-pinned host<->container wire contract, so a
  stale CLI talking to a newer image is not a correctness hazard.
  Never blocks, never re-execs. Prints a one-line hint to stderr and returns.
- Never raises into the build (telemetry, not a gate - the whole body is wrapped).
- Tight ~1s fetch budget; results cached in `~/.franky/update_check.json` with tiered TTLs
  ("available" ~24h, "current" ~1h, failures negative-cached ~1h) so a same-day release is
  not invisible yet repeated builds cost nothing.
- `FRANKY_NO_UPDATE_CHECK=1` silences it; dev checkout is silent (updates via git).
- `FRANKY_AUTO_UPDATE=1` is opt-in and installs for the NEXT run only - still no re-exec.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from . import franky_version
from ._install import DIST_NAME, Install, detect_install
from .container import PULL_TIMEOUT_SECS

# Self-update reinstalls by the published distribution name (DIST_NAME, defined once in
# _install.py); the latest version is read from PyPI's JSON API for the same package.
_PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"

# A generous budget for the PyPI fetch: the operator is waiting on a single HTTPS GET.
_FETCH_TIMEOUT = 10.0

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """Raised when the latest version cannot be fetched from PyPI (network/parse failure)."""


def parse_version(s: str) -> tuple[int, int, int] | None:
    """Parse a plain `X.Y.Z` (optionally `vX.Y.Z`) tag into a comparable tuple, else None."""
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest_tag: str, current: str) -> bool:
    """True iff `latest_tag` should trigger an install relative to `current`.

    Both parse as X.Y.Z -> numeric tuple ordering. Otherwise (an unparseable value on either
    side) fall back to an exact-string compare: differ -> treat as newer. We never silently
    swallow an update just because a version string is non-semver.
    """
    lv = parse_version(latest_tag)
    cv = parse_version(current)
    if lv is not None and cv is not None:
        return lv > cv
    return latest_tag.strip().lstrip("v") != str(current).strip().lstrip("v")


def _spec(version: str) -> str:
    """The pip/uv/pipx install spec pinning the given release version, e.g.
    `franky-agent==1.2.3`. A leading `v` is stripped (PyPI versions are plain `X.Y.Z`; this
    keeps the spec valid even if a `vX.Y.Z`-shaped string is passed in)."""
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    return f"{DIST_NAME}=={v}"


def _vstr(version: str) -> str:
    """A `v`-prefixed display string for a version. PyPI versions are bare `X.Y.Z`, but the
    CLI shows versions v-prefixed everywhere, so user-facing messages route through this.
    Idempotent for an already-`v`-prefixed value."""
    v = version.strip()
    return v if v.startswith("v") else f"v{v}"


def fetch_latest_version(
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> str:
    """Fetch the latest published version from PyPI, fresh (no cache).

    A single unauthenticated HTTPS GET of the package's JSON metadata; `info.version` is the
    latest non-yanked release. Raises UpdateError with an operator-readable message on any
    network/parse failure or a missing version field.
    """
    req = urllib.request.Request(_PYPI_JSON_URL, headers={"User-Agent": "franky-update"})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as exc:
        raise UpdateError(f"could not reach PyPI to check for the latest release ({exc})") from exc
    version = (data.get("info") or {}).get("version")
    if not version:
        raise UpdateError("PyPI response had no info.version")
    return version


def _install_command(install: Install, version: str) -> list[str] | None:
    """Build the reinstall argv for the detected manager, or None if undetectable.

    pip targets `install.path` (the resolved interpreter) so the upgrade lands in the exact
    running env - never a blind install into the wrong one.

    The version comes from the PyPI JSON API, which can be newer than the installer's cached
    PyPI simple-index page (PyPI allows 10 minutes), so each command bypasses that cache or the
    pinned version reads as missing right after a release.
    """
    spec = _spec(version)
    if install.kind == "uv tool":
        return ["uv", "tool", "install", "--force", "--refresh-package", DIST_NAME, spec]
    if install.kind == "pipx":
        return ["pipx", "install", "--force", "--pip-args=--no-cache-dir", spec]
    if install.kind == "pip":
        py = install.path or sys.executable
        return [py, "-m", "pip", "install", "--upgrade", "--no-cache-dir", spec]
    return None


def _tail(text: str, lines: int = 15) -> str:
    """Last `lines` non-empty-trimmed lines of `text` (the install stderr tail)."""
    return "\n".join(text.strip().splitlines()[-lines:])


def force_update(
    *,
    force: bool = False,
    install: Install | None = None,
    current: str | None = None,
    fetch: Callable[[], str] = fetch_latest_version,
    runner: Callable = subprocess.run,
    out: Callable[[str], None] = print,
) -> int:
    """Install the latest published release. Returns 0 (current/upgraded/reinstalled) or 1.

    Args:
        force: Reinstall even when already on the latest release.
        install: Injectable install provenance (default: detect_install()).
        current: Injectable running version (default: franky_version()).
        fetch: Injectable latest-version fetcher (default: fetch_latest_version).
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
        out(f"franky: already on the latest release ({_vstr(current)})")
        return 0

    argv = _install_command(install, tag)
    if argv is None:
        out(
            "franky: could not detect how franky was installed - update manually:\n"
            f"  uv tool install --force --refresh-package {DIST_NAME} {_spec(tag)}\n"
            f"  # or: pipx install --force --pip-args=--no-cache-dir {_spec(tag)}"
        )
        return 1

    out(f"franky: {'reinstalling' if (not newer and force) else f'updating to {_vstr(tag)}'} ...")
    try:
        proc = runner(argv, capture_output=True, text=True)
    except OSError as exc:
        out(f"franky: install command failed to start ({exc})")
        return 1
    if proc.returncode != 0:
        tail = _tail(proc.stderr or proc.stdout or "")
        out(f"franky: update failed (exit {proc.returncode})" + (f":\n{tail}" if tail else ""))
        return 1

    out(f"franky: {'reinstalled' if (not newer and force) else 'updated to'} {_vstr(tag)}")
    _prepull_images(runner, out)
    return 0


def _prepull_images(runner: Callable, out: Callable[[str], None]) -> None:
    """Best-effort: pull the NEW version's images so the first run needs no 1.2 GB pull.

    The install replaced the package in this interpreter's environment, so a child process
    resolves the new version's tags. It inherits stderr for progress and caps each pull itself;
    the outer timeout only guards a hung child. Never changes the update's exit code.
    """
    # -I (isolated): never import a `franky` from the current directory, so an update
    # run inside a checkout still runs the newly installed code with the user's creds.
    argv = [sys.executable, "-I", "-m", "franky.cli", "pull-images"]
    try:
        proc = runner(argv, timeout=2 * PULL_TIMEOUT_SECS + 60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        proc = None
        reason = f"({exc})"
    else:
        reason = f"(exit {proc.returncode})"
    if proc is None or proc.returncode != 0:
        out(
            f"franky: image pre-pull did not finish {reason}. Update is installed. "
            "The next run pulls the images, or pull them now with the `docker pull` command above."
        )


# ---------------------------------------------------------------------------
# Best-effort auto-update hint (run at the top of `franky build`)
# ---------------------------------------------------------------------------

# A tight budget: the build is waiting, so a check that can't answer fast is negative-cached
# and skipped. PyPI is a fast CDN; anything slower than this is effectively down, so skip it.
_BUILD_FETCH_TIMEOUT = 1.0

# Tiered cache TTLs (seconds). "available" lingers (you already know); "current" is short so a
# same-day release becomes visible within the hour; failures are negative-cached like "current".
_TTL_AVAILABLE = 24 * 60 * 60
_TTL_CURRENT = 60 * 60
_TTL_ERROR = 60 * 60
_TTLS = {"available": _TTL_AVAILABLE, "current": _TTL_CURRENT, "error": _TTL_ERROR}

# Host-CLI-only env flags. NOTE: these are deliberately NOT in config.py's container passthrough,
# so the bridge never forwards them into the task container - they steer the host CLI alone.
NO_CHECK_VAR = "FRANKY_NO_UPDATE_CHECK"
AUTO_UPDATE_VAR = "FRANKY_AUTO_UPDATE"


def default_cache_path() -> Path:
    """The auto-update cache file: ``~/.franky/update_check.json``."""
    return Path.home() / ".franky" / "update_check.json"


def _eprint(msg: str) -> None:
    """Print to stderr (the hint must never pollute stdout, which carries the PR URL)."""
    print(msg, file=sys.stderr)


def _flag(env: Mapping[str, str], name: str) -> bool:
    """True iff the env flag is set to exactly "1" (matches the documented opt-in/opt-out)."""
    return env.get(name, "").strip() == "1"


def _read_cache(path: Path) -> dict | None:
    """Load the cache entry, or None if absent/unreadable/corrupt (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path: Path, entry: dict) -> None:
    """Persist the cache entry; a write failure must never break the build (swallowed)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding="utf-8")
    except Exception:
        pass


def _cache_fresh(entry: dict | None, now_ts: float) -> bool:
    """True iff `entry` is still within its status's TTL.

    A future-dated `checked_at` (clock skew) is treated as stale -> we refetch rather than
    trust a timestamp we can't explain. An unknown status has no TTL -> stale.
    """
    if not entry:
        return False
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return False
    ttl = _TTLS.get(entry.get("status"))
    if ttl is None:
        return False
    age = now_ts - checked_at
    return 0 <= age < ttl


def _install_for_next_run(
    install: Install, tag: str, runner: Callable, out: Callable[[str], None]
) -> None:
    """Opt-in (`FRANKY_AUTO_UPDATE=1`): install `tag` for the NEXT run. Never re-execs.

    Best-effort: an undetectable installer or a failed install just leaves the hint standing.
    """
    argv = _install_command(install, tag)
    if argv is None:
        return
    out(f"franky: {AUTO_UPDATE_VAR}=1 - installing {_vstr(tag)} for your next build ...")
    try:
        proc = runner(argv, capture_output=True, text=True)
    except OSError:
        return
    if proc.returncode == 0:
        out(f"franky: installed {_vstr(tag)}; it takes effect on your next `franky build`")
    else:
        out(
            f"franky: auto-install of {_vstr(tag)} failed (exit {proc.returncode}) "
            "- run `franky update`"
        )


def _auto_update(
    env: Mapping[str, str],
    install: Install,
    current: str,
    fetch: Callable[[], str],
    now: Callable[[], float],
    cache_path: Path,
    out: Callable[[str], None],
    runner: Callable,
) -> None:
    """The auto-update body. Raises freely; `maybe_auto_update` is the bare-except wrapper."""
    if _flag(env, NO_CHECK_VAR):
        return
    # Dev checkout: silent - the checkout is the source of truth, updated via git.
    if install.kind == "dev checkout":
        return

    entry = _read_cache(cache_path)
    if _cache_fresh(entry, now()):
        tag = entry.get("latest_tag")
        status = entry.get("status")
        # A cached "available" can outlive the upgrade that resolved it; re-check vs current.
        if status == "available" and tag and not is_newer(tag, current):
            status = "current"
    else:
        try:
            tag = fetch()
            status = "available" if is_newer(tag, current) else "current"
        except Exception:
            tag, status = None, "error"
        _write_cache(cache_path, {"checked_at": now(), "latest_tag": tag, "status": status})

    if status != "available" or not tag:
        return

    out(
        f"franky: {_vstr(tag)} available (installed {_vstr(current)}) "
        "- run `franky update` to upgrade"
    )
    if _flag(env, AUTO_UPDATE_VAR):
        _install_for_next_run(install, tag, runner, out)


def maybe_auto_update(
    *,
    env: Mapping[str, str] | None = None,
    install: Install | None = None,
    current: str | None = None,
    fetch: Callable[[], str] | None = None,
    now: Callable[[], float] = time.time,
    cache_path: Path | None = None,
    out: Callable[[str], None] = _eprint,
    runner: Callable = subprocess.run,
) -> None:
    """Best-effort update hint for `franky build`. Hint-only, never blocks, never re-execs.

    Telemetry, not a gate: ANY failure (network, cache, detection) is swallowed so the build
    is never affected. By default prints a one-line stderr hint when a newer release exists.

    Args (all injectable for tests):
        env: Environment mapping (default: os.environ).
        install: Install provenance (default: detect_install()).
        current: Running version (default: franky_version()).
        fetch: Zero-arg latest-version fetcher (default: fetch_latest_version, ~1s budget).
        now: Clock (default: time.time).
        cache_path: Cache file (default: ~/.franky/update_check.json).
        out: Line printer (default: stderr).
        runner: subprocess.run-compatible callable (only used for FRANKY_AUTO_UPDATE installs).
    """
    try:
        if env is None:
            env = os.environ
        if install is None:
            install = detect_install()
        if current is None:
            current = franky_version()
        if fetch is None:
            fetch = lambda: fetch_latest_version(timeout=_BUILD_FETCH_TIMEOUT)  # noqa: E731
        if cache_path is None:
            cache_path = default_cache_path()
        _auto_update(env, install, current, fetch, now, cache_path, out, runner)
    except Exception:
        return
