"""Finding status (`review-pr --thread` re-reviews): parsing, blocking, and the review body."""

import json

from franky.reviewpr import (
    MAX_BODY_CHARS,
    MAX_INLINE,
    anchor_ok,
    build_body_only_payload,
    build_review_findings,
    build_review_payload,
    commentable_lines,
    match_resolved_threads,
    parse_review_findings,
    render_review_body,
    review_event,
)


def _shaped(*findings, threaded=True):
    # A finding needs a content field to be well formed; default the legacy body.
    findings = [
        {"body": "why", **f} if isinstance(f, dict) and "title" in f else f for f in findings
    ]
    return build_review_findings(
        {"summary": "s", "findings": findings, "checks": []}, threaded=threaded
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
    assert "**Nit: typo**" in body and "[new]" not in body


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
        build_review_findings({"findings": [{"title": "x", "body": "b", "status": "open"}]})[
            "findings"
        ][0]["status"]
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
    assert "Off diff" not in p["body"]  # a nit is inline or nothing
    assert "**Major: No line**\n\nwhy" in p["body"]
    assert "Persist tax id" not in p["body"]
    assert p["body"].startswith("Review of sha1: 2 findings (1 blocking). sum\n\n") and p[
        "body"
    ].endswith("<sub>Automated review by Franky</sub>")


def test_single_line_comment_has_no_start_fields():
    p = build_review_payload(_shape(_f()), commentable_lines(FILES), "s", [])
    assert "start_line" not in p["comments"][0] and "start_side" not in p["comments"][0]
    assert p["comments"][0]["body"].startswith("**Major: t**")


def test_empty_summary_default_and_no_findings():
    p = build_review_payload(build_review_findings({}), {}, "s", [])
    assert (
        p["body"] == "Review of s: no findings above nit.\n\n<sub>Automated review by Franky</sub>"
    )
    assert p["comments"] == [] and p["event"] == "COMMENT"


def test_inline_cap_and_body_overflow():
    c = {"a.py": [(1, 100)]}
    shaped = _shape(*[_f(f"t{i}", line=i + 1) for i in range(MAX_INLINE + 9)])
    p = build_review_payload(shaped, c, "s", [])
    assert len(p["comments"]) == MAX_INLINE
    assert "Findings with no line (9)" in p["body"]  # all 9 overflow, none dropped silently
    assert all(f"**Major: t{i}**" in p["body"] for i in range(MAX_INLINE, MAX_INLINE + 9))


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
    assert (
        "<summary>Since the last review (2)</summary>\n\n- Still open: still (a.py:11)\n- Fixed: done (a.py:11)\n\n</details>"
        in p["body"]
    )
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
    assert "**Major: one** (a.py:11)\n\nwhy" in p["body"] and "two" not in p["body"]


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


def test_event_is_never_approve_by_default():
    for sev in ("blocking", "normal", "nit", "bogus"):
        shaped = _shape(_f(severity=sev))
        p = build_review_payload(shaped, commentable_lines(FILES), "s", [])
        assert p["event"] in ("COMMENT", "REQUEST_CHANGES")
        assert build_body_only_payload(shaped, "s", [])["event"] != "APPROVE"
    assert review_event(_shape()) == "COMMENT"  # no findings, flag off


def test_approve_needs_the_flag_and_no_open_major():
    assert review_event(_shape(), allow_approve=True) == "APPROVE"
    resolved = _shaped({"title": "fixed", "severity": "normal", "status": "resolved"})
    assert review_event(resolved, allow_approve=True) == "APPROVE"
    assert review_event(resolved) == "COMMENT"
    nit = _shaped({"title": "n", "severity": "nit", "status": "open"})
    assert review_event(nit, allow_approve=True) == "APPROVE"
    major = _shaped({"title": "m", "severity": "normal", "status": "open"})
    assert review_event(major, allow_approve=True) == "COMMENT"
    new_major = _shaped({"title": "m", "severity": "normal"})
    assert review_event(new_major, allow_approve=True) == "COMMENT"
    block = _shaped({"title": "b", "severity": "blocking"})
    assert review_event(block, allow_approve=True) == "REQUEST_CHANGES"


def test_failed_check_blocks_approve():
    shaped = build_review_findings(
        {"summary": "s", "findings": [], "checks": [{"name": "t", "outcome": "fail"}]}
    )
    assert review_event(shaped, allow_approve=True) == "COMMENT"
    ok = build_review_findings(
        {"summary": "s", "findings": [], "checks": [{"name": "t", "outcome": "pass"}]}
    )
    assert review_event(ok, allow_approve=True) == "APPROVE"


def test_prior_nits_never_listed_in_posted_bodies():
    shaped = _shaped(
        {"title": "open nit", "severity": "nit", "status": "open"},
        {"title": "fixed nit", "severity": "nit", "status": "resolved"},
        {"title": "fixed major", "severity": "normal", "status": "resolved"},
    )
    for p in (
        build_review_payload(shaped, {}, "s", [], allow_approve=True),
        build_body_only_payload(shaped, "s", [], allow_approve=True),
    ):
        assert "open nit" not in p["body"] and "fixed nit" not in p["body"]
        assert "- Fixed: fixed major" in p["body"]


def test_dropped_malformed_finding_blocks_approve():
    shaped = _shaped({"title": "n", "severity": "nit"}, {"body": "no title"})
    assert shaped["dropped_malformed"] == 1
    assert review_event(shaped, allow_approve=True) == "COMMENT"
    p = build_review_payload(shaped, {}, "s", [], allow_approve=True)
    assert p["event"] == "COMMENT"


def test_nits_never_reach_the_body():
    shaped = _shape(_f("big", severity="normal", line=99), _f("tiny", severity="nit", line=99))
    for p in (
        build_review_payload(shaped, commentable_lines(FILES), "s", []),
        build_body_only_payload(shaped, "s", []),
    ):
        assert "big" in p["body"] and "tiny" not in p["body"]
    only_nit = _shape(_f("tiny", severity="nit", line=99))
    body = build_review_payload(only_nit, commentable_lines(FILES), "s", [])["body"]
    assert body.startswith("Review of s: no findings above nit. sum") and "tiny" not in body


def test_nit_never_displaces_a_major_from_inline():
    nits = [_f(f"nit{i}", severity="nit", line=11) for i in range(MAX_INLINE)]
    major = _f("major", severity="normal", line=12)
    p = build_review_payload(_shape(*nits, major), commentable_lines(FILES), "s", [])
    assert len(p["comments"]) == MAX_INLINE
    assert p["comments"][0]["body"].startswith("**Major: major**")
    assert "nit7" not in json.dumps(p)  # cut nit is dropped, not moved to the body


def _node(id_, title="t", label="Major", path="a.py", resolved=False, typename="Bot"):
    return {
        "id": id_,
        "isResolved": resolved,
        "path": path,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "franky-bot", "__typename": typename},
                    "body": f"**{label}: {title}**\n\nwhy",
                }
            ]
        },
    }


