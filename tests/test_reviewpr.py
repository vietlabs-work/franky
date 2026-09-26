"""Finding status (`review-pr --thread` re-reviews): parsing, blocking, and the review body."""

from franky.reviewpr import build_review_findings, render_review_body, review_event


def _shaped(*findings, threaded=True):
    return build_review_findings(
        {"summary": "s", "findings": list(findings), "checks": []}, threaded=threaded
    )


def test_status_defaults_to_new_and_invalid_values_fall_back():
    shaped = _shaped(
        {"title": "a"},
        {"title": "b", "status": "open"},
        {"title": "c", "status": "resolved"},
        {"title": "d", "status": "wontfix"},
    )
    assert [f["status"] for f in shaped["findings"]] == ["new", "open", "resolved", "new"]


def test_resolved_blocking_finding_does_not_request_changes():
    shaped = _shaped({"title": "fixed", "severity": "blocking", "status": "resolved"})
    assert shaped["has_blocking"] is False and review_event(shaped) == "COMMENT"
    shaped = _shaped({"title": "still", "severity": "blocking", "status": "open"})
    assert shaped["has_blocking"] is True and review_event(shaped) == "REQUEST_CHANGES"


def test_review_body_tags_status_except_new():
    body = render_review_body(
        _shaped(
            {"title": "race", "severity": "blocking", "status": "open", "file": "a.py", "line": 3},
            {"title": "typo", "severity": "nit"},
        )
    )
    assert "- [blocking][open] race (a.py:3)" in body
    assert "- [nit] typo" in body and "[new]" not in body


def test_non_thread_review_ignores_agent_status():
    """Without --thread an agent cannot suppress REQUEST_CHANGES or add tags via status."""
    shaped = _shaped(
        {"title": "bug", "severity": "blocking", "status": "resolved"},
        {"title": "old", "severity": "nit", "status": "open"},
        threaded=False,
    )
    assert [f["status"] for f in shaped["findings"]] == ["new", "new"]
    assert review_event(shaped) == "REQUEST_CHANGES"
    body = render_review_body(shaped)
    assert "[resolved]" not in body and "[open]" not in body
    assert (
        build_review_findings({"findings": [{"title": "x", "status": "open"}]})["findings"][0][
            "status"
        ]
        == "new"
    )  # the default is non-threaded
