"""Parse and shape the `franky review-pr` findings payload (pure - no I/O, no docker).

The `review-pr` command runs a read-only container pass whose final output ends in a
nonce-fenced sentinel block carrying compact JSON findings (mirrors `franky plan` / decompose.py).
This module owns the pure halves of consuming that:

- `parse_review_findings` extracts the JSON via the shared `sentinel.scan_sentinel_json` scanner
  (label "REVIEW").
- `build_review_findings` normalizes the parsed dict into the stable shape the CLI reports.
- `render_review_body` / `review_event` turn that shape into the actual GitHub review the CLI
  posts host-side.

`review_event` with `allow_approve` is only the findings half of the APPROVE decision. The CLI
also applies a human sign-off floor (`human_signoff_required`): a PR that changes a path listed in
the reviewed repo's `.github/human-signoff-paths` (read at the PR base commit), changes that file,
or cannot be checked is posted as a COMMENT that says so. That costs one extra GitHub read.

The prompt tells the agent to emit findings JSON without writing to GitHub. Franky's host process
publishes that result. `review_event` returns COMMENT or REQUEST_CHANGES unless the caller passes
`allow_approve=True` (`review-pr --allow-approve`), the only way APPROVE is reachable. The
container is autonomous, so GitHub token permissions remain the hard external-write boundary.

`review-pr --resolve-fixed` resolves the bot's own fixed threads and `--reply-prior` replies once
on its still-open ones. A trusted wrapper may allowlist these exact argv shapes byte for byte
(`<o>`, `<n>`, `<N>`, `<id>`, `<cursor>`, `<repo>`, `<cid>`, `<body>` are values; `THREADS_QUERY` and
`RESOLVE_MUTATION` are the constants below; `<cid>` is the thread's first comment databaseId):

    gh api graphql -f query=<THREADS_QUERY> -F owner=<o> -F name=<n> -F number=<N>
    gh api graphql -f query=<THREADS_QUERY> -F owner=<o> -F name=<n> -F number=<N> -F after=<cursor>
    gh api graphql -f query=<RESOLVE_MUTATION> -F id=<id>
    gh api -X POST repos/<repo>/pulls/<N>/comments/<cid>/replies -f body=<body>

Page one omits `-F after`; later pages (at most MAX_THREAD_PAGES) add it. The read must NOT pass
`-X GET`: GitHub then ignores the query and returns the schema. It returns each thread's first
comment (with its databaseId) and its last comment, which `thread_reply_target` reads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime

from .config import redact
from .sentinel import scan_sentinel_json

SIGNOFF_FILE = ".github/human-signoff-paths"
SIGNOFF_LINE = "code-clean, human sign-off required (`.github/human-signoff-paths`)."
_FILES_API_CAP = 3000  # the PR files API never lists more than this many files

_VALID_SEVERITIES = frozenset({"blocking", "normal", "question", "nit"})
_VALID_OUTCOMES = frozenset({"pass", "fail", "skipped"})
# Per-finding status on a `--thread` re-review: new, still open, or fixed since the last review.
_VALID_STATUSES = frozenset({"new", "open", "resolved", "accepted"})
_CLEARED = frozenset({"resolved", "accepted"})  # statuses that no longer block or stay live
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # a GitHub login

# Fixed GraphQL documents (see the module docstring); never built from input.
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} "
    "nodes{id isResolved path comments(first:1){nodes{databaseId author{login __typename} body}} "
    "last:comments(last:1){nodes{databaseId author{login __typename}}}}}}}}"
)
RESOLVE_MUTATION = (
    "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
)
MAX_THREAD_PAGES = 5
MAX_REPLY_EVIDENCE_CHARS = 600
MAX_REPLY_CHARS = 2000  # the host gh wrapper refuses a longer reply body
APPROVE_MARKER = "franky-review"  # `<!-- franky-review:<nonce> -->` ends an APPROVE body

_EFFECTIVE_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
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
    not `--thread`: only a threaded re-review may mark a finding open, resolved or accepted.
    "accepted" needs an `accepted_by` GitHub login; without one it is "open". Franky's host
    still verifies the acceptance (`verify_accepted`), this only shapes the claim.
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
    status = raw["status"] if threaded and _in(raw.get("status"), _VALID_STATUSES) else "new"
    accepted_by = raw.get("accepted_by")
    accepted_by = accepted_by.strip().lstrip("@") if isinstance(accepted_by, str) else ""
    if status == "accepted" and not _LOGIN_RE.match(accepted_by):
        status = "open"
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
        "status": status,
        "accepted_by": accepted_by if status == "accepted" else None,
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
    blocks, and neither does an accepted one. `threaded` (`review-pr --thread`) is the only way a
    status other than "new" is kept.
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
            if shaped["severity"] == "question" and shaped["status"] not in _CLEARED:
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
        "has_blocking": _has_blocking(findings),
    }


def _has_blocking(findings: list[dict]) -> bool:
    return any(f["severity"] == "blocking" and f["status"] not in _CLEARED for f in findings)


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
    live = [f for f in shaped["findings"] if f["status"] not in _CLEARED and f["severity"] != "nit"]
    live += shaped.get("omitted_prior") or []
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
    big = [f for f in shaped["findings"] if f["status"] == "accepted" and f["severity"] != "nit"]
    if big:
        who = ", ".join(dict.fromkeys(f"@{_esc(f['accepted_by'])}" for f in big))
        parts.append(
            f"Accepted risk: {len(big)} finding(s) above nit accepted by {who} "
            "(approval on this commit)."
        )
    if prior_url and REVIEW_URL_RE.match(prior_url):
        parts.append(f"Follows [the previous review]({prior_url}).")
    if shaped.get("signoff_required"):
        parts.append(SIGNOFF_LINE)
    room = REVIEW_BODY_BUDGET - _TAIL_RESERVE - sum(len(p) + 2 for p in parts)
    rows = [
        "\n\n".join([f"**{_label(f)}: {_esc(f['title'])}**{_loc(f)}", *_detail(f)])
        for f in unanchored
    ]
    prior = [
        f"- Not re-reported, still open: {_esc(f.get('title') or '')}"
        + (f" ({_esc(f['file'])})" if f.get("file") else "")
        for f in shaped.get("omitted_prior") or []
    ] + [
        (
            f"- Accepted risk (by @{_esc(f['accepted_by'])}): {_esc(f['title'])}{_loc(f)}"
            if f["status"] == "accepted"
            else f"- {'Still open' if f['status'] == 'open' else 'Fixed'}: "
            f"{_esc(f['title'])}{_loc(f)}"
        )
        for f in shaped["findings"]
        if f["status"] in ("open", "resolved", "accepted")
        and (prior_nits or f["severity"] != "nit")
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


def _signoff_regex(pattern: str) -> re.Pattern:
    """One gitignore-style glob as a regex over a repo-root-relative path.

    shortcut: no [...] classes, no \\# or \\! escapes, no parent-exclusion rule; upgrade if a repo
    needs them.
    """
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    # A match on a directory covers every file under it.
    return re.compile(
        ("" if anchored else "(?:.*/)?") + "".join(out) + ("/.+" if dir_only else "(?:/.+)?")
    )


def signoff_match(globs: Iterable[str], paths: Iterable[str]) -> bool:
    """True when any path ends up matched: a leading `!` negates, the last matching rule wins."""
    rules = [(g.startswith("!"), _signoff_regex(g.removeprefix("!"))) for g in globs]
    for path in paths:
        matched = False
        for negated, regex in rules:
            if regex.fullmatch(path):
                matched = not negated
        if matched:
            return True
    return False


def human_signoff_required(files: list | None, read_globs: Callable[[], list[str] | None]) -> bool:
    """True when a review that would be an APPROVE must stay a COMMENT.

    `files` is the PR files list (None when it could not be read). Fails closed: an unreadable
    or full-size list, a malformed entry, a PR that changes the sign-off file itself (it cannot
    remove its own floor), or an unreadable globs file all give True. A renamed file counts under
    both names. `read_globs` runs only when needed and returns [] for an absent file.
    """
    if files is None or len(files) >= _FILES_API_CAP:
        return True
    paths: list[str] = []
    for entry in files:
        name = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            return True
        paths.append(name)
        if isinstance(entry.get("previous_filename"), str):
            paths.append(entry["previous_filename"])
        elif entry.get("status") == "renamed":
            return True  # a rename that hides its old path cannot be checked
    if not paths:
        return False
    if SIGNOFF_FILE in paths:
        return True
    globs = read_globs()
    return globs is None or signoff_match(globs, paths)


def review_event(shaped: dict, allow_approve: bool = False) -> str:
    """COMMENT, REQUEST_CHANGES or (only with `allow_approve`) APPROVE.

    The default can never return APPROVE. With `allow_approve`, APPROVE needs every finding above
    nit (blocking, Major, or an open question, whose deciding fact is still unknown) to be resolved
    or accepted (an acceptance `verify_accepted` confirmed),
    no finding dropped as malformed, no prior finding left out of a threaded re-review
    (`omitted_prior`; an omitted blocking one gives REQUEST_CHANGES), no thread handoff that
    overflowed (`prior_overflow`), and no failed check. The decision reads the full structured
    list, never the rendered or truncated text.
    """
    omitted = shaped.get("omitted_prior") or []
    if shaped["has_blocking"] or any(f.get("severity") == "blocking" for f in omitted):
        return "REQUEST_CHANGES"
    if (
        allow_approve
        and not shaped.get("dropped_malformed")
        and not omitted
        and not shaped.get("prior_overflow")
        and not any(c["outcome"] == "fail" for c in shaped["checks"])
        and not any(
            f["severity"] != "nit" and f["status"] not in _CLEARED for f in shaped["findings"]
        )
    ):
        return "APPROVE"
    return "COMMENT"


def _title_key(title: object) -> str:
    return title.casefold().rstrip(".:;!? ") if isinstance(title, str) else ""


def _utc(value: object) -> datetime | None:
    """An ISO-8601 UTC timestamp as an aware datetime, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