def _resolved(*findings):
    return _shaped(*({"status": "resolved", "file": "a.py", "line": 3, **f} for f in findings))


def test_match_resolved_threads_matches_only_unambiguous_own_threads():
    shaped = _resolved({"title": "t", "severity": "normal"})
    assert match_resolved_threads(shaped, [_node("T1")], "Franky-Bot") == ["T1"]
    for bad in (
        _node("T1", typename="User"),
        _node("T1", resolved=True),
        _node("T1", path="b.py"),
        _node("T1", title="other"),
    ):
        assert match_resolved_threads(shaped, [bad], "franky-bot") == []
    assert (
        match_resolved_threads(shaped, [_node("T1"), _node("T2")], "franky-bot") == []
    )  # ambiguous
    assert match_resolved_threads(
        shaped, [_node("T1"), _node("T2", resolved=True)], "franky-bot"
    ) == ["T1"]


def test_match_resolved_threads_needs_the_posting_login():
    shaped = _resolved({"title": "t", "severity": "normal"})
    assert match_resolved_threads(shaped, [_node("T1")], None) == []
    assert match_resolved_threads(shaped, [_node("T1")], "other-bot") == []
    assert match_resolved_threads(shaped, [_node("T1")], "FRANKY-BOT") == ["T1"]


def test_match_resolved_threads_accepts_any_severity_label():
    shaped = _resolved({"title": "t", "severity": "normal"})
    assert match_resolved_threads(shaped, [_node("T1", label="Blocking")], "franky-bot") == ["T1"]
    assert match_resolved_threads(shaped, [_node("T1", label="Bogus")], "franky-bot") == []


