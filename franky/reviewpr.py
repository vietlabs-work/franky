"""Parse and shape the `franky review-pr` findings payload (pure - no I/O, no docker).

The `review-pr` command runs a read-only container pass whose final output ends in a
nonce-fenced sentinel block carrying compact JSON findings (mirrors `franky plan` / decompose.py).
This module owns the pure halves of consuming that:

- `parse_review_findings` extracts the JSON via the shared `sentinel.scan_sentinel_json` scanner
  (label "REVIEW").
- `build_review_findings` normalizes the parsed dict into the stable shape the CLI reports.
- `render_review_body` / `review_event` turn that shape into the actual GitHub review the CLI
  posts host-side.

The prompt tells the agent to emit findings JSON without writing to GitHub. Franky's host process
publishes that result. `review_event` returns COMMENT or REQUEST_CHANGES unless the caller passes
`allow_approve=True` (`review-pr --allow-approve`), the only way APPROVE is reachable. The
container is autonomous, so GitHub token permissions remain the hard external-write boundary.

`review-pr --resolve-fixed` resolves the bot's own fixed threads with two fixed GraphQL calls. A
trusted wrapper may allowlist these exact argv shapes byte for byte (`<o>`, `<n>`, `<N>`, `<id>`,
`<cursor>` are values; `THREADS_QUERY` and `RESOLVE_MUTATION` are the constants below):

    gh api graphql -f query=<THREADS_QUERY> -F owner=<o> -F name=<n> -F number=<N>
    gh api graphql -f query=<THREADS_QUERY> -F owner=<o> -F name=<n> -F number=<N> -F after=<cursor>
    gh api graphql -f query=<RESOLVE_MUTATION> -F id=<id>

Page one omits `-F after`; later pages (at most MAX_THREAD_PAGES) add it. The read must NOT pass
`-X GET`: GitHub then ignores the query and returns the schema.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import redact
from .sentinel import scan_sentinel_json

_VALID_SEVERITIES = frozenset({"blocking", "normal", "question", "nit"})
_VALID_OUTCOMES = frozenset({"pass", "fail", "skipped"})
# Per-finding status on a `--thread` re-review: new, still open, or fixed since the last review.
_VALID_STATUSES = frozenset({"new", "open", "resolved"})

# Fixed GraphQL documents (see the module docstring); never built from input.
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} "
    "nodes{id isResolved path comments(first:1){nodes{author{login __typename} body}}}}}}}"
)
RESOLVE_MUTATION = (
    "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
)
MAX_THREAD_PAGES = 5
APPROVE_MARKER = "franky-review"  # `<!-- franky-review:<nonce> -->` ends an APPROVE body

MAX_INLINE = 8  # inline comments per review
MAX_BODY_FINDINGS = 5  # lines per list in the review body
MAX_QUESTIONS = 2  # question findings kept per review
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LABELS = {"blocking": "Blocking", "normal": "Major", "question": "Question", "nit": "Nit"}
_FOOTER = "<sub>Automated review by Franky</sub>"
# Host-side length caps: the prompt asks for short text, these guarantee it.
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 1500
MAX_SUGGESTION_CHARS = 4000
MAX_SUMMARY_CHARS = 600
MAX_DETAIL_CHARS = 300


def parse_review_findings(output: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced review JSON from the engine transcript, or None.

    Thin wrapper over `sentinel.scan_sentinel_json(output, "REVIEW", nonce)` - see that module
    for the scanning details shared with `plan`/`job diagnose`. Never raises.
    """
    return scan_sentinel_json(output, "REVIEW", nonce)