_AUTHORITY = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})  # review author_association


def _approval_ok(reviews: list, login: str, head_sha: str, raised_at: object) -> bool:
    """True when `login`'s latest effective review is a human APPROVED on `head_sha`, submitted
    after `raised_at`, by an owner, member or collaborator. COMMENTED and PENDING reviews do not
    count either way. Any effective review of the login with an unreadable `submitted_at`, or a
    newest-time tie that review ids cannot break, fails closed."""
    raised = _utc(raised_at)
    mine = []
    for r in map(_dict, reviews):
        user = _dict(r.get("user"))
        if (
            str(user.get("login", "")).lower() == login.lower()
            and r.get("state") in _EFFECTIVE_REVIEW_STATES
        ):
            at = _utc(r.get("submitted_at"))
            if at is None:
                return False
            mine.append((at, r, user))
    if raised is None or not mine:
        return False
    newest = max(m[0] for m in mine)
    last = [m for m in mine if m[0] == newest]
    if len(last) > 1:
        ids = [m[1].get("id") for m in last]
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            return False
        top = max(ids)
        if ids.count(top) > 1:
            return False
        last = [m for m in last if m[1]["id"] == top]
    at, review, user = last[0]
    return (
        review["state"] == "APPROVED"
        and str(review.get("commit_id", "")).lower() == head_sha.lower()
        and at > raised
        and isinstance(review.get("author_association"), str)
        and review["author_association"] in _AUTHORITY
        and user.get("type") != "Bot"
        and not str(user.get("login", "")).lower().endswith("[bot]")
        and APPROVE_MARKER not in str(review.get("body") or "")
    )