def test_match_resolved_threads_open_twin_vetoes_the_title():
    shaped = _shaped(
        {"title": "t", "severity": "normal", "status": "resolved", "file": "a.py"},
        {"title": "t", "severity": "nit", "status": "open", "file": "a.py"},
    )
    assert match_resolved_threads(shaped, [_node("T1")], "franky-bot") == []


def test_match_resolved_threads_ignores_open_and_new_findings():
    shaped = _shaped(
        {"title": "t", "severity": "normal", "status": "open", "file": "a.py"},
        {"title": "t", "severity": "normal", "status": "new", "file": "a.py"},
    )
    assert match_resolved_threads(shaped, [_node("T1")], "Franky-Bot") == []


def test_match_resolved_threads_dedupes_ids():
    shaped = _resolved({"title": "t", "severity": "normal"}, {"title": "t", "severity": "normal"})
    assert match_resolved_threads(shaped, [_node("T1")], "Franky-Bot") == ["T1"]


def test_empty_suggestion_renders_a_deletion_block():
    p = build_review_payload(_shape(_f(suggestion="")), commentable_lines(FILES), "s", [])
    assert p["comments"][0]["body"].endswith("why\n\n```suggestion\n```")


def test_host_caps_text_length():
    from franky.reviewpr import MAX_SUMMARY_CHARS, MAX_TITLE_CHARS

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
    body = build_body_only_payload(shaped, "s", [])["body"]
    assert all(f"**Major: t{i}**" in body for i in range(MAX_INLINE)) and "more" not in body


def test_question_severity_is_kept_capped_and_never_blocks():
    shaped = _shape(*[_f(f"q{i}", severity="question") for i in range(4)])
    assert [f["title"] for f in shaped["findings"]] == ["q0", "q1"]
    assert shaped["has_blocking"] is False
    p = build_review_payload(shaped, commentable_lines(FILES), "s", [])
    assert p["comments"][0]["body"].startswith("**Question: q0**")
    assert p["event"] == "COMMENT"


# --- evidence / impact / fix, verified, collapsed body, round link ---------------------------

REVIEW_URL = "https://github.com/me/repo/pull/7#pullrequestreview-123"


def _ev(title="t", **kw):
    f = {
        "title": title,
        "evidence": "a.py:11 x=None -> NPE",
        "impact": "checkout 500s",
        "fix": "guard x",
        "file": "a.py",
        "line": 11,
    }
    f.update(kw)
    return f


def _inline(f):
    return build_review_payload(_shaped(f), commentable_lines(FILES), "s", [])["comments"][0][
        "body"
    ]


def test_inline_comment_renders_three_fields_after_the_byte_identical_heading():
    body = _inline(_ev("Guard x", severity="blocking", suggestion="y = 1"))
    assert body == (
        "**Blocking: Guard x**\n\n**Evidence:** a.py:11 x=None -&gt; NPE\n\n"
        "**Why it matters:** checkout 500s\n\n**Suggestion:** guard x\n\n"
        "```suggestion\ny = 1\n```"
    )


def test_inline_comment_omits_a_missing_field():
    body = _inline(_ev(impact=""))
    assert "Why it matters" not in body and body.count("\n\n") == 2
    assert body.startswith("**Major: t**\n\n**Evidence:**") and body.endswith(
        "**Suggestion:** guard x"
    )


def test_legacy_body_renders_as_before_and_new_fields_win():
    assert (
        _inline({"title": "t", "body": "why", "file": "a.py", "line": 11}) == "**Major: t**\n\nwhy"
    )
    assert "why" not in _inline(_ev(body="why"))


def test_finding_without_any_content_field_is_malformed_and_blocks_approve():
    shaped = build_review_findings(
        {"summary": "s", "findings": [{"title": "bare"}, {"title": "x", "fix": "  "}]}
    )
    assert shaped["findings"] == [] and shaped["dropped_malformed"] == 2
    assert review_event(shaped, allow_approve=True) == "COMMENT"
    assert build_review_findings({"findings": [{"title": "t", "fix": "do it"}]})["findings"]


def test_field_caps():
    from franky.reviewpr import MAX_FIELD_CHARS

    f = build_review_findings({"findings": [_ev(evidence="e" * 5000, fix="f" * 5000)]})["findings"][
        0
    ]
    assert len(f["evidence"]) == MAX_FIELD_CHARS == len(f["fix"])


