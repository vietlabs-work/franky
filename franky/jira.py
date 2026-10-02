"""Fetch a JIRA issue and return its text for use as a task body.

WHY host-side: the task container has a default-deny egress allowlist and receives NO
JIRA creds, so the agent inside the container cannot reach JIRA. We fetch on the HOST
before the container launches and pass the resulting text as the task body. JIRA creds
must stay host-side: never on argv, never in the container, never in passthrough_env,
never logged.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from .result import AuthError, NetworkError

JIRA_BASE_URL_VAR = "JIRA_BASE_URL"
JIRA_EMAIL_VAR = "JIRA_EMAIL"
JIRA_API_TOKEN_VAR = "JIRA_API_TOKEN"

# Keys scanned out of free text (PR title, branch, body). Not anchored like task.py's
# JIRA_KEY_RE: a key may sit mid-sentence or inside a /browse/ URL, but not glued to a
# preceding word character (foo_FBT-1 is not a key).
# A `/browse/KEY` URL is explicit (group "url"): its key skips the `_NOT_PROJECTS` denylist.
_KEY_SCAN_RE = re.compile(r"(?P<url>/browse/)?(?<![A-Za-z0-9_])(?P<key>[A-Z][A-Z0-9]+-\d+)")
# A branch key only counts at a path-segment start and when followed by end, `-`, `_` or `/`:
# `abc-123-fix` and `feature/abc-123-x` match, `requests-2.32.0` and `node-18.x` do not.
_BRANCH_KEY_RE = re.compile(r"(?:^|[/_])([A-Z][A-Z0-9]+-\d+)(?=$|[-_/])")
# ponytail: `v1-2` style release segments are skipped by a prefix check, not a real version parser
_VERSION_PREFIX_RE = re.compile(r"V\d+")
# ponytail: naive prefix denylist for common non-ticket tokens; a JIRA project-list lookup if noise matters
_NOT_PROJECTS = frozenset({"UTF", "SHA", "ISO", "RFC", "CVE", "HTTP", "TLS", "AES", "PEP"})

# Cap on the JIRA response body read (bytes) before decode/parse.
_MAX_RESPONSE_BYTES = 256 * 1024

# Block-type ADF nodes that should trail with a newline so paragraphs/headings separate
# visually. Module-level (not rebuilt per recursion) - see _flatten_adf.
_BLOCK_TYPES = frozenset({"paragraph", "heading", "blockquote", "codeBlock", "rule"})


def _flatten_adf(node: object) -> str:
    """Best-effort recursive walk of an Atlassian Document Format node tree.

    Handles the common subset (text, hardBreak, paragraph, heading, blockquote,
    codeBlock, listItem, rule, bulletList, orderedList). Unknown node types recurse
    into their content so we do not silently drop structured content. Exotic or
    future node types (columns, media, etc.) may be lost - this is intentional:
    the result is human-readable task text, not a lossless round-trip.
    """
    if isinstance(node, list):
        return "".join(_flatten_adf(child) for child in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    content = node.get("content", [])

    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "listItem":
        inner = "".join(_flatten_adf(child) for child in content)
        return "- " + inner.lstrip()

    inner = "".join(_flatten_adf(child) for child in content)

    if node_type in _BLOCK_TYPES:
        return inner + "\n"

    return inner


def _tag(exc: Exception, reason: str, *, stop: bool = False) -> Exception:
    """Attach a machine `reason` (never ticket text or secrets) and whether a caller fetching
    several tickets should stop (the failure would repeat for every key)."""
    exc.reason = reason  # type: ignore[attr-defined]
    exc.stop = stop  # type: ignore[attr-defined]
    return exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would re-send the Authorization header on the next hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _tag(NetworkError(f"JIRA redirected (HTTP {code}) - refusing to follow"), "network")


_no_redirect_open = urllib.request.build_opener(_NoRedirect).open


def _clean_text(text: str) -> str:
    """Collapse 3+ consecutive newlines to 2 and strip leading/trailing whitespace."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_jira_keys(title: str, head_ref: str, body: str, *, limit: int = 3) -> list[str]:
    """Up to `limit` distinct JIRA keys, ordered by source: title, then head branch, then body.

    The branch is upper-cased first and only matches at a segment start (`abc-123-fix` ->
    ABC-123; `requests-2.32.0` -> none). Prefixes in `_NOT_PROJECTS` (UTF-8, SHA-256, ...) and
    version prefixes (`v1-2`) are skipped, except for a key inside an explicit `/browse/` URL.
    """
    candidates: list[tuple[str, bool]] = []
    for text in (title, None, body):
        if text is None:
            candidates += [(k, False) for k in _BRANCH_KEY_RE.findall(head_ref.upper())]
        else:
            candidates += [(m["key"], bool(m["url"])) for m in _KEY_SCAN_RE.finditer(text)]
    keys: list[str] = []
    for key, explicit in candidates:
        prefix = key.split("-")[0]
        if key in keys or _VERSION_PREFIX_RE.fullmatch(prefix):
            continue
        if prefix in _NOT_PROJECTS and not explicit:
            continue
        keys.append(key)
        if len(keys) >= limit:
            break
    return keys


