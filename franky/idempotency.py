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

import base64
import binascii
import json
import re
import urllib.error
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
    meta_sink: dict | None = None,
) -> str | None:
    """Return the LIVE head commit SHA of PR `number` in `repo`, or None on any failure.

    GitHub REST: GET /repos/{repo}/pulls/{number}. Same opener-injection + degrade-to-None
    style as find_open_pr. UNLIKE find_open_pr (a best-effort idempotency convenience), the
    `review-pr` caller treats a None here as FAIL-CLOSED - it refuses rather than reviewing or
    publishing against an unconfirmed head, since this is the ground truth a review is pinned
    to (both before the pass starts and again immediately before publishing). The leniency is
    the caller's policy, not this function's - it always just degrades to None on any error.

    `meta_sink` (review-pr) is filled from the SAME response with {"title", "body", "head_ref",
    "base_sha", "private"} (non-string text becomes ""; `private` is True only for a literal
    `true`) - only on the valid-sha return path, so no extra GitHub call
    shape exists and a failed pin leaves the sink untouched. The sha stays fail-closed.
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
    if not isinstance(sha, str) or not sha:
        return None
    if meta_sink is not None:
        base = data.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None

        def _text(value: object) -> str:
            return value if isinstance(value, str) else ""

        meta_sink.update(
            title=_text(data.get("title")),
            body=_text(data.get("body")),
            head_ref=_text(head.get("ref")),
            base_sha=_text(base.get("sha") if isinstance(base, dict) else None),
            private=isinstance(base_repo, dict) and base_repo.get("private") is True,
        )
    return sha


def repo_is_private(
    repo: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> bool:
    """True only when GitHub says `repo` is private. Any failure is False (fail closed): this
    gates credentials entering the task container, so uncertainty must mean "no"."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "franky-idempotency"}
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            if getattr(resp, "status", None) != 200:
                return False
            data = json.load(resp)
    except Exception:
        return False
    return isinstance(data, dict) and data.get("private") is True


_SIGNOFF_PATH = ".github/human-signoff-paths"
_SIGNOFF_MAX_BYTES = 64 * 1024


def fetch_signoff_globs(
    repo: str,
    sha: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> list[str] | None:
    """The glob lines of `.github/human-signoff-paths` at commit `sha` of `repo`.

    `review-pr` passes the PR BASE commit, never the head: the PR author controls the head.
    Returns [] when the file does not exist (HTTP 404: no floor) and None on ANY other failure
    (fail closed: the caller must not approve). Blank lines and `#` comments are dropped.

    GitHub REST: GET /repos/{repo}/contents/{path}?ref={sha}. Same urllib + GH_TOKEN style as
    fetch_pr_head_sha.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha or ""):
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{_SIGNOFF_PATH}?ref={sha}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "franky-idempotency"}
    token = env.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            if getattr(resp, "status", None) != 200:
                return None
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return [] if exc.code == 404 else None
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    size, content = data.get("size"), data.get("content")
    if data.get("encoding") != "base64" or not isinstance(content, str):
        return None
    if not isinstance(size, int) or size > _SIGNOFF_MAX_BYTES:
        return None
    try:
        text = base64.b64decode(content.replace("\n", ""), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]
