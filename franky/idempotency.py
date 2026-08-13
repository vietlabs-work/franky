"""Best-effort idempotency pre-check for `franky build`: is a Franky PR already open?

WHY this exists: the primary caller is an agent that may retry a `build` (a dropped
connection, a flaky run, an over-eager loop). Without a guard, a retry spins up a fresh
container and opens a SECOND PR for the same task. `build` computes a deterministic branch
name (`franky/<slug>`, see prompt.task_slug) and asks GitHub whether an open PR already uses
that head branch; if so it short-circuits and reports the existing PR instead of building.

BEST-EFFORT by design: this is a convenience guard, not a safety boundary. ANY failure
(no token, network error, non-200, unparseable body, empty result) degrades to None ("no
existing PR found") so a flaky check never blocks a legitimate build. Stdlib urllib only,
mirroring update_check.py's host-side fetch style; `opener` is injectable so tests pass a
fake and never touch the network.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping

# The host is waiting on the build, so a tight budget: a slow check is treated as "no PR"
# rather than stalling the run.
_FETCH_TIMEOUT = 5.0


def find_open_pr(
    repo: str,
    branch: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> str | None:
    """Return the html_url of an open PR whose head branch is `branch`, or None.

    GitHub REST: GET /repos/{repo}/pulls?head={owner}:{branch}&state=open. The head filter
    REQUIRES the `owner:` prefix (owner = the first path segment of `repo`). Auth header is
    added only when GH_TOKEN is present. Best-effort: any error returns None.
    """
    owner = repo.split("/")[0]
    url = f"https://api.github.com/repos/{repo}/pulls?head={owner}:{branch}&state=open"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "franky-idempotency",
    }
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            # A response with no observable status is treated as non-200 (fail to "no PR"),
            # consistent with the "any uncertainty degrades to None" contract.
            if getattr(resp, "status", None) != 200:
                return None
            data = json.load(resp)
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None
    # The head=owner:branch filter guarantees at most one open PR per head branch, so the
    # first element is the (only) match.
    first = data[0]
    if not isinstance(first, dict):
        return None
    html_url = first.get("html_url")
    return html_url if isinstance(html_url, str) and html_url else None


def fetch_pr_head_sha(
    repo: str,
    number: int,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> str | None:
    """Return the LIVE head commit SHA of PR `number` in `repo`, or None on any failure.

    GitHub REST: GET /repos/{repo}/pulls/{number}. Same opener-injection + degrade-to-None
    style as find_open_pr. UNLIKE find_open_pr (a best-effort idempotency convenience), the
    `review-pr` caller treats a None here as FAIL-CLOSED - it refuses rather than reviewing or
    publishing against an unconfirmed head, since this is the ground truth a review is pinned
    to (both before the pass starts and again immediately before publishing). The leniency is
    the caller's policy, not this function's - it always just degrades to None on any error.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "franky-idempotency",
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

    if not isinstance(data, dict):
        return None
    head = data.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None
