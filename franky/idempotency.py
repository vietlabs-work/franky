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
import urllib.parse
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
    error_sink: dict | None = None,
) -> str | None:
    """Return the LIVE head commit SHA of PR `number` in `repo`, or None on any failure.

    `error_sink`, when given, gets {"reason": ...} on a None return: `HTTP <code>`, the
    exception class name (never its message, which can echo a URL), or `bad response`.

    GitHub REST: GET /repos/{repo}/pulls/{number}. Same opener-injection + degrade-to-None
    style as find_open_pr. UNLIKE find_open_pr (a best-effort idempotency convenience), the
    `review-pr` caller treats a None here as FAIL-CLOSED - it refuses rather than reviewing or
    publishing against an unconfirmed head, since this is the ground truth a review is pinned
    to (both before the pass starts and again immediately before publishing). The leniency is
    the caller's policy, not this function's - it always just degrades to None on any error.

    `meta_sink` (review-pr) is filled from the SAME response with {"title", "body", "head_ref",
    "base_sha", "private", "author"} (non-string text becomes ""; `private` is True only for a literal
    `true`; `author` is the PR opener's login or None) - only on the valid-sha return path, so no extra GitHub call
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

    def fail(reason: str) -> None:
        if error_sink is not None:
            error_sink["reason"] = reason

    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status != 200:
                fail(f"HTTP {status}")
                return None
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code}")
        return None
    except Exception as exc:
        fail(type(exc).__name__)
        return None

    head = data.get("head") if isinstance(data, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        fail("bad response")
        return None
    if meta_sink is not None:
        base = data.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        user = data.get("user")
        login = user.get("login") if isinstance(user, dict) else None

        def _text(value: object) -> str:
            return value if isinstance(value, str) else ""

        meta_sink.update(
            title=_text(data.get("title")),
            body=_text(data.get("body")),
            head_ref=_text(head.get("ref")),
            base_sha=_text(base.get("sha") if isinstance(base, dict) else None),
            private=isinstance(base_repo, dict) and base_repo.get("private") is True,
            author=login if isinstance(login, str) else None,
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
_SIGNOFF_CONTENT_PATH = ".github/human-signoff-content"
_SIGNOFF_MAX_BYTES = 64 * 1024
CONTENT_FILE_MAX_BYTES = 1024 * 1024


def fetch_repo_file(
    repo: str,
    path: str,
    sha: str,
    env: Mapping[str, str],
    *,
    max_bytes: int = _SIGNOFF_MAX_BYTES,
    absent: bytes | None = b"",
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> bytes | None:
    """The bytes of `path` at commit `sha` of `repo` (GitHub REST contents API, base64).

    `absent` (default b"") when the file does not exist (HTTP 404; pass None to treat a missing
    file as unreadable) and None on ANY other failure, a non-file, an
    unexpected encoding or a size over `max_bytes` (fail closed: callers treat None as unknown).
    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha or ""):
        return None
    quoted = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{repo}/contents/{quoted}?ref={sha}"
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
        return absent if exc.code == 404 else None
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    size, content = data.get("size"), data.get("content")
    if data.get("encoding") != "base64" or not isinstance(content, str):
        return None
    if not isinstance(size, int) or size > max_bytes:
        return None
    try:
        return base64.b64decode(content.replace("\n", ""), validate=True)
    except (binascii.Error, ValueError):
        return None


def _list_lines(raw: bytes | None) -> list[str] | None:
    """Non-blank, non-comment lines of a UTF-8 file, or None for an unreadable one."""
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


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
    """
    raw = fetch_repo_file(repo, _SIGNOFF_PATH, sha, env, opener=opener, timeout=timeout)
    return _list_lines(raw)


def fetch_signoff_content_rules(
    repo: str,
    sha: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = _FETCH_TIMEOUT,
) -> list[tuple[str, str]] | None:
    """The `<glob> <fixed string>` rules of `.github/human-signoff-content` at `sha`.

    Same contract as `fetch_signoff_globs`: [] for a missing file, None on any failure or a
    malformed line (a rule with no string), read at the PR BASE commit."""
    lines = _list_lines(
        fetch_repo_file(repo, _SIGNOFF_CONTENT_PATH, sha, env, opener=opener, timeout=timeout)
    )
    if lines is None:
        return None
    rules = []
    for line in lines:
        glob, _sep, needle = line.partition(" ")
        if not glob or not needle.strip():
            return None
        rules.append((glob, needle.strip()))
    return rules