def _pos_int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def _shape_finding(raw: object, threaded: bool = False) -> dict | None:
    """Normalize one raw finding into {title, body, severity, file, line, start_line,
    suggestion, status}, or None.

    A finding must be a dict with a non-empty title; anything else is malformed and dropped (the
    engine is autonomous, so we never trust the shape). An unrecognized severity defaults to
    "normal" rather than being dropped, so a slightly-off label doesn't silently lose a finding.
    A missing or unrecognized status defaults to "new", and so does every status when the run is
    not `--thread`: only a threaded re-review may mark a finding open or resolved.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    body = raw.get("body")
    severity = raw.get("severity") if raw.get("severity") in _VALID_SEVERITIES else "normal"
    file_ = raw.get("file")
    line = _pos_int(raw.get("line"))
    start = _pos_int(raw.get("start_line"))
    suggestion = raw.get("suggestion")
    return {
        "title": title.strip()[:MAX_TITLE_CHARS],
        "body": body.strip()[:MAX_BODY_CHARS] if isinstance(body, str) else "",
        "severity": severity,
        "file": file_.strip() if isinstance(file_, str) and file_.strip() else None,
        "line": line,
        "start_line": start if start is not None and line is not None and start < line else None,
        # An empty string is a valid suggestion: it deletes the anchored lines.
        "suggestion": suggestion[:MAX_SUGGESTION_CHARS] if isinstance(suggestion, str) else None,
        "status": raw.get("status") if threaded and raw.get("status") in _VALID_STATUSES else "new",
    }


def _shape_check(raw: object) -> dict | None:
    """Normalize one raw check outcome into {name, outcome, detail}, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    outcome = raw.get("outcome") if raw.get("outcome") in _VALID_OUTCOMES else "skipped"
    detail = raw.get("detail")
    return {
        "name": name.strip(),
        "outcome": outcome,
        "detail": detail.strip()[:MAX_DETAIL_CHARS] if isinstance(detail, str) else "",
    }


def build_review_findings(parsed: dict, threaded: bool = False) -> dict:
    """Normalize the parsed review payload into {summary, findings, checks, has_blocking,
    dropped_malformed}.

    `dropped_malformed` counts findings dropped as malformed; any of them blocks APPROVE, because
    the dropped finding might have been a blocker.

    `has_blocking` is derived (never trusted from the agent directly) so `review_event` has a
    single, grounded signal for whether REQUEST_CHANGES is warranted. A resolved finding never
    blocks. `threaded` (`review-pr --thread`) is the only way a status other than "new" is kept.
    """
    raw_findings = parsed.get("findings")
    findings: list[dict] = []
    dropped = 0
    if isinstance(raw_findings, list):
        questions = 0
        for raw in raw_findings:
            shaped = _shape_finding(raw, threaded)
            if shaped is None:
                dropped += 1
                continue
            if shaped["severity"] == "question":
                questions += 1
                if questions > MAX_QUESTIONS:  # the method allows at most 2
                    continue
            findings.append(shaped)

    raw_checks = parsed.get("checks")
    checks: list[dict] = []
    if isinstance(raw_checks, list):
        for raw in raw_checks:
            shaped = _shape_check(raw)
            if shaped is not None:
                checks.append(shaped)

    summary = parsed.get("summary")
    return {
        "summary": summary.strip()[:MAX_SUMMARY_CHARS] if isinstance(summary, str) else "",
        "findings": findings,
        "checks": checks,
        "dropped_malformed": dropped,
        "has_blocking": any(
            f["severity"] == "blocking" and f["status"] != "resolved" for f in findings
        ),
    }