def jira_configured(env: Mapping[str, str]) -> bool:
    """True when all three JIRA vars are set and non-blank."""
    return all(
        (env.get(var) or "").strip()
        for var in (JIRA_BASE_URL_VAR, JIRA_EMAIL_VAR, JIRA_API_TOKEN_VAR)
    )


def fetch_jira_issue(
    key: str,
    env: Mapping[str, str],
    *,
    opener: Callable | None = None,
    timeout: float = 10.0,
    refuse_restricted: bool = False,
) -> str:
    """Fetch a JIRA issue by key and return a plain-text task body.

    Args:
        key: A JIRA issue key (e.g. "FOO-123"). Must already be validated by parse_task.
        env: Environment mapping to read creds from (never reads os.environ directly).
        opener: Injectable urllib.request.urlopen-compatible callable for tests.
        timeout: Seconds before the HTTP request times out.
        refuse_restricted: Also request `security` and accept only an explicit
            `"security": null`; a security level, or a response that does not say, raises.
            Also refuses redirects (the Authorization header must not follow one). Used where
            the text goes to a model (review-pr), never by build/plan.

    Returns:
        A string of the form "[FOO-123] Summary\\n\\nDescription text" (or summary-only
        if there is no description).

    Raises:
        ValueError: (FrankyError, with `.reason` and `.stop` attributes) On missing/blank creds, bad JIRA_BASE_URL scheme, HTTP errors,
            network errors, or unparseable JSON. No raised message contains the token
            or email value.
    """
    base = (env.get(JIRA_BASE_URL_VAR) or "").strip()
    email = (env.get(JIRA_EMAIL_VAR) or "").strip()
    token = (env.get(JIRA_API_TOKEN_VAR) or "").strip()

    missing = [
        var
        for var, val in [
            (JIRA_BASE_URL_VAR, base),
            (JIRA_EMAIL_VAR, email),
            (JIRA_API_TOKEN_VAR, token),
        ]
        if not val
    ]
    if missing:
        raise _tag(AuthError(f"missing JIRA creds: {', '.join(missing)}"), "auth", stop=True)

    # SSRF guard (fail-closed): only https:// with a real netloc is accepted.
    # Without this, Basic-auth creds could travel over cleartext http or a
    # non-http scheme (file://, ftp://, etc.).
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise _tag(
            NetworkError(
                f"{JIRA_BASE_URL_VAR} must be an https:// URL (got scheme={parsed.scheme!r})"
            ),
            "config",
            stop=True,
        )

    # Two-layer path-injection defense: JIRA_KEY_RE in task.py is the first gate (anchored,
    # rejects `/.?#`), quote(safe='') is the second (percent-encodes anything that slips past
    # a future caller). The key is interpolated into the REST URL path here.
    url = (
        f"{base.rstrip('/')}/rest/api/3/issue"
        f"/{urllib.parse.quote(key, safe='')}?fields=summary,description"
        f"{',security' if refuse_restricted else ''}"
    )

    credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        },
    )

    host = parsed.netloc
    # urlopen follows redirects and re-sends the Authorization header on a same-host hop.
    # JIRA_BASE_URL is operator-supplied out-of-band (a trusted Atlassian instance), so a
    # downgrade redirect is not an attacker-controlled surface; we accept the default handler.
    if opener is None:
        opener = _no_redirect_open if refuse_restricted else urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:
            payload = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 404:
            raise _tag(NetworkError(f"JIRA issue {key} not found"), "not_found") from exc
        if code in (401, 403):
            raise _tag(
                AuthError(
                    f"JIRA auth failed (HTTP {code}) - check "
                    f"{JIRA_EMAIL_VAR} / {JIRA_API_TOKEN_VAR}"
                ),
                "auth",
                stop=True,
            ) from exc
        raise _tag(NetworkError(f"JIRA fetch failed (HTTP {code}) for {key}"), "network") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _tag(
            NetworkError(f"could not reach JIRA at {host} ({exc})"), "network", stop=True
        ) from exc

    if len(payload) > _MAX_RESPONSE_BYTES:
        raise NetworkError(f"JIRA response for {key} is too large")

    try:
        data = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise NetworkError(f"JIRA returned unparseable JSON for {key}") from exc

    # A valid-JSON response can still lack the expected shape (a restricted issue, an API
    # change). Guard so it surfaces as a ValueError (clean ClickException) rather than a
    # KeyError/TypeError traceback - KeyError/TypeError are not caught by the CLI's handler.
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict) or not fields.get("summary"):
        raise NetworkError(f"JIRA response for {key} is missing fields.summary")
    if refuse_restricted and ("security" not in fields or fields["security"] is not None):
        raise _tag(
            NetworkError(f"JIRA issue {key} has a security level or no security info - skipped"),
            "restricted",
        )
    summary: str = fields["summary"]
    description = fields.get("description")

    if description is None:
        text = ""
    elif isinstance(description, str):
        text = description
    else:
        text = _clean_text(_flatten_adf(description))

    body = f"[{key}] {summary}"
    if text:
        body = f"{body}\n\n{text}"
    return body.strip()
