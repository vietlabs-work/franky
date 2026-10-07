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
MAX_QUESTIONS = 2  # question findings kept per review
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LABELS = {"blocking": "Blocking", "normal": "Major", "question": "Question", "nit": "Nit"}
_FOOTER = "<sub>Automated review by Franky</sub>"
# Host-side length caps: the prompt asks for short text, these guarantee it.
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 1500
MAX_FIELD_CHARS = 1000  # evidence / impact / fix, each
MAX_VERIFIED = 8
MAX_CLAIM_CHARS = 150
MAX_VERIFIED_EVIDENCE_CHARS = 200
REVIEW_BODY_BUDGET = 60000  # GitHub allows 65536; stay well under
_TAIL_RESERVE = 2000  # footer, the APPROVE marker and block wrappers
MAX_LIST_ITEMS = 20  # items per <details> list in the review body, then "+N more"
_VALID_VERIFIED = frozenset({"confirmed", "contradicted"})
_CITE_RE = re.compile(r"\S+:\d+")  # a file:line citation
# A GitHub PR review permalink, the only URL stored in a handoff and linked from a body.
REVIEW_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+#pullrequestreview-\d+$")
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


def _in(v: object, allowed: frozenset) -> bool:
    """Membership that never raises: a list or dict from the engine is not a member."""
    return isinstance(v, str) and v in allowed


def _text(v: object, cap: int) -> str:
    return v.strip()[:cap] if isinstance(v, str) else ""


def _shape_verified(raw: object) -> dict | None:
    """Normalize one verified claim into {claim, evidence, status}, or None (dropped silently).

    The evidence must cite `file:line`; a claim without a citation proves nothing."""
    if not isinstance(raw, dict) or not _in(raw.get("status"), _VALID_VERIFIED):
        return None
    claim = _text(raw.get("claim"), MAX_CLAIM_CHARS)
    evidence = _text(raw.get("evidence"), MAX_VERIFIED_EVIDENCE_CHARS)
    if not claim or not _CITE_RE.search(evidence):
        return None
    return {"claim": claim, "evidence": evidence, "status": raw["status"]}


def _shape_finding(raw: object, threaded: bool = False) -> dict | None:
    """Normalize one raw finding into {title, body, evidence, impact, fix, severity, file, line,
    start_line, suggestion, status}, or None.

    A finding must be a dict with a non-empty title and at least one non-empty content field
    (`evidence`, `impact`, `fix`, or the legacy `body`); anything else is malformed and dropped (the
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
    body = _text(raw.get("body"), MAX_BODY_CHARS)
    evidence, impact, fix = (
        _text(raw.get(k), MAX_FIELD_CHARS) for k in ("evidence", "impact", "fix")
    )
    if not (body or evidence or impact or fix):
        return None
    severity = raw["severity"] if _in(raw.get("severity"), _VALID_SEVERITIES) else "normal"
    file_ = raw.get("file")
    line = _pos_int(raw.get("line"))
    start = _pos_int(raw.get("start_line"))
    suggestion = raw.get("suggestion")
    return {
        "title": title.strip()[:MAX_TITLE_CHARS],
        "body": body,
        "evidence": evidence,
        "impact": impact,
        "fix": fix,
        "severity": severity,
        "file": file_.strip() if isinstance(file_, str) and file_.strip() else None,
        "line": line,
        "start_line": start if start is not None and line is not None and start < line else None,
        # An empty string is a valid suggestion: it deletes the anchored lines.
        "suggestion": suggestion[:MAX_SUGGESTION_CHARS] if isinstance(suggestion, str) else None,
        "status": raw["status"] if threaded and _in(raw.get("status"), _VALID_STATUSES) else "new",
    }


def _shape_check(raw: object) -> dict | None:
    """Normalize one raw check outcome into {name, outcome, detail}, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    outcome = raw["outcome"] if _in(raw.get("outcome"), _VALID_OUTCOMES) else "skipped"
    detail = raw.get("detail")
    return {
        "name": name.strip(),
        "outcome": outcome,
        "detail": detail.strip()[:MAX_DETAIL_CHARS] if isinstance(detail, str) else "",
    }


