"""Self-update for Franky: fetch the latest release, reinstall via the detected manager.

`franky update` (force_update) is the deliberate, interactive command; `maybe_auto_update`
is its passive, best-effort sibling run at the top of `franky build`. Both share THIS module
(fetch + version-compare + installer detection in `_install.py`), so the logic lives once.

Stdlib only (subprocess + urllib + json + re + time + pathlib) plus `franky._install`.

`force_update` shape (replicated from the reference tool's `force_update`, not imported):
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

`maybe_auto_update` shape (the parts of the reference tool's `maybe_auto_update` that transfer):
- HINT ONLY by default - Franky has NO version-pinned host<->container wire contract (unlike
  the reference tool's ipc_schema), so a stale CLI talking to a newer image is not a correctness hazard.
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


# ---------------------------------------------------------------------------
# Best-effort auto-update hint (run at the top of `franky build`)
# ---------------------------------------------------------------------------

# A tight budget: the build is waiting, so a check that can't answer fast is negative-cached
# and skipped. The common path (gh present + authed) returns well under this.
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
    out(f"franky: {AUTO_UPDATE_VAR}=1 - installing {tag} for your next build ...")
    try:
        proc = runner(argv, capture_output=True, text=True)
    except OSError:
        return
    if proc.returncode == 0:
        out(f"franky: installed {tag}; it takes effect on your next `franky build`")
    else:
        out(f"franky: auto-install of {tag} failed (exit {proc.returncode}) - run `franky update`")


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
    else:
        try:
            tag = fetch()
            status = "available" if is_newer(tag, current) else "current"
        except Exception:
            tag, status = None, "error"
        _write_cache(cache_path, {"checked_at": now(), "latest_tag": tag, "status": status})

    if status != "available" or not tag:
        return

    out(f"franky: {tag} available (installed v{current}) - run `franky update` to upgrade")
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
        fetch: Zero-arg latest-tag fetcher (default: fetch_latest_tag with a ~1s budget).
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
            fetch = lambda: fetch_latest_tag(runner=runner, timeout=_BUILD_FETCH_TIMEOUT)  # noqa: E731
        if cache_path is None:
            cache_path = default_cache_path()
        _auto_update(env, install, current, fetch, now, cache_path, out, runner)
    except Exception:
        return
