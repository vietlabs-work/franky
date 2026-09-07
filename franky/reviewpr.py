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
publishes that result. `review_event` hard-caps host publication to COMMENT or REQUEST_CHANGES.
The container is autonomous, so GitHub token permissions remain the hard external-write boundary.
"""

from __future__ import annotations

from .sentinel import scan_sentinel_json

_VALID_SEVERITIES = frozenset({"blocking", "normal", "nit"})
_VALID_OUTCOMES = frozenset({"pass", "fail", "skipped"})


def parse_review_findings(output: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced review JSON from the engine transcript, or None.

    Thin wrapper over `sentinel.scan_sentinel_json(output, "REVIEW", nonce)` - see that module
    for the scanning details shared with `plan`/`job diagnose`. Never raises.
    """
    return scan_sentinel_json(output, "REVIEW", nonce)


def _shape_finding(raw: object) -> dict | None:
    """Normalize one raw finding into {title, body, severity, file, line}, or None to drop it.

    A finding must be a dict with a non-empty title; anything else is malformed and dropped (the
    engine is autonomous, so we never trust the shape). An unrecognized severity defaults to
    "normal" rather than being dropped, so a slightly-off label doesn't silently lose a finding.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    body = raw.get("body")
    severity = raw.get("severity") if raw.get("severity") in _VALID_SEVERITIES else "normal"
    file_ = raw.get("file")
    line = raw.get("line")
    return {
        "title": title.strip(),
        "body": body.strip() if isinstance(body, str) else "",
        "severity": severity,
        "file": file_.strip() if isinstance(file_, str) and file_.strip() else None,
        "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
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
        "detail": detail.strip() if isinstance(detail, str) else "",
    }


def build_review_findings(parsed: dict) -> dict:
    """Normalize the parsed review payload into {summary, findings, checks, has_blocking}.

    `has_blocking` is derived (never trusted from the agent directly) so `review_event` has a
    single, grounded signal for whether REQUEST_CHANGES is warranted.
    """
    raw_findings = parsed.get("findings")
    findings: list[dict] = []
    if isinstance(raw_findings, list):
        for raw in raw_findings:
            shaped = _shape_finding(raw)
            if shaped is not None:
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
        "summary": summary.strip() if isinstance(summary, str) else "",
        "findings": findings,
        "checks": checks,
        "has_blocking": any(f["severity"] == "blocking" for f in findings),
    }


def render_review_body(shaped: dict) -> str:
    """Render the GitHub review body from shaped findings - grounded, never fabricated text."""
    lines = [shaped["summary"] or "Franky reviewed this pull request."]
    if shaped["checks"]:
        lines.append("\n**Checks run:**")
        for c in shaped["checks"]:
            detail = f" - {c['detail']}" if c["detail"] else ""
            lines.append(f"- {c['name']}: {c['outcome']}{detail}")
    if shaped["findings"]:
        lines.append("\n**Findings:**")
        for f in shaped["findings"]:
            loc = f" ({f['file']}:{f['line']})" if f["file"] else ""
            lines.append(f"- [{f['severity']}] {f['title']}{loc}\n  {f['body']}")
    else:
        lines.append("\nNo findings.")
    return "\n".join(lines)


def review_event(shaped: dict) -> str:
    """COMMENT or REQUEST_CHANGES - APPROVE is never a reachable value from this function."""
    return "REQUEST_CHANGES" if shaped["has_blocking"] else "COMMENT"