def verify_accepted(
    shaped: dict,
    reviews: list | None,
    head_sha: str,
    pr_author: str | None,
    prior_findings: object,
) -> None:
    """Keep an "accepted" finding only when GitHub itself shows the named person accepted it.

    The engine claims `accepted_by`; PR text is untrusted, so the host checks the claim. In place,
    every accepted finding that fails any check becomes "open" (accepted_by None), the question
    cap is applied again, and `has_blocking` is recomputed. A finding stays accepted only when
    ALL hold:
    - `reviews` was read and `pr_author` is known, and the accepter is not the PR author;
    - it matches exactly ONE prior handoff finding (same file, same title key) that no other
      accepted finding already used;
    - the accepter's latest effective review (APPROVED, CHANGES_REQUESTED or DISMISSED) is an
      APPROVED on `head_sha`, submitted strictly after the prior finding's `raised_at`, and its
      `author_association` is OWNER, MEMBER or COLLABORATOR;
    - the accepter is not a bot and the review is not one of Franky's own (APPROVE marker).
    Never raises on malformed input.
    """
    prior = (
        [p for p in prior_findings if isinstance(p, dict)]
        if isinstance(prior_findings, list)
        else []
    )
    used: set[int] = set()
    demoted = False
    for f in shaped["findings"]:
        if f["status"] != "accepted":
            continue
        ok = isinstance(reviews, list) and isinstance(pr_author, str) and bool(pr_author)
        ok = ok and f["accepted_by"].lower() != pr_author.lower()
        hits = [
            i
            for i, p in enumerate(prior)
            if p.get("file") == f["file"] and _title_key(p.get("title")) == _title_key(f["title"])
        ]
        if ok and len(hits) == 1 and hits[0] not in used:
            used.add(hits[0])
            ok = _approval_ok(reviews, f["accepted_by"], head_sha, prior[hits[0]].get("raised_at"))
        else:
            ok = False
        if not ok:
            f["status"], f["accepted_by"] = "open", None
            demoted = True
    if demoted:  # a demoted question counts against the open-question cap again
        kept, questions = [], 0
        for f in shaped["findings"]:
            if f["severity"] == "question" and f["status"] not in _CLEARED:
                questions += 1
                if questions > MAX_QUESTIONS:
                    shaped["dropped_malformed"] += 1
                    continue
            kept.append(f)
        shaped["findings"] = kept
    shaped["has_blocking"] = _has_blocking(shaped["findings"])


