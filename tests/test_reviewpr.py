"""Finding status (`review-pr --thread` re-reviews): parsing, blocking, and the review body."""

import json

from franky.reviewpr import (
    MAX_BODY_FINDINGS,
    MAX_INLINE,
    anchor_ok,
    build_body_only_payload,
    build_review_findings,
    build_review_payload,
    commentable_lines,
    parse_review_findings,
    render_review_body,
    review_event,
)


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
    assert "- Still open: race" in body
    assert "- **Nit:** typo" in body and "[new]" not in body


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


# --- inline review payload ---------------------------------------------------------------------

PATCH = (
    "@@ -1,4 +10,5 @@ def f():\n"
    " ctx10\n"
    "-gone\n"
    "+new11\n"
    "+new12\n"
    " ctx13\n"
    "\\ No newline at end of file\n"
    "@@ -30,2 +40,3 @@\n"
    " ctx40\n"
    "+new41\n"
    " ctx42"
)
FILES = [
    {"filename": "a.py", "status": "modified", "patch": PATCH},
    {"filename": "bin.png", "status": "added"},
    {"filename": "old.py", "status": "removed", "patch": "@@ -1 +0,0 @@\n-x"},
]


def _shape(*findings, checks=(), threaded=False):
    return build_review_findings(
        {"summary": "sum", "findings": list(findings), "checks": list(checks)}, threaded=threaded
    )


def _f(title="t", file="a.py", line=11, **kw):
    return {"title": title, "body": "why", "file": file, "line": line, **kw}


def test_commentable_lines_hunks():
    c = commentable_lines(FILES)
    assert c == {"a.py": [(10, 13), (40, 42)]}  # no patch / removed -> no entry


def test_commentable_lines_single_hunk_and_deletion_only():
    assert commentable_lines(
        [{"filename": "b", "status": "modified", "patch": "@@ -1,2 +1,1 @@\n-a\n b"}]
    ) == {"b": [(1, 1)]}
    assert commentable_lines([{"filename": "b", "patch": "@@ -1 +0,0 @@\n-a"}]) == {"b": [(0, -1)]}
    assert anchor_ok({"file": "b", "line": 1, "start_line": None}, {"b": [(0, -1)]}) is False


def test_anchor_ok():
    c = commentable_lines(FILES)
    ok = lambda **kw: anchor_ok({"file": "a.py", "start_line": None, **kw}, c)  # noqa: E731
    assert ok(line=11)  # added line
    assert ok(line=13)  # context line
    assert not ok(line=20)  # outside any hunk
    assert not ok(line=9)
    assert not ok(line=None)
    assert not anchor_ok({"file": "nope.py", "line": 11, "start_line": None}, c)
    assert ok(line=12, start_line=10)
    assert not ok(line=11, start_line=12)  # reversed
    assert not ok(line=11, start_line=11)
    assert not ok(line=41, start_line=12)  # crosses hunks
    assert not ok(line=12, start_line=9)  # start outside


def test_shape_finding_start_line_suggestion_and_line_validation():
    f = _shape(
        _f(line=5, start_line=3, suggestion="x = 1"),
        _f(line=5, start_line=5, suggestion=""),
        _f(line=0, start_line=1),
        _f(line=True),
    )["findings"]
    assert (f[0]["start_line"], f[0]["suggestion"]) == (3, "x = 1")
    assert (f[1]["start_line"], f[1]["suggestion"]) == (None, "")
    assert (f[2]["line"], f[2]["start_line"]) == (None, None)
    assert f[3]["line"] is None


def test_payload_splits_inline_and_body():
    c = commentable_lines(FILES)
    shaped = _shape(
        _f("Persist tax id", severity="blocking", line=12, start_line=11, suggestion="y = 2"),
        _f("Off diff", line=99, severity="nit"),
        _f("No line", file=None, line=None),
    )
    p = build_review_payload(shaped, c, "sha1", [])
    assert p["event"] == "REQUEST_CHANGES" and p["commit_id"] == "sha1"
    assert p["comments"] == [
        {
            "path": "a.py",
            "line": 12,
            "side": "RIGHT",
            "start_line": 11,
            "start_side": "RIGHT",
            "body": "**Blocking: Persist tax id**\n\nwhy\n\n```suggestion\ny = 2\n```",
        }
    ]
    assert "- **Nit:** Off diff (a.py:99) - why" in p["body"]
    assert "- **Major:** No line - why" in p["body"]
    assert "Persist tax id" not in p["body"]
    assert p["body"].startswith("sum\n\n") and p["body"].endswith(
        "<sub>Automated review by Franky</sub>"
    )


def test_single_line_comment_has_no_start_fields():
    p = build_review_payload(_shape(_f()), commentable_lines(FILES), "s", [])
    assert "start_line" not in p["comments"][0] and "start_side" not in p["comments"][0]
    assert p["comments"][0]["body"].startswith("**Major: t**")


def test_empty_summary_default_and_no_findings():
    p = build_review_payload(build_review_findings({}), {}, "s", [])
    assert (
        p["body"] == "Franky reviewed this pull request.\n\n<sub>Automated review by Franky</sub>"
    )
    assert p["comments"] == [] and p["event"] == "COMMENT"


def test_inline_cap_and_body_overflow():
    c = {"a.py": [(1, 100)]}
    shaped = _shape(*[_f(f"t{i}", line=i + 1) for i in range(MAX_INLINE + 9)])
    p = build_review_payload(shaped, c, "s", [])
    assert len(p["comments"]) == MAX_INLINE
    rows = [r for r in p["body"].split("\n") if r.startswith("- ")]
    assert len(rows) == MAX_BODY_FINDINGS + 1
    assert rows[-1] == "- +4 more"  # 9 overflow - 5 shown