def build_review_findings(parsed: dict, threaded: bool = False) -> dict:
    """Normalize the parsed review payload into {summary, findings, checks, verified, has_blocking,
    dropped_malformed}.

    `dropped_malformed` counts findings dropped as malformed, and open questions over the cap;
    any of them blocks APPROVE, because the dropped finding might have been a blocker.

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
            if shaped["severity"] == "question" and shaped["status"] != "resolved":
                questions += 1
                if questions > MAX_QUESTIONS:  # the method allows at most 2 open
                    dropped += 1  # an unshown open question must still block APPROVE
                    continue
            findings.append(shaped)

    raw_checks = parsed.get("checks")
    checks: list[dict] = []
    if isinstance(raw_checks, list):
        for raw in raw_checks:
            shaped = _shape_check(raw)
            if shaped is not None:
                checks.append(shaped)

    raw_verified = parsed.get("verified")
    verified = [
        v
        for v in (
            _shape_verified(r) for r in (raw_verified if isinstance(raw_verified, list) else [])
        )
        if v is not None
    ][:MAX_VERIFIED]

    summary = parsed.get("summary")
    return {
        "verified": verified,
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


def _esc(text: str) -> str:
    """Escape model text so no HTML (a closing </details>, a marker comment) is live in a body."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(f: dict) -> str:
    return _LABELS[f["severity"]]


def _detail(f: dict) -> list[str]:
    """The Evidence / Why it matters / Suggestion lines of a finding, empty fields omitted.

    A legacy finding (`body`, none of the three fields) renders its body as it always did."""
    fields = (
        ("Evidence", f["evidence"]),
        ("Why it matters", f["impact"]),
        ("Suggestion", f["fix"]),
    )
    if any(v for _, v in fields):
        return [f"**{name}:** {_esc(v)}" for name, v in fields if v]
    return [_esc(f["body"])] if f["body"] else []


def _comment(f: dict) -> dict:
    # The heading line is a contract: match_resolved_threads and host wrappers read it.
    body = "\n\n".join([f"**{_label(f)}: {_esc(f['title'])}**", *_detail(f)])
    if f["suggestion"] is not None:
        body += f"\n\n{_fence(f['suggestion'])}"
    c = {"path": f["file"], "line": f["line"], "side": "RIGHT", "body": body}
    if f["start_line"]:
        c.update(start_line=f["start_line"], start_side="RIGHT")
    return c


def _loc(f: dict) -> str:
    if f["file"] and f["line"]:
        return f" ({_esc(f['file'])}:{f['line']})"
    return f" ({_esc(f['file'])})" if f["file"] else ""


def _block(title: str, items: list[str], sep: str, room: int) -> str:
    """A collapsed block that fits in `room` chars: items are added in order until the next
    would not fit (or MAX_LIST_ITEMS), then a "- +N more" line. The closing tag is always there,
    and the blank lines inside let GitHub render markdown."""
    head = f"<details><summary>{title} ({len(items)})</summary>\n\n"
    tail = "\n\n</details>"
    used = len(head) + len(tail) + len(sep) + len(f"- +{len(items)} more")
    shown: list[str] = []
    for item in items[:MAX_LIST_ITEMS]:
        if used + len(item) + len(sep) > room:
            break
        shown.append(item)
        used += len(item) + len(sep)
    if len(shown) < len(items):
        shown.append(f"- +{len(items) - len(shown)} more")
    return head + sep.join(shown) + tail


def _headline(shaped: dict, sha: str) -> str:
    live = [f for f in shaped["findings"] if f["status"] != "resolved" and f["severity"] != "nit"]
    blocking = sum(f["severity"] == "blocking" for f in live)
    what = (
        f"{len(live)} finding{'' if len(live) == 1 else 's'} ({blocking} blocking)."
        if live
        else "no findings above nit."
    )
    head = f"Review of {sha[:7]}: {what}" if sha else f"Review: {what}"
    return f"{head} {_esc(shaped['summary'])}".rstrip()