def test_heading_still_matches_resolve_threads():
    shaped = _shaped(_ev("Guard x", status="resolved"))
    comment = _inline(_ev("Guard x"))
    node = {
        "id": "T1",
        "isResolved": False,
        "path": "a.py",
        "comments": {"nodes": [{"author": {"login": "bot", "__typename": "Bot"}, "body": comment}]},
    }
    assert match_resolved_threads(shaped, [node], "bot") == ["T1"]


def test_body_headline_counts_and_collapsed_blocks_only_when_non_empty():
    quiet = build_review_payload(_shaped(_ev(severity="nit")), {}, "abcdef1234", [])["body"]
    assert quiet == (
        "Review of abcdef1: no findings above nit. s\n\n<sub>Automated review by Franky</sub>"
    )
    shaped = _shaped(_ev("a", severity="blocking", file=None, line=None), _ev("b", line=11))
    body = build_review_payload(shaped, commentable_lines(FILES), "abcdef1234", [])["body"]
    assert body.startswith("Review of abcdef1: 2 findings (1 blocking). s\n\n")
    assert body.count("<details>") == 1 and "Since the last" not in body
    assert (
        "<details><summary>Findings with no line (1)</summary>\n\n**Blocking: a**\n\n"
        "**Evidence:** a.py:11 x=None -&gt; NPE\n\n**Why it matters:** checkout 500s\n\n"
        "**Suggestion:** guard x\n\n</details>"
    ) in body
    assert body.endswith("</details>\n\n<sub>Automated review by Franky</sub>")


def test_since_last_review_uses_fixed_and_shows_plus_n_more():
    shaped = _shaped(*[_ev(f"p{i}", status="open") for i in range(23)])
    body = render_review_body(shaped, "abcdef1")
    assert "Since the last review (23)" in body and body.count("- Still open:") == 20
    assert "- +3 more" in body
    assert "- Fixed: gone (a.py:11)" in render_review_body(_shaped(_ev("gone", status="resolved")))


def test_verified_normalised_capped_and_rendered():
    parsed = {
        "summary": "s",
        "findings": [],
        "verified": [
            {"claim": "c" * 400, "evidence": "a.py:3 " + "e" * 400, "status": "confirmed"},
            {"claim": "no cite", "evidence": "trust me", "status": "confirmed"},
            {"claim": "bad status", "evidence": "a.py:1", "status": "maybe"},
            "junk",
            {
                "claim": "flag off is identical",
                "evidence": "b.py:9 branch differs",
                "status": "contradicted",
            },
            *[{"claim": f"k{i}", "evidence": "x.py:1", "status": "confirmed"} for i in range(10)],
        ],
    }
    shaped = build_review_findings(parsed)
    assert len(shaped["verified"]) == 8 and shaped["dropped_malformed"] == 0
    assert (
        len(shaped["verified"][0]["claim"]) == 150 and len(shaped["verified"][0]["evidence"]) == 200
    )
    body = render_review_body(shaped)
    assert "<summary>What I verified (8)</summary>\n\n- **cccc" in body
    assert "- **flag off is identical** -> b.py:9 branch differs (contradicted)" in body
    assert review_event(shaped, allow_approve=True) == "APPROVE"  # verified never blocks
    assert build_review_findings({"findings": [], "verified": "x"})["verified"] == []


def test_follows_line_only_for_a_github_review_url():
    shaped = _shaped(_ev())
    body = build_review_payload(
        shaped, commentable_lines(FILES), "abcdef1", [], prior_url=REVIEW_URL
    )["body"]
    assert body.split("\n\n")[1] == f"Follows [the previous review]({REVIEW_URL})."
    for bad in (None, "", "https://evil.example/x", "javascript:alert(1)"):
        assert "Follows" not in render_review_body(shaped, "abcdef1", bad)


def test_approve_marker_is_appended_after_the_footer():
    from franky import cli

    # cli appends the marker to the built body; the body must therefore end with the footer.
    p = build_review_payload(_shaped(), {}, "s", [], allow_approve=True)
    assert p["event"] == "APPROVE" and p["body"].endswith("<sub>Automated review by Franky</sub>")
    assert cli.APPROVE_MARKER


# --- hardening: non-str types, size budget, HTML escaping ------------------------------------