def test_suggestion_with_backticks_gets_longer_fence():
    p = build_review_payload(
        _shape(_f(suggestion="a = ```x```")), commentable_lines(FILES), "s", []
    )
    assert p["comments"][0]["body"].endswith("````suggestion\na = ```x```\n````")


def test_threaded_statuses_go_to_body_only():
    shaped = _shape(
        _f("fresh"),
        _f("still", status="open"),
        _f("done", status="resolved", severity="blocking"),
        threaded=True,
    )
    p = build_review_payload(shaped, commentable_lines(FILES), "s", [])
    assert [c["body"].split("\n")[0] for c in p["comments"]] == ["**Major: fresh**"]
    assert "Since the last review:\n- Still open: still\n- Resolved: done" in p["body"]
    assert p["event"] == "COMMENT"


def test_only_failed_checks_in_body():
    checks = [
        {"name": "pytest", "outcome": "pass", "detail": "12 passed"},
        {"name": "mypy", "outcome": "skipped", "detail": "n/a"},
        {"name": "lint", "outcome": "fail", "detail": "3 errors"},
    ]
    shaped = _shape(checks=checks)
    for body in (
        build_review_payload(shaped, {}, "s", [])["body"],
        build_body_only_payload(shaped, "s", [])["body"],
        render_review_body(shaped),
    ):
        assert "- Failed check: lint - 3 errors" in body
        assert "pytest" not in body and "mypy" not in body


def test_body_only_payload_has_no_comments_and_lists_everything():
    shaped = _shape(_f("one"), _f("two", severity="nit"))
    p = build_body_only_payload(shaped, "sha", [])
    assert p["comments"] == [] and p["commit_id"] == "sha"
    assert "- **Major:** one (a.py:11) - why" in p["body"] and "- **Nit:** two" in p["body"]


def test_secrets_redacted_everywhere_including_unescaped_text():
    secret = "ghp_abcSECRET"
    parsed = parse_review_findings(
        "FRANKY_REVIEW_n_BEGIN"
        + json.dumps(
            {
                "summary": f"s {secret}",
                "findings": [
                    {
                        "title": "t ghp_\u0061bcSECRET",  # only matches after JSON decoding
                        "body": f"b {secret}",
                        "file": "a.py",
                        "line": 11,
                        "suggestion": f"k = '{secret}'",
                    },
                    {"title": f"o {secret}", "body": "x", "file": "z.py", "line": 1},
                ],
                "checks": [{"name": "c", "outcome": "fail", "detail": secret}],
            }
        ).replace("ghp_abcSECRET", "ghp_\\u0061bcSECRET")
        + "FRANKY_REVIEW_n_END",
        "n",
    )
    shaped = build_review_findings(parsed)
    assert secret in shaped["findings"][0]["title"]  # decoded form carries the secret
    for p in (
        build_review_payload(shaped, commentable_lines(FILES), "s", [secret]),
        build_body_only_payload(shaped, "s", [secret]),
    ):
        assert secret not in json.dumps(p)
        assert "[REDACTED" in json.dumps(p) or "REDACTED" in json.dumps(p)


def test_event_is_never_approve():
    for sev in ("blocking", "normal", "nit", "bogus"):
        p = build_review_payload(_shape(_f(severity=sev)), commentable_lines(FILES), "s", [])
        assert p["event"] in ("COMMENT", "REQUEST_CHANGES")


def test_empty_suggestion_renders_a_deletion_block():
    p = build_review_payload(_shape(_f(suggestion="")), commentable_lines(FILES), "s", [])
    assert p["comments"][0]["body"].endswith("why\n\n```suggestion\n```")


def test_host_caps_text_length():
    from franky.reviewpr import MAX_BODY_CHARS, MAX_SUMMARY_CHARS, MAX_TITLE_CHARS

    shaped = build_review_findings(
        {
            "summary": "s" * 5000,
            "findings": [_f("t" * 900, body="b" * 9000, line=12)],
            "checks": [{"name": "ci", "outcome": "fail", "detail": "d" * 900}],
        }
    )
    p = build_review_payload(shaped, commentable_lines(FILES), "s", [])
    f = shaped["findings"][0]
    assert len(f["title"]) == MAX_TITLE_CHARS and len(f["body"]) == MAX_BODY_CHARS
    assert len(shaped["summary"]) == MAX_SUMMARY_CHARS
    assert len(p["comments"][0]["body"]) < MAX_TITLE_CHARS + MAX_BODY_CHARS + 50
    assert len(p["body"]) < 2000


def test_body_only_fallback_keeps_up_to_eight_findings():
    from franky.reviewpr import build_body_only_payload

    shaped = _shape(*[_f(f"t{i}", line=i + 1) for i in range(MAX_INLINE)])
    rows = [
        r
        for r in build_body_only_payload(shaped, "s", [])["body"].split("\n")
        if r.startswith("- ")
    ]
    assert len(rows) == MAX_INLINE and not any("more" in r for r in rows)


def test_question_severity_is_kept_capped_and_never_blocks():
    shaped = _shape(*[_f(f"q{i}", severity="question") for i in range(4)])
    assert [f["title"] for f in shaped["findings"]] == ["q0", "q1"]
    assert shaped["has_blocking"] is False
    p = build_review_payload(shaped, commentable_lines(FILES), "s", [])
    assert p["comments"][0]["body"].startswith("**Question: q0**")
    assert p["event"] == "COMMENT"