def _body(
    shaped: dict,
    unanchored: list[dict],
    sha: str = "",
    prior_url: str | None = None,
    prior_nits: bool = True,
) -> str:
    """Summary line visible, everything else collapsed under <details>. The whole body stays
    under REVIEW_BODY_BUDGET (room is left for the footer and the APPROVE marker)."""
    parts = [_headline(shaped, sha)]
    if prior_url and REVIEW_URL_RE.match(prior_url):
        parts.append(f"Follows [the previous review]({prior_url}).")
    room = REVIEW_BODY_BUDGET - _TAIL_RESERVE - sum(len(p) + 2 for p in parts)
    rows = [
        "\n\n".join([f"**{_label(f)}: {_esc(f['title'])}**{_loc(f)}", *_detail(f)])
        for f in unanchored
    ]
    prior = [
        f"- {'Still open' if f['status'] == 'open' else 'Fixed'}: {_esc(f['title'])}{_loc(f)}"
        for f in shaped["findings"]
        if f["status"] in ("open", "resolved") and (prior_nits or f["severity"] != "nit")
    ]
    failed = [
        f"- Failed check: {_esc(c['name'])}" + (f" - {_esc(c['detail'])}" if c["detail"] else "")
        for c in shaped["checks"]
        if c["outcome"] == "fail"
    ]
    verified = [
        f"- **{_esc(v['claim'])}** -> {_esc(v['evidence'])}"
        + (" (contradicted)" if v["status"] == "contradicted" else "")
        for v in shaped.get("verified", [])
    ]
    # Fill in priority order, show in reading order.
    blocks: dict[str, str] = {}
    for key, title, items, sep in (
        ("rows", "Findings with no line", rows, "\n\n"),
        ("prior", "Since the last review", prior, "\n"),
        ("failed", "Failed checks", failed, "\n"),
        ("verified", "What I verified", verified, "\n"),
    ):
        if items:
            blocks[key] = _block(title, items, sep, room)
            room -= len(blocks[key]) + 2
    parts += [blocks[k] for k in ("rows", "prior", "verified", "failed") if k in blocks]
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
    prior_url: str | None = None,
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
        "body": _body(shaped, rest, commit_id, prior_url, prior_nits=False),
        "commit_id": commit_id,
        "comments": [_comment(f) for f in inline],
    }
    return _redacted(payload, secrets)


def build_body_only_payload(
    shaped: dict,
    commit_id: str,
    secrets: Iterable[str],
    allow_approve: bool = False,
    prior_url: str | None = None,
) -> dict:
    """Fallback when GitHub rejects the inline anchors: every new non-nit finding goes in the body."""
    payload = {
        "event": review_event(shaped, allow_approve),
        "body": _body(shaped, _not_nit(_new(shaped)), commit_id, prior_url, prior_nits=False),
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


def render_review_body(shaped: dict, sha: str = "", prior_url: str | None = None) -> str:
    """Review body for `--no-publish`: same layout as the posted one, no inline split."""
    return _body(shaped, _new(shaped), sha, prior_url)


def review_event(shaped: dict, allow_approve: bool = False) -> str:
    """COMMENT, REQUEST_CHANGES or (only with `allow_approve`) APPROVE.

    The default can never return APPROVE. With `allow_approve`, APPROVE needs every finding above
    nit (blocking, Major, or an open question, whose deciding fact is still unknown) to be resolved,
    no finding dropped as malformed, and no failed check. The decision reads the full structured
    list, never the rendered or truncated text.
    """
    if shaped["has_blocking"]:
        return "REQUEST_CHANGES"
    if (
        allow_approve
        and not shaped.get("dropped_malformed")
        and not any(c["outcome"] == "fail" for c in shaped["checks"])
        and not any(
            f["severity"] != "nit" and f["status"] != "resolved" for f in shaped["findings"]
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
        # GitHub stores the escaped title; older comments carry the raw one.
        heads = tuple(
            f"**{label}: {t}**"
            for label in _LABELS.values()
            for t in {_esc(f["title"]), f["title"]}
        )
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
