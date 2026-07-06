"""Best-effort base-commit resolution + existence check for `franky job replay` (issue #70).

WHY this exists: a replay must pin the target repo to the EXACT commit the original run
started from, or a "reproduce this failure" pass silently drifts onto whatever the default
branch has become since. `resolve_base_sha` captures that pin at build start by asking GitHub
for the default branch's tip - this DEFINES "base commit" as the default-branch tip at the
moment the original build began, not the commit the agent may have branched from inside the
container (the agent's own branching choices are its own business; the host only needs a
stable, recorded starting point to hand back on replay).

`commit_exists` is the pre-flight check `job replay` runs before spending a container pass: a
force-pushed or garbage-collected base commit can no longer be checked out, so failing fast
host-side (no container, no cost) beats discovering it deep inside an autonomous run.

BOTH are best-effort, HOST-SIDE, stdlib urllib only (mirroring idempotency.py): any failure
(no token, network error, non-200, unparseable body) degrades to None so a flaky check never
blocks a build or wrongly refuses a replay. `opener` is injectable so tests never touch the
network.

PATH-INJECTION GUARD: `commit_exists` receives `sha` from an on-disk run record - a value this
process wrote, but still untrusted input from the caller's point of view (a hand-edited record,
a future format change). It is validated against a strict hex-sha shape BEFORE ever being
interpolated into a URL path, so a malformed value can never be smuggled into the request path.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

# The host is waiting on the build (or the replay pre-flight), so a tight budget: a slow check
# degrades to "unknown" rather than stalling the run.
_FETCH_TIMEOUT = 5.0

# LOAD-BEARING path-injection control: a sha read back from a run record is interpolated
# directly into the GitHub REST API URL path (`/repos/{repo}/commits/{sha}`). Anything that
# does not match this shape is refused BEFORE it ever reaches urllib - never widen this to
# accept traversal separators or query-smuggling characters.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def is_valid_sha(sha: object) -> bool:
    """True iff `sha` is a str matching the strict hex-sha shape (7-40 hex chars).

    The `isinstance` guard is load-bearing: a corrupt/hand-edited run record could store
    `base_sha` as a non-string (e.g. a JSON number), and `_SHA_RE.match(<non-str>)` would raise
    TypeError. This helper (and the callers that reuse it) must NEVER raise, so a non-str is
    simply "not a valid sha".
    """
    return isinstance(sha, str) and bool(_SHA_RE.match(sha))


def resolve_base_sha(
    repo: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> str | None:
    """Return the SHA of `repo`'s default-branch tip right now, or None.

    GET /repos/{repo}/commits?per_page=1 - the first (only) entry is the default branch's
    current HEAD. This is what "base commit" MEANS for a Franky run: the default-branch tip
    at build start, captured once before the attempt loop. Best-effort: any error, non-200,
    empty/non-list body, or a missing `sha` degrades to None.
    """
    url = f"https://api.github.com/repos/{repo}/commits?per_page=1"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "franky-baseref",
    }
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            if getattr(resp, "status", None) != 200:
                return None
            data = json.load(resp)
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    sha = first.get("sha")
    return sha if isinstance(sha, str) and sha else None


def commit_exists(
    repo: str,
    sha: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> bool | None:
    """Return True/False if `repo`'s commit `sha` provably does/does not exist, else None.

    Validates `sha` against the strict hex-sha shape FIRST (via is_valid_sha, which also guards
    a non-str value) - anything that fails is refused without ever building a request (never
    interpolate an untrusted value into the URL path, and never raise). GET
    /repos/{repo}/commits/{sha}: 200 -> True; 404 or 422 (GitHub uses 422 for a
    malformed-but-shape-valid sha it cannot resolve) -> False; any other status or error ->
    None (uncertain - a replay proceeds and lets the in-container checkout fail cleanly).
    """
    if not is_valid_sha(sha):
        return None

    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "franky-baseref",
    }
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            return getattr(resp, "status", None) == 200
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 422):
            return False
        return None
    except Exception:
        return None