def commentable_lines(files: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """Map each file to its diff hunks as inclusive (first, last) RIGHT-side line ranges.

    Input is GitHub's `pulls/{n}/files` list. Context and `+` lines count; `-` lines and
    `\\ No newline` markers do not. A file with no `patch` (binary, too large) or status
    "removed" gets no entry, so findings on it fall back to the review body.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        if not isinstance(f, dict) or f.get("status") == "removed":
            continue
        name, patch = f.get("filename"), f.get("patch")
        if not isinstance(name, str) or not isinstance(patch, str):
            continue
        hunks: list[tuple[int, int]] = []
        nxt = 0  # next RIGHT line number inside the open hunk
        for row in patch.split("\n"):
            m = _HUNK_RE.match(row)
            if m:
                nxt = int(m.group(1))
                hunks.append((nxt, nxt - 1))
            elif hunks and row[:1] in (" ", "+"):
                hunks[-1] = (hunks[-1][0], nxt)
                nxt += 1
        if hunks:
            out[name] = hunks
    return out


def _hunk_of(hunks: list[tuple[int, int]], line: int) -> int | None:
    for i, (lo, hi) in enumerate(hunks):
        if lo <= line <= hi:
            return i
    return None


def anchor_ok(finding: dict, commentable: dict[str, list[tuple[int, int]]]) -> bool:
    """True when file/line (and start_line, if set) are RIGHT lines of one diff hunk."""
    hunks = commentable.get(finding.get("file") or "")
    line = finding.get("line")
    if not hunks or line is None:
        return False
    idx = _hunk_of(hunks, line)
    if idx is None:
        return False
    start = finding.get("start_line")
    return start is None or (start < line and _hunk_of(hunks, start) == idx)


def _fence(suggestion: str) -> str:
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", suggestion)), default=0) + 1)
    # An empty block (no inner line) is how GitHub suggests deleting the anchored lines.
    inner = f"{suggestion}\n" if suggestion else ""
    return f"{ticks}suggestion\n{inner}{ticks}"


def _label(f: dict) -> str:
    return _LABELS[f["severity"]]


def _comment(f: dict) -> dict:
    body = f"**{_label(f)}: {f['title']}**\n\n{f['body']}".rstrip()
    if f["suggestion"] is not None:
        body += f"\n\n{_fence(f['suggestion'])}"
    c = {"path": f["file"], "line": f["line"], "side": "RIGHT", "body": body}
    if f["start_line"]:
        c.update(start_line=f["start_line"], start_side="RIGHT")
    return c


def _capped(lines: list[str], cap: int) -> list[str]:
    if len(lines) <= cap:
        return lines
    return [*lines[:cap], f"- +{len(lines) - cap} more"]


def _body(shaped: dict, unanchored: list[dict], cap: int, prior_nits: bool = True) -> str:
    parts = [shaped["summary"] or "Franky reviewed this pull request."]
    rows = []
    for f in unanchored:
        loc = f" ({f['file']}:{f['line']})" if f["file"] and f["line"] else ""
        loc = loc or (f" ({f['file']})" if f["file"] else "")
        text = f" - {f['body']}" if f["body"] else ""
        rows.append(f"- **{_label(f)}:** {f['title']}{loc}{text}")
    if rows:
        parts.append("\n".join(_capped(rows, cap)))
    prior = [
        f"- {'Still open' if f['status'] == 'open' else 'Resolved'}: {f['title']}"
        for f in shaped["findings"]
        if f["status"] in ("open", "resolved") and (prior_nits or f["severity"] != "nit")
    ]
    if prior:
        parts.append("\n".join(["Since the last review:", *_capped(prior, MAX_BODY_FINDINGS)]))
    failed = [
        f"- Failed check: {c['name']}" + (f" - {c['detail']}" if c["detail"] else "")
        for c in shaped["checks"]
        if c["outcome"] == "fail"
    ]
    if failed:
        parts.append("\n".join(failed[:MAX_BODY_FINDINGS]))
    parts.append(_FOOTER)
    return "\n\n".join(parts)


def _new(shaped: dict) -> list[dict]:
    return [f for f in shaped["findings"] if f["status"] == "new"]


_RANK = {"blocking": 0, "normal": 1, "question": 2, "nit": 3}


def _not_nit(findings: list[dict]) -> list[dict]:
    # A nit is posted inline or not at all: it never reaches the review body.
    return [f for f in findings if f["severity"] != "nit"]


def build_review_payload(
    shaped: dict,
    commentable: dict[str, list[tuple[int, int]]],
    commit_id: str,
    secrets: Iterable[str],
    allow_approve: bool = False,
) -> dict:
    """The `POST pulls/{n}/reviews` body: anchored findings inline, the rest in a short body.

    Anchored findings are cut to MAX_INLINE by severity (blocking, normal, question, nit; stable
    inside a severity), so a nit never displaces a Major. Nits that miss the cut are dropped.
    """
    new = _new(shaped)
    anchored = sorted(
        (f for f in new if anchor_ok(f, commentable)), key=lambda f: _RANK[f["severity"]]
    )
    inline = anchored[:MAX_INLINE]
    rest = _not_nit([f for f in new if f not in inline])
    payload = {
        "event": review_event(shaped, allow_approve),
        "body": _body(shaped, rest, MAX_BODY_FINDINGS, prior_nits=False),
        "commit_id": commit_id,
        "comments": [_comment(f) for f in inline],
    }
    return _redacted(payload, secrets)


def build_body_only_payload(
    shaped: dict, commit_id: str, secrets: Iterable[str], allow_approve: bool = False
) -> dict:
    """Fallback when GitHub rejects the inline anchors: every new non-nit finding goes in the body."""
    payload = {
        "event": review_event(shaped, allow_approve),
        "body": _body(shaped, _not_nit(_new(shaped)), MAX_INLINE, prior_nits=False),
        "commit_id": commit_id,
        "comments": [],
    }
    return _redacted(payload, secrets)


def _redacted(payload: dict, secrets: Iterable[str]) -> dict:
    secrets = list(secrets)
    payload["body"] = redact(payload["body"], secrets)
    for c in payload["comments"]:
        c["body"] = redact(c["body"], secrets)
    return payload


def render_review_body(shaped: dict) -> str:
    """Review body for `--no-publish`: same shape and caps as the posted one, no inline split."""
    return _body(shaped, _new(shaped), 8)


def review_event(shaped: dict, allow_approve: bool = False) -> str:
    """COMMENT, REQUEST_CHANGES or (only with `allow_approve`) APPROVE.

    The default can never return APPROVE. With `allow_approve`, APPROVE needs every finding that
    is blocking or Major to be resolved, no finding dropped as malformed, and no failed check. The
    decision reads the full structured list, never the rendered or truncated text.
    """
    if shaped["has_blocking"]:
        return "REQUEST_CHANGES"
    if (
        allow_approve
        and not shaped.get("dropped_malformed")
        and not any(c["outcome"] == "fail" for c in shaped["checks"])
        and not any(
            f["severity"] in ("blocking", "normal") and f["status"] != "resolved"
            for f in shaped["findings"]
        )
    ):
        return "APPROVE"
    return "COMMENT"


def match_resolved_threads(shaped: dict, nodes: list, login: str | None) -> list[str]:
    """Ids of the publishing bot's own unresolved threads for findings now marked resolved.

    A finding matches a thread when the thread is open, its first comment is by a Bot whose login
    is `login` (case-insensitive; no login resolves nothing), its path is the finding's file and
    its body starts with `**{Label}: {title}**` for any severity label (the severity may have
    changed since the comment). Zero or 2+ candidates skip that finding. A finding never matches
    when a new/open finding has the same file and title, and new/open findings never match.
    """
    if not login:
        return []
    login = login.lower()
    live = {(f["file"], f["title"]) for f in shaped["findings"] if f["status"] != "resolved"}
    ids: list[str] = []
    for f in shaped["findings"]:
        if f["status"] != "resolved" or not f["file"] or (f["file"], f["title"]) in live:
            continue
        heads = tuple(f"**{label}: {f['title']}**" for label in _LABELS.values())
        found = []
        for n in nodes:
            if not isinstance(n, dict) or n.get("isResolved") is not False:
                continue
            if n.get("path") != f["file"] or not isinstance(n.get("id"), str):
                continue
            first = ((n.get("comments") or {}).get("nodes") or [None])[0]
            if not isinstance(first, dict):
                continue
            author = first.get("author") or {}
            body = first.get("body")
            if (
                author.get("__typename") == "Bot"
                and str(author.get("login", "")).lower() == login
                and isinstance(body, str)
                and body.startswith(heads)
            ):
                found.append(n["id"])
        if len(found) == 1 and found[0] not in ids:
            ids.append(found[0])
    return ids