def test_non_str_enum_fields_never_raise():
    for bad in ([], {}, 1, None, ["confirmed"]):
        shaped = build_review_findings(
            {
                "findings": [
                    {"title": "t", "body": "b", "severity": bad, "status": bad},
                    {"title": "u", "evidence": bad, "impact": bad, "fix": bad, "body": "b"},
                ],
                "verified": [{"claim": "c", "evidence": "a.py:1", "status": bad}],
                "checks": [{"name": "n", "outcome": bad}],
            },
            threaded=True,
        )
        assert shaped["verified"] == []
        assert [f["severity"] for f in shaped["findings"]] == ["normal", "normal"]
        assert shaped["checks"][0]["outcome"] == "skipped"
        render_review_body(shaped)


def test_body_budget_keeps_blocks_balanced_footer_and_marker_last():
    from franky.reviewpr import MAX_FIELD_CHARS, REVIEW_BODY_BUDGET

    big = "&<>" * (MAX_FIELD_CHARS // 3)
    findings = [
        {"title": "t" * 200, "evidence": big, "impact": big, "fix": big, "file": None}
        for _ in range(20)
    ]
    verified = [{"claim": "c" * 150, "evidence": "a.py:1 " + "&" * 190, "status": "confirmed"}] * 8
    shaped = build_review_findings({"summary": "s", "findings": findings, "verified": verified})
    for p in (
        build_review_payload(shaped, {}, "abcdef1", [], allow_approve=False),
        build_body_only_payload(shaped, "abcdef1", []),
    ):
        body = p["body"]
        assert len(body) < REVIEW_BODY_BUDGET
        assert body.count("<details>") == body.count("</details>") >= 2
        assert "more" in body and body.endswith("<sub>Automated review by Franky</sub>")
    assert len(render_review_body(shaped, "abcdef1")) < REVIEW_BODY_BUDGET
    marked = build_review_payload(build_review_findings({"findings": []}), {}, "s", [])["body"]
    assert len(marked + "\n\n<!-- franky-review:" + "0" * 32 + " -->") < REVIEW_BODY_BUDGET


def test_model_html_stays_literal_in_body_and_comment():
    evil = "a.py:1\n\n</details>\n\nVISIBLE <!-- franky-review:deadbeef --> & <b>"
    f = _ev("T <x> & y", evidence=evil, impact=evil, fix=evil)
    shaped = _shaped(f, _ev("u", file=None, line=None, evidence=evil))
    shaped["summary"] = "</details> sum"
    shaped["checks"] = [{"name": "<n>", "outcome": "fail", "detail": "</details>"}]
    shaped["verified"] = [{"claim": "<c>", "evidence": "a.py:1 </details>", "status": "confirmed"}]
    p = build_review_payload(shaped, commentable_lines(FILES), "abcdef1", [])
    for text in (p["body"], p["comments"][0]["body"]):
        assert "<!--" not in text and "<b>" not in text and "<x>" not in text
    # Only renderer-owned tags remain, and they stay balanced.
    assert p["body"].count("</details>") == p["body"].count("<details>")
    assert p["comments"][0]["body"].startswith("**Major: T &lt;x&gt; &amp; y**\n\n")
    assert "&lt;/details&gt;" in p["body"] and "&lt;!-- franky-review:deadbeef --&gt;" in p["body"]


def test_resolve_matches_title_with_html_chars():
    title = "Guard <T> & friends"
    comment = _inline(_ev(title))
    assert comment.startswith("**Major: Guard &lt;T&gt; &amp; friends**")
    node = {
        "id": "T9",
        "isResolved": False,
        "path": "a.py",
        "comments": {"nodes": [{"author": {"login": "bot", "__typename": "Bot"}, "body": comment}]},
    }
    shaped = _shaped(_ev(title, status="resolved"))
    assert match_resolved_threads(shaped, [node], "bot") == ["T9"]
    node["comments"]["nodes"][0]["body"] = f"**Major: {title}**\n\nold raw comment"
    assert match_resolved_threads(shaped, [node], "bot") == ["T9"]  # pre-escaping comments


def test_suggestion_cannot_close_its_fence_and_is_not_escaped():
    sug = "a = '<T>'\n```\nrm -rf /\n```"
    body = _inline(_ev(suggestion=sug))
    fence = "````"
    assert body.endswith(f"{fence}suggestion\n{sug}\n{fence}") and "<T>" in body
