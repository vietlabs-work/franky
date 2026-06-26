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


def _clean_text(text: str) -> str:
    """Collapse 3+ consecutive newlines to 2 and strip leading/trailing whitespace."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_jira_issue(
    key: str,
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = 10.0,
) -> str:
    """Fetch a JIRA issue by key and return a plain-text task body.

    Args:
        key: A JIRA issue key (e.g. "FOO-123"). Must already be validated by parse_task.
        env: Environment mapping to read creds from (never reads os.environ directly).
        opener: Injectable urllib.request.urlopen-compatible callable for tests.
        timeout: Seconds before the HTTP request times out.

    Returns:
        A string of the form "[FOO-123] Summary\\n\\nDescription text" (or summary-only
        if there is no description).

    Raises:
        ValueError: On missing/blank creds, bad JIRA_BASE_URL scheme, HTTP errors,
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
        raise AuthError(f"missing JIRA creds: {', '.join(missing)}")

    # SSRF guard (fail-closed): only https:// with a real netloc is accepted.
    # Without this, Basic-auth creds could travel over cleartext http or a
    # non-http scheme (file://, ftp://, etc.).
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise NetworkError(
            f"{JIRA_BASE_URL_VAR} must be an https:// URL (got scheme={parsed.scheme!r})"
        )

    # Two-layer path-injection defense: JIRA_KEY_RE in task.py is the first gate (anchored,
    # rejects `/.?#`), quote(safe='') is the second (percent-encodes anything that slips past
    # a future caller). The key is interpolated into the REST URL path here.
    url = (
        f"{base.rstrip('/')}/rest/api/3/issue"
        f"/{urllib.parse.quote(key, safe='')}?fields=summary,description"
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
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 404:
            raise NetworkError(f"JIRA issue {key} not found") from exc
        if code in (401, 403):
            raise AuthError(
                f"JIRA auth failed (HTTP {code}) - check {JIRA_EMAIL_VAR} / {JIRA_API_TOKEN_VAR}"
            ) from exc
        raise NetworkError(f"JIRA fetch failed (HTTP {code}) for {key}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NetworkError(f"could not reach JIRA at {host} ({exc})") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise NetworkError(f"JIRA returned unparseable JSON for {key}") from exc

    # A valid-JSON response can still lack the expected shape (a restricted issue, an API
    # change). Guard so it surfaces as a ValueError (clean ClickException) rather than a
    # KeyError/TypeError traceback - KeyError/TypeError are not caught by the CLI's handler.
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict) or not fields.get("summary"):
        raise NetworkError(f"JIRA response for {key} is missing fields.summary")
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