def _dict(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def _first_node(conn: object) -> dict:
    """The first comment node of a `comments`-shaped value, or {} when malformed."""
    nodes = _dict(conn).get("nodes")
    return _dict(nodes[0]) if isinstance(nodes, list) and nodes else {}


def match_prior_threads(
    shaped: dict, nodes: list, login: str | None
) -> list[tuple[str, dict, list[dict], dict | None]]:
    """The publishing bot's own unresolved threads matched to open/resolved findings.

    Returns `(decision, node, findings, reply_finding)` per thread: decision is "resolve" when
    every matched finding is resolved, "open" when any is open (it is never resolved).
    `reply_finding` is the first open finding matched by path, or None: an open finding that
    matched only through the title fallback blocks a resolve but never earns a reply.
    A thread is a candidate when it is open, its first comment is by a Bot whose login is `login`
    (case-insensitive; no login matches nothing) and its body starts with `**{Label}: {title}**`
    for any severity label (the severity may have changed since the comment). A finding picks the
    candidates on its own file; if none, any candidate by title (the model may report a prior
    finding under another file). Zero or 2+ candidates skip that finding. A resolved finding is
    skipped when a new/open finding has the same file and title (same title in the fallback).
    Malformed nodes are skipped, never raised on.
    """
    if not login or not isinstance(nodes, list):
        return []
    login = login.lower()
    own = []
    for n in nodes:
        if not isinstance(n, dict) or n.get("isResolved") is not False:
            continue
        if not isinstance(n.get("id"), str):
            continue
        first = _first_node(n.get("comments"))
        author = _dict(first.get("author"))
        body = first.get("body")
        if (
            author.get("__typename") == "Bot"
            and str(author.get("login", "")).lower() == login
            and isinstance(body, str)
        ):
            own.append((n, body))
    live = {(f["file"], f["title"]) for f in shaped["findings"] if f["status"] != "resolved"}
    live_titles = {t for _f, t in live}
    grouped: dict[str, tuple[dict, list[dict], list[dict]]] = {}
    for f in shaped["findings"]:
        if f["status"] not in ("open", "resolved"):
            continue
        # GitHub stores the escaped title; older comments carry the raw one.
        heads = tuple(
            f"**{label}: {t}**"
            for label in _LABELS.values()
            for t in {_esc(f["title"]), f["title"]}
        )
        titled = [n for n, body in own if body.startswith(heads)]
        by_path = [n for n in titled if f["file"] and n.get("path") == f["file"]]
        found = by_path or titled
        if len(found) != 1:
            continue
        if f["status"] == "resolved" and (
            (f["file"], f["title"]) in live if by_path else f["title"] in live_titles
        ):
            continue
        entry = grouped.setdefault(found[0]["id"], (found[0], [], []))
        entry[1].append(f)
        if f["status"] == "open" and by_path:
            entry[2].append(f)
    return [
        ("open" if any(f["status"] == "open" for f in fs) else "resolve", n, fs, (rs or [None])[0])
        for n, fs, rs in grouped.values()
    ]


def match_resolved_threads(shaped: dict, nodes: list, login: str | None) -> list[str]:
    """Ids of the matched threads whose findings are all resolved (see `match_prior_threads`)."""
    matched = match_prior_threads(shaped, nodes, login)
    return [n["id"] for d, n, _fs, _rf in matched if d == "resolve"]


def thread_reply_target(node: dict) -> int | None:
    """The databaseId of the thread's first comment when a reply is due, else None.

    Due when nobody replied yet (last is first) or the last comment is by a non-Bot author (a
    human posted after). A Bot last, ours or another, means no reply. Anything malformed, an
    author without a string `__typename` included, fails closed.
    """
    node = _dict(node)
    first, last = _first_node(node.get("comments")), _first_node(node.get("last"))
    fid, lid = _pos_int(first.get("databaseId")), _pos_int(last.get("databaseId"))
    if fid is None or lid is None:
        return None
    if lid == fid:
        return fid
    typename = _dict(last.get("author")).get("__typename")
    return fid if isinstance(typename, str) and typename != "Bot" else None


def reply_body(finding: dict, sha: str, secrets: list[str]) -> str:
    """One short reply for a thread whose finding is still open at `sha`.

    Redact first (so a secret crossing the cut cannot leak a prefix), then cut, then escape, and
    finally keep the whole body under MAX_REPLY_CHARS without splitting an `&...;` entity.
    """
    head = f"Still open at `{sha[:7]}`"
    evidence = redact((finding.get("evidence") or "").strip(), secrets)
    evidence = _esc(evidence.strip()[:MAX_REPLY_EVIDENCE_CHARS])
    body = f"{head}: {evidence}" if evidence else f"{head}."
    if len(body) >= MAX_REPLY_CHARS:
        body = body[: MAX_REPLY_CHARS - 1]
        amp = body.rfind("&")
        if amp != -1 and ";" not in body[amp:]:
            body = body[:amp]
    return body
