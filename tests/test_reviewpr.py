"""Finding status (`review-pr --thread` re-reviews): parsing, blocking, and the review body."""

import json

import pytest

from franky.reviewpr import (
    SIGNOFF_FILE,
    MAX_BODY_CHARS,
    MAX_INLINE,
    anchor_ok,
    human_signoff_required,
    build_body_only_payload,
    build_review_findings,
    build_review_payload,
    commentable_lines,
    match_prior_threads,
    match_resolved_threads,
    review_footer,
    reply_body,
    thread_reply_target,
    parse_review_findings,
    verify_accepted,
    render_review_body,
    review_event,
    signoff_match,
    signoff_reason,
    signoff_text,
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
    assert p["body"].startswith("Review of sha1: 2 findings (1 blocking, 1 major). sum\n\n") and p[
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
    question = _shaped({"title": "q", "severity": "question"})
    assert review_event(question, allow_approve=True) == "COMMENT"  # an unknown fact blocks
    answered = _shaped({"title": "q", "severity": "question", "status": "resolved"})
    assert review_event(answered, allow_approve=True) == "APPROVE"


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


def _node(
    id_,
    title="t",
    label="Major",
    path="a.py",
    resolved=False,
    typename="Bot",
    database_id=1,
    last=None,
):
    first = {
        "databaseId": database_id,
        "author": {"login": "franky-bot", "__typename": typename},
        "body": f"**{label}: {title}**\n\nwhy",
    }
    return {
        "id": id_,
        "isResolved": resolved,
        "path": path,
        "comments": {"nodes": [first]},
        "last": {"nodes": [first if last is None else last]},
    }


def _resolved(*findings):
    return _shaped(*({"status": "resolved", "file": "a.py", "line": 3, **f} for f in findings))


def test_match_resolved_threads_matches_only_unambiguous_own_threads():
    shaped = _resolved({"title": "t", "severity": "normal"})
    assert match_resolved_threads(shaped, [_node("T1")], "Franky-Bot") == ["T1"]
    for bad in (
        _node("T1", typename="User"),
        _node("T1", resolved=True),
        _node("T1", title="other"),
    ):
        assert match_resolved_threads(shaped, [bad], "franky-bot") == []
    assert (
        match_resolved_threads(shaped, [_node("T1"), _node("T2")], "franky-bot") == []
    )  # ambiguous
    assert match_resolved_threads(
        shaped, [_node("T1"), _node("T2", resolved=True)], "franky-bot"
    ) == ["T1"]


def test_match_resolved_threads_falls_back_to_a_single_title_match_elsewhere():
    shaped = _resolved({"title": "t", "severity": "normal"})  # finding reported on a.py
    assert match_resolved_threads(shaped, [_node("T1", path="b.py")], "franky-bot") == ["T1"]
    two = [_node("T1", path="b.py"), _node("T2", path="c.py")]
    assert match_resolved_threads(shaped, two, "franky-bot") == []  # ambiguous
    # the thread on the finding's own file wins over a title match on another file
    both = [_node("T1", path="b.py"), _node("T2", path="a.py")]
    assert match_resolved_threads(shaped, both, "franky-bot") == ["T2"]


def test_match_prior_threads_finding_without_a_file_matches_by_title():
    shaped = _shaped({"title": "t", "severity": "normal", "status": "resolved", "file": None})
    assert match_resolved_threads(shaped, [_node("T1")], "franky-bot") == ["T1"]


def _two_findings(first, second):
    a = {"title": "t", "severity": "normal", "line": 3, "file": "a.py"}
    b = {"title": "t", "severity": "normal", "line": 3, "file": "b.py"}
    return _shaped({**a, "status": first}, {**b, "status": second})


@pytest.mark.parametrize("order", [("resolved", "open"), ("open", "resolved")])
def test_match_prior_threads_open_finding_blocks_resolving_a_shared_thread(order):
    shaped = _two_findings(*order)
    nodes = [_node("T1", path="a.py")]  # b.py has no thread: it falls back to A's by title
    assert match_resolved_threads(shaped, nodes, "franky-bot") == []
    [(decision, node, findings, _rf)] = match_prior_threads(shaped, nodes, "franky-bot")
    assert decision == "open" and node["id"] == "T1"
    assert "open" in {f["status"] for f in findings}


def test_match_prior_threads_never_returns_new_findings():
    shaped = _shaped({"title": "t", "severity": "normal", "status": "new", "file": "a.py"})
    assert match_prior_threads(shaped, [_node("T1")], "franky-bot") == []


def test_match_prior_threads_open_finding_gets_open_decision():
    shaped = _shaped({"title": "t", "severity": "normal", "status": "open", "file": "a.py"})
    [(decision, _n, findings, rf)] = match_prior_threads(shaped, [_node("T1")], "franky-bot")
    assert decision == "open" and findings[0]["status"] == "open" and rf is findings[0]


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


def _bot(i):
    return {"databaseId": i, "author": {"login": "x", "__typename": "Bot"}}


def test_thread_reply_target():
    assert thread_reply_target(_node("T", database_id=7)) == 7  # never replied
    assert thread_reply_target(_node("T", database_id=7, last=_bot(8))) is None  # a bot last
    human = {"databaseId": 8, "author": {"login": "me", "__typename": "User"}}
    assert thread_reply_target(_node("T", database_id=7, last=human)) == 7
    for bad in (
        {**_node("T"), "last": None},
        {k: v for k, v in _node("T").items() if k != "last"},
        _node("T", database_id=True),
        _node("T", database_id=0),
        _node("T", database_id=7, last={"author": {"__typename": "User"}}),
        _node("T", database_id=7, last={"databaseId": 0, "author": {"__typename": "User"}}),
    ):
        assert thread_reply_target(bad) is None


def test_reply_body():
    sha = "abcdef1234"
    assert reply_body({"evidence": " a.py:3 still races "}, sha, []) == (
        "Still open at `abcdef1`: a.py:3 still races"
    )
    assert reply_body({"evidence": ""}, sha, []) == "Still open at `abcdef1`."
    assert reply_body({"evidence": "x" * 900}, sha, []) == "Still open at `abcdef1`: " + "x" * 600
    assert "s3cret" not in reply_body({"evidence": "key s3cret"}, sha, ["s3cret"])
    assert "</details>" not in reply_body({"evidence": "</details>"}, sha, [])
    assert len(reply_body({"evidence": "y" * 1000}, sha, [])) < 2000


def test_open_finding_matched_only_by_title_never_replies_or_resolves():
    shaped = _shaped({"title": "t", "severity": "normal", "status": "open", "file": "b.py"})
    nodes = [_node("T1", path="a.py")]
    assert match_resolved_threads(shaped, nodes, "franky-bot") == []
    [(decision, _n, _fs, reply_finding)] = match_prior_threads(shaped, nodes, "franky-bot")
    assert decision == "open" and reply_finding is None


def test_match_prior_threads_survives_malformed_nodes():
    shaped = _shaped({"title": "t", "severity": "normal", "status": "open", "file": "a.py"})
    bad = [
        {"id": "T", "isResolved": False, "comments": [1]},
        {"id": "T", "isResolved": False, "comments": {"nodes": None}},
        {"id": "T", "isResolved": False, "comments": {"nodes": [1]}},
        {"id": "T", "isResolved": False, "comments": {"nodes": [{"author": "x", "body": "b"}]}},
        "junk",
        None,
    ]
    assert match_prior_threads(shaped, bad, "franky-bot") == []
    assert match_prior_threads(shaped, None, "franky-bot") == []
    for node in ({"comments": [1], "last": 3}, {"comments": {"nodes": None}}, {}, None, "x"):
        assert thread_reply_target(node) is None


def test_thread_reply_target_needs_a_typed_last_author():
    for last in (
        {"databaseId": 8},
        {"databaseId": 8, "author": None},
        {"databaseId": 8, "author": "x"},
        {"databaseId": 8, "author": {"login": "me"}},
        {"databaseId": 8, "author": {"__typename": 5}},
    ):
        assert thread_reply_target(_node("T", database_id=7, last=last)) is None


def test_reply_body_stays_short_and_redacts_before_cutting():
    sha = "abcdef1234"
    body = reply_body({"evidence": "&" * 600}, sha, [])
    assert len(body) < 2000 and not body.endswith("&") and not body.endswith("&a")
    assert "a&b" not in reply_body({"evidence": "key a&b"}, sha, ["a&b"])
    secret = "Z" * 20
    crossing = "x" * 590 + secret  # the secret straddles the 600 cut
    assert "Z" not in reply_body({"evidence": crossing}, sha, [secret])


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


def test_prior_finding_left_out_of_a_re_review_blocks_approve():
    shaped = _shape(threaded=True)
    assert review_event(shaped, allow_approve=True) == "APPROVE"
    shaped["omitted_prior"] = [{"title": "Guard the null list", "severity": "normal"}]
    assert review_event(shaped, allow_approve=True) == "COMMENT"
    body = render_review_body(shaped, "a" * 40)
    assert "Not re-reported, still open: Guard the null list" in body
    assert "1 finding (0 blocking, 1 major)" in body
    shaped["omitted_prior"] = [{"title": "Race", "severity": "blocking", "file": "a.py"}]
    assert review_event(shaped) == "REQUEST_CHANGES"
    over = _shape(threaded=True)
    over["prior_overflow"] = True
    assert review_event(over, allow_approve=True) == "COMMENT"


def test_open_question_over_the_cap_still_blocks_approve():
    def q(t, status):
        return {"title": t, "body": "why", "severity": "question", "status": status}

    # Resolved questions do not use the cap, so a third open one is kept and shown.
    shaped = _shape(q("a", "resolved"), q("b", "resolved"), q("c", "new"), threaded=True)
    assert [f["title"] for f in shaped["findings"]] == ["a", "b", "c"]
    assert review_event(shaped, allow_approve=True) == "COMMENT"
    # A third OPEN question is not shown, but it is counted, so it still blocks APPROVE.
    over = _shape(q("a", "open"), q("b", "open"), q("c", "new"), threaded=True)
    assert [f["title"] for f in over["findings"]] == ["a", "b"]
    assert over["dropped_malformed"] == 1
    assert review_event(over, allow_approve=True) == "COMMENT"
    # An explicit open question blocks; resolving every question allows APPROVE.
    assert review_event(_shape(q("a", "open"), threaded=True), allow_approve=True) == "COMMENT"
    done = _shape(q("a", "resolved"), q("b", "resolved"), q("c", "resolved"), threaded=True)
    assert review_event(done, allow_approve=True) == "APPROVE"


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


def test_headline_names_major_and_question_counts_beside_blocking():
    shaped = _shaped(_ev("a"), _ev("b"), _ev("c", severity="question"), _ev("d", severity="nit"))
    body = build_body_only_payload(shaped, "abcdef1234", [])["body"]
    assert body.startswith("Review of abcdef1: 3 findings (0 blocking, 2 major, 1 question). s")


def test_body_headline_counts_and_collapsed_blocks_only_when_non_empty():
    quiet = build_review_payload(_shaped(_ev(severity="nit")), {}, "abcdef1234", [])["body"]
    assert quiet == (
        "Review of abcdef1: no findings above nit. s\n\n<sub>Automated review by Franky</sub>"
    )
    shaped = _shaped(_ev("a", severity="blocking", file=None, line=None), _ev("b", line=11))
    body = build_review_payload(shaped, commentable_lines(FILES), "abcdef1234", [])["body"]
    assert body.startswith("Review of abcdef1: 2 findings (1 blocking, 1 major). s\n\n")
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
    assert len(marked + "\n\n<!-- bot-review:" + "0" * 32 + " -->") < REVIEW_BODY_BUDGET


def test_model_html_stays_literal_in_body_and_comment():
    evil = "a.py:1\n\n</details>\n\nVISIBLE <!-- bot-review:deadbeef --> & <b>"
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
    assert "&lt;/details&gt;" in p["body"] and "&lt;!-- bot-review:deadbeef --&gt;" in p["body"]


def test_review_footer_env(monkeypatch):
    monkeypatch.setenv("FRANKY_REVIEW_FOOTER", "Custom line")
    p = build_review_payload(build_review_findings({}), {}, "s", [])
    assert p["body"].endswith("\n\nCustom line")
    monkeypatch.setenv("FRANKY_REVIEW_FOOTER", "")
    p = build_review_payload(build_review_findings({}), {}, "s", [])
    assert p["body"] == "Review of s: no findings above nit."
    monkeypatch.setenv("FRANKY_REVIEW_FOOTER", "x" * 501)
    with pytest.raises(ValueError):
        review_footer()


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


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("**/refund/**", "a/refund/X.java", True),
        ("**/refund/**", "refund/X.java", True),
        ("refund/", "a/refund/X.java", True),
        ("refund/", "a/refund", False),
        ("*Refund*.java", "x/PartialRefundService.java", True),
        ("/top.txt", "top.txt", True),
        ("/top.txt", "a/top.txt", False),
        ("src/*.py", "src/a/b.py", False),
        ("src/*.py", "src/b.py", True),
        ("src/**/*.py", "src/b.py", True),
        ("src/**/*.py", "src/a/b.py", True),
        ("docs", "docs/x.md", True),
        ("docs", "other/docs/x.md", True),
        ("a?c.txt", "abc.txt", True),
        ("a?c.txt", "a/c.txt", False),
        ("a.b", "aXb", False),
    ],
)
def test_signoff_match_glob_semantics(glob, path, expected):
    assert signoff_match([glob], [path]) is expected


def test_signoff_match_negation_last_match_wins():
    globs = ["**/refund/**", "!**/test/**"]
    assert signoff_match(globs, ["a/refund/X.java"]) is True
    assert signoff_match(globs, ["a/refund/test/XTest.java"]) is False
    assert signoff_match(["!**/test/**", "**/refund/**"], ["a/refund/test/XTest.java"]) is True
    assert signoff_match(["!x"], ["x"]) is False


def _no_read():
    raise AssertionError("the globs must not be read")


def _pf(name, previous=None):
    return {"filename": name, **({"previous_filename": previous} if previous else {})}


def test_human_signoff_floor_branches():
    read = lambda: ["**/pay/**"]  # noqa: E731
    assert human_signoff_required([_pf("a/pay/X.java")], read) is True
    assert human_signoff_required([_pf("a/ok/X.java")], read) is False
    # a rename counts under both names
    assert human_signoff_required([_pf("a/ok/X.java", "a/pay/X.java")], read) is True
    assert human_signoff_required([_pf("a/pay/X.java", "a/ok/X.java")], read) is True
    # absent list: no floor; any other read failure: floor
    assert human_signoff_required([_pf("a/pay/X.java")], lambda: []) is False
    assert human_signoff_required([_pf("a/pay/X.java")], lambda: None) is True
    # unreadable, full-size or malformed file lists fail closed
    assert human_signoff_required(None, _no_read) is True
    assert human_signoff_required([_pf("a")] * 3000, _no_read) is True
    assert human_signoff_required([_pf("a"), "oops"], _no_read) is True
    assert human_signoff_required([{"filename": 3}], _no_read) is True
    assert human_signoff_required([{"filename": "x", "status": "renamed"}], _no_read) is True


def test_human_signoff_floor_skips_the_read_when_it_cannot_matter():
    assert human_signoff_required([], _no_read) is False
    # a PR that edits, renames or deletes the list itself always needs sign-off, unread
    assert human_signoff_required([_pf(SIGNOFF_FILE)], _no_read) is True
    assert human_signoff_required([_pf("x", SIGNOFF_FILE)], _no_read) is True


def test_body_carries_the_signoff_text_only_when_flagged():
    shaped = _shaped()
    assert "Automated approval is off" not in render_review_body(shaped)
    info = {"reason": "match", "path": "a/pay/X.java", "rule": "**/pay/**", "more": 0}
    body = render_review_body({**shaped, "signoff": info})
    assert (
        "**Looks good: no findings above nit.** Automated approval is off for this PR. "
        "A human must approve it, because `a/pay/X.java` matches `**/pay/**` in "
        "`.github/human-signoff-paths`." in body
    )


def test_signoff_text_variants():
    base = "Automated approval is off for this PR. A human must approve it, because "
    m = {"reason": "match", "path": "a/X.java", "rule": "/a/*", "more": 1}
    assert f"{base}`a/X.java` (and 1 more file) matches `/a/*` in" in signoff_text(m)
    assert "(and 3 more files)" in signoff_text({**m, "more": 3})
    changed = {"reason": "list_changed", "path": SIGNOFF_FILE, "rule": None, "more": 0}
    assert signoff_text(changed).endswith(f"{base}it changes `{SIGNOFF_FILE}`.")
    unread = {"reason": "unreadable", "path": None, "rule": None, "more": 0}
    assert signoff_text(unread).endswith(
        f"{base}the sign-off list or the PR file list could not be read."
    )


def test_signoff_text_fences_the_path_verbatim_and_caps_its_length():
    info = {"reason": "match", "path": "a/<b>&`x`.java", "rule": "**", "more": 0}
    text = signoff_text(info)
    assert "``a/<b>&`x`.java``" in text
    edge = signoff_text({**info, "path": "`x"})
    assert "`` `x ``" in edge
    long = signoff_text({**info, "rule": "r" * 70000})
    assert len(long) < 600 and "r" * 197 + "...`" in long


def test_signoff_reason_match_names_first_path_and_deciding_rule():
    read = lambda: ["/a/*", "**/pay/**"]  # noqa: E731
    files = [_pf("z/ok.txt"), _pf("a/pay/X.java"), _pf("a/Y.java"), _pf("b/pay/Z.java")]
    assert signoff_reason(files, read) == {
        "reason": "match",
        "path": "a/pay/X.java",
        "rule": "**/pay/**",
        "more": 2,
    }
    # the rule text is kept as written, leading slash included
    assert signoff_reason([_pf("a/Y.java")], read)["rule"] == "/a/*"


def test_signoff_reason_negated_last_rule_wins():
    read = lambda: ["**/pay/**", "!**/test/**"]  # noqa: E731
    assert signoff_reason([_pf("a/pay/test/XTest.java")], read) is None
    got = signoff_reason([_pf("a/pay/test/XTest.java"), _pf("a/pay/X.java")], read)
    assert got["path"] == "a/pay/X.java" and got["more"] == 0
    # a later positive rule re-matches what a negation cleared
    again = signoff_reason([_pf("a/pay/test/X.java")], lambda: ["!**/test/**", "**/pay/**"])
    assert again["rule"] == "**/pay/**"


def test_signoff_reason_rename_counts_old_name():
    got = signoff_reason([_pf("a/ok/X.java", "a/pay/X.java")], lambda: ["**/pay/**"])
    assert got == {"reason": "match", "path": "a/pay/X.java", "rule": "**/pay/**", "more": 0}


def test_signoff_reason_list_changed_and_unreadable():
    assert signoff_reason([_pf("x"), _pf(SIGNOFF_FILE)], _no_read) == {
        "reason": "list_changed",
        "path": SIGNOFF_FILE,
        "rule": None,
        "more": 0,
    }
    unread = {"reason": "unreadable", "path": None, "rule": None, "more": 0}
    assert signoff_reason(None, _no_read) == unread
    assert signoff_reason([_pf("a")] * 3000, _no_read) == unread
    assert signoff_reason([_pf("a"), "oops"], _no_read) == unread
    assert signoff_reason([{"filename": "x", "status": "renamed"}], _no_read) == unread
    assert signoff_reason([_pf("a/pay/X.java")], lambda: None) == unread


_AGREE_CASES = [
    ([], _no_read),
    (None, _no_read),
    ([_pf("a")] * 3000, _no_read),
    ([_pf(SIGNOFF_FILE)], _no_read),
    ([{"filename": "x", "status": "renamed"}], _no_read),
    ([_pf("a/pay/X.java")], lambda: ["**/pay/**"]),
    ([_pf("a/ok/X.java")], lambda: ["**/pay/**"]),
    ([_pf("a/ok/X.java", "a/pay/X.java")], lambda: ["**/pay/**"]),
    ([_pf("a/pay/test/X.java")], lambda: ["**/pay/**", "!**/test/**"]),
    ([_pf("a/pay/X.java")], lambda: []),
    ([_pf("a/pay/X.java")], lambda: None),
]


@pytest.mark.parametrize("files,read", _AGREE_CASES)
def test_signoff_reason_is_none_iff_signoff_not_required(files, read):
    assert (signoff_reason(files, read) is None) is (not human_signoff_required(files, read))


# --- accepted risk ---------------------------------------------------------------------------

HEAD = "a" * 40
RAISED = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"
PRIOR = [{"title": "Tradeoff.", "file": "a.py", "severity": "normal", "raised_at": RAISED}]


def _accepted(**kw):
    return _f(title="tradeoff", status="accepted", accepted_by="owner-x", **kw)


def _review(
    state="APPROVED",
    login="owner-x",
    sha=HEAD,
    at=LATER,
    kind="User",
    body="",
    assoc="MEMBER",
    id=1,
):
    return {
        "id": id,
        "author_association": assoc,
        "user": {"login": login, "type": kind},
        "state": state,
        "commit_id": sha,
        "submitted_at": at,
        "body": body,
    }


def _verify(reviews, finding=None, prior=PRIOR, author="author-y", **kw):
    shaped = _shaped(finding or _accepted(), **kw)
    verify_accepted(shaped, reviews, HEAD, author, prior)
    return shaped


def test_accepted_by_a_human_approval_on_head_clears_the_finding_and_can_approve():
    shaped = _verify([_review()])
    assert shaped["findings"][0]["status"] == "accepted"
    assert shaped["findings"][0]["accepted_by"] == "owner-x"
    assert shaped["has_blocking"] is False
    assert review_event(shaped) == "COMMENT"
    assert review_event(shaped, allow_approve=True) == "APPROVE"


def test_accepted_blocking_finding_no_longer_blocks_once_verified():
    shaped = _verify([_review()], _accepted(severity="blocking"))
    assert shaped["has_blocking"] is False
    assert review_event(shaped, allow_approve=True) == "APPROVE"
    refused = _verify([], _accepted(severity="blocking"))
    assert refused["has_blocking"] is True
    assert review_event(refused, allow_approve=True) == "REQUEST_CHANGES"


@pytest.mark.parametrize(
    "reviews",
    [
        [_review(sha="b" * 40)],  # approval on an older commit
        [_review(), _review("CHANGES_REQUESTED", at="2026-01-03T00:00:00Z")],  # latest wins
        [_review("DISMISSED", at="2026-01-03T00:00:00Z"), _review()],
        [_review(at="2025-12-31T00:00:00Z")],  # before the finding was raised
        [_review(at=RAISED)],  # not strictly after
        [_review(at="not a date")],
        [_review(at=None)],
        [_review(), _review("CHANGES_REQUESTED", at="garbage", id=2)],  # any bad timestamp
        [_review(assoc="CONTRIBUTOR")],
        [_review(assoc="NONE")],
        [_review(assoc=None)],
        [_review(assoc=5)],
        [_review(assoc=["MEMBER"])],
        [_review(assoc={"MEMBER": 1})],
        [
            _review(at="2026-01-02T00:00:00Z"),
            _review("CHANGES_REQUESTED", at=LATER, id=1),
        ],  # tie, equal ids
        [_review(at=LATER, id=None), _review("CHANGES_REQUESTED", at=LATER, id=2)],  # tie, no id
        [
            _review(at=LATER, id=3),
            _review("CHANGES_REQUESTED", at=LATER, id=7),
        ],  # tie, higher id wins
        [_review("COMMENTED")],
        [_review("PENDING")],
        [_review(login="someone-else")],
        [_review(kind="Bot")],
        [_review(login="owner-x[bot]")],
        [_review(body="ok <!-- franky-review:abc -->")],
        [_review(body="ok <!-- bot-review:abc -->")],
        [],
        None,
        "nope",
    ],
)
def test_unverified_acceptance_falls_back_to_open(reviews):
    shaped = _verify(reviews, _accepted(severity="blocking"))
    f = shaped["findings"][0]
    assert f["status"] == "open" and f["accepted_by"] is None
    assert shaped["has_blocking"] is True


class _Perms:
    """A fake repository-permission read that records every login it is asked about."""

    def __init__(self, value):
        self.value, self.asked = value, []

    def __call__(self, login):
        self.asked.append(login)
        return self.value


def _verify_perm(reviews, perms, finding=None):
    shaped = _shaped(finding or _accepted(severity="blocking"))
    verify_accepted(shaped, reviews, HEAD, "author-y", PRIOR, permission_of=perms)
    return shaped


@pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "NONE"])
def test_a_private_member_with_write_permission_can_accept(assoc, permission):
    # An App token sees a private org member as CONTRIBUTOR: the repository permission decides.
    perms = _Perms(permission)
    shaped = _verify_perm([_review(assoc=assoc)], perms)
    assert shaped["findings"][0]["status"] == "accepted"
    assert shaped["has_blocking"] is False
    assert perms.asked == ["owner-x"]


@pytest.mark.parametrize("permission", ["read", "triage", "none", "", None, 5, "WRITE"])
def test_a_non_writer_cannot_accept_through_the_permission_read(permission):
    shaped = _verify_perm([_review(assoc="CONTRIBUTOR")], _Perms(permission))
    assert shaped["findings"][0]["status"] == "open"
    assert shaped["has_blocking"] is True


@pytest.mark.parametrize(
    "review",
    [
        _review(assoc="CONTRIBUTOR", sha="b" * 40),
        _review(assoc="CONTRIBUTOR", at=RAISED),
        _review(assoc="CONTRIBUTOR", kind="Bot"),
        _review("CHANGES_REQUESTED", assoc="CONTRIBUTOR"),
        _review(assoc=None),
        _review(assoc=5),
    ],
)
def test_the_permission_read_never_rescues_a_failed_approval(review):
    perms = _Perms("admin")
    assert _verify_perm([review], perms)["findings"][0]["status"] == "open"
    assert perms.asked == []


def test_an_authorized_association_needs_no_permission_read():
    perms = _Perms(None)
    assert _verify_perm([_review()], perms)["findings"][0]["status"] == "accepted"
    assert perms.asked == []


def test_the_permission_is_read_once_per_login():
    perms = _Perms("write")
    prior = PRIOR + [{"title": "other", "file": "a.py", "raised_at": RAISED}]
    shaped = _shaped(
        _accepted(severity="blocking"),
        _f(title="other", status="accepted", accepted_by="Owner-X", severity="blocking"),
    )
    verify_accepted(
        shaped, [_review(assoc="CONTRIBUTOR")], HEAD, "author-y", prior, permission_of=perms
    )
    assert [f["status"] for f in shaped["findings"]] == ["accepted", "accepted"]
    assert len(perms.asked) == 1


def test_a_contributor_without_a_permission_reader_stays_open():
    shaped = _verify([_review(assoc="CONTRIBUTOR")], _accepted(severity="blocking"))
    assert shaped["findings"][0]["status"] == "open"


def test_a_later_comment_review_does_not_hide_the_approval_and_login_case_is_ignored():
    reviews = [_review(login="Owner-X"), _review("COMMENTED", at="2026-01-05T00:00:00Z")]
    assert _verify(reviews)["findings"][0]["status"] == "accepted"
    assert _verify([_review(sha=HEAD.upper())])["findings"][0]["status"] == "accepted"


def test_author_cannot_accept_their_own_pr_case_insensitively():
    shaped = _verify([_review()], author="OWNER-X")
    assert shaped["findings"][0]["status"] == "open"


@pytest.mark.parametrize("author", [None, "", 5])
def test_unknown_pr_author_fails_closed(author):
    assert _verify([_review()], author=author)["findings"][0]["status"] == "open"


@pytest.mark.parametrize(
    "prior",
    [
        None,
        "x",
        [],
        [{"title": "other", "file": "a.py", "raised_at": RAISED}],
        [{"title": "tradeoff", "file": "b.py", "raised_at": RAISED}],
        [{"title": "tradeoff", "file": "a.py"}],  # no raised_at
        [{"title": "tradeoff", "file": "a.py", "raised_at": "garbage"}],
        ["junk"],
    ],
)
def test_acceptance_needs_one_matching_prior_with_a_raised_at(prior):
    assert _verify([_review()], prior=prior)["findings"][0]["status"] == "open"


def test_one_prior_entry_serves_one_accepted_finding_only():
    shaped = _shaped(_accepted(), _accepted())
    verify_accepted(shaped, [_review()], HEAD, "author-y", PRIOR)
    assert [f["status"] for f in shaped["findings"]] == ["accepted", "open"]


def test_verify_accepted_leaves_other_statuses_alone_and_never_raises():
    shaped = _shaped(_f(title="a", status="open"), _f(title="b", status="resolved"), _f(title="c"))
    verify_accepted(shaped, None, HEAD, None, None)
    assert [f["status"] for f in shaped["findings"]] == ["open", "resolved", "new"]
    junk = _shaped(_accepted())
    verify_accepted(junk, [None, 3, {"user": 5, "state": "APPROVED"}], HEAD, "a", PRIOR)
    assert junk["findings"][0]["status"] == "open"


def test_accepted_needs_a_valid_login_and_a_thread():
    bad = ["", None, "a b", "-x", "x" * 40, "o/x", 5]
    for login in bad:
        f = _shaped(_f(status="accepted", accepted_by=login))["findings"][0]
        assert f["status"] == "open" and f["accepted_by"] is None
    f = _shaped(_f(status="accepted", accepted_by=" @owner-x "))["findings"][0]
    assert f["status"] == "accepted" and f["accepted_by"] == "owner-x"
    plain = _shaped(_f(status="accepted", accepted_by="owner-x"), threaded=False)["findings"][0]
    assert plain["status"] == "new" and plain["accepted_by"] is None


def test_accepted_finding_is_not_live_in_headline_questions_or_blocking():
    shaped = _shaped(
        _f(title="q1", severity="question", status="accepted", accepted_by="owner-x"),
        _f(title="q2", severity="question", status="open"),
        _f(title="q3", severity="question", status="open"),
        _f(title="q4", severity="question", status="resolved"),
        _f(title="b", severity="blocking", status="accepted", accepted_by="owner-x"),
    )
    assert shaped["dropped_malformed"] == 0  # accepted questions do not use the cap
    assert shaped["has_blocking"] is False
    assert "no findings above nit" not in render_review_body(shaped)  # q2, q3 still live
    body = render_review_body(_shaped(_f(severity="blocking", status="accepted", accepted_by="o")))
    assert "no findings above nit." in body


def test_body_lists_accepted_risk_with_escaped_login_and_title():
    shaped = _shaped(_f(title="a <b>", status="accepted", accepted_by="owner-x"))
    body = render_review_body(shaped)
    assert "- Accepted risk (by @owner-x): a &lt;b&gt; (a.py:11)" in body
    nit = _shaped(_f(severity="nit", status="accepted", accepted_by="owner-x"))
    payload = build_review_payload(nit, commentable_lines(FILES), HEAD, [])
    assert "Accepted risk" not in payload["body"]  # prior nits stay out of a posted body


def test_accepted_finding_never_gets_an_inline_comment_or_blocking_heading():
    shaped = _shaped(
        _f(title="fresh", severity="normal"),
        _f(title="tradeoff", severity="blocking", status="accepted", accepted_by="owner-x"),
        _f(title="old", severity="blocking", status="resolved"),
    )
    payload = build_review_payload(shaped, commentable_lines(FILES), HEAD, [], allow_approve=True)
    bodies = [c["body"] for c in payload["comments"]]
    assert len(bodies) == 1 and bodies[0].startswith("**Major: fresh**")
    assert not any(b.startswith(("**Blocking:", "**Major: tradeoff")) for b in bodies)
    assert "**Blocking:" not in payload["body"]
    assert "Accepted risk (by @owner-x): tradeoff" in payload["body"]


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_an_owner_member_or_collaborator_approval_counts(assoc):
    assert _verify([_review(assoc=assoc)])["findings"][0]["status"] == "accepted"


def test_a_same_time_tie_is_broken_by_the_larger_review_id():
    reviews = [_review(id=9), _review("CHANGES_REQUESTED", id=3)]
    assert _verify(reviews)["findings"][0]["status"] == "accepted"


def test_acceptance_compares_full_precision_and_needs_strictly_after():
    raised = [{**PRIOR[0], "raised_at": "2026-01-01T00:00:00.500Z"}]
    early = _verify([_review(at="2026-01-01T00:00:00.250Z")], prior=raised)
    assert early["findings"][0]["status"] == "open"  # same second, earlier subsecond
    equal = _verify([_review(at="2026-01-01T00:00:00.500Z")], prior=raised)
    assert equal["findings"][0]["status"] == "open"
    late = _verify([_review(at="2026-01-01T00:00:00.750Z")], prior=raised)
    assert late["findings"][0]["status"] == "accepted"


def test_two_prior_entries_with_the_same_key_make_the_acceptance_ambiguous():
    shaped = _verify([_review()], prior=[*PRIOR, {**PRIOR[0], "title": "tradeoff"}])
    assert shaped["findings"][0]["status"] == "open"


def test_a_demoted_question_counts_against_the_question_cap_again():
    shaped = _shaped(
        _f(title="q1", severity="question", status="accepted", accepted_by="owner-x"),
        _f(title="q2", severity="question", status="open"),
        _f(title="q3", severity="question", status="open"),
    )
    assert shaped["dropped_malformed"] == 0
    verify_accepted(shaped, None, HEAD, "author-y", [])
    assert [f["title"] for f in shaped["findings"]] == ["q1", "q2"]
    assert shaped["dropped_malformed"] == 1
    assert review_event(shaped, allow_approve=True) == "COMMENT"


def test_body_has_one_fixed_line_for_verified_accepted_risks_above_nit():
    shaped = _shaped(
        _f(title="a", severity="blocking", status="accepted", accepted_by="owner-x"),
        _f(title="b", severity="normal", status="accepted", accepted_by="owner-x"),
        _f(title="c", severity="normal", status="accepted", accepted_by="Other-y"),
        _f(title="d", severity="nit", status="accepted", accepted_by="nit-z"),
        _f(title="q", severity="question", status="accepted", accepted_by="q-w"),
    )
    body = render_review_body(shaped)
    line = (
        "Accepted risk: 4 finding(s) above nit accepted by @owner-x, @Other-y, @q-w "
        "(approval on this commit)."
    )
    assert body.count(line) == 1
    assert body.index(line) < body.index("<details>")
    assert "Accepted risk:" not in render_review_body(_shaped(_f(severity="nit")))
    only_nit = _shaped(_f(severity="nit", status="accepted", accepted_by="nit-z"))
    assert "Accepted risk:" not in render_review_body(only_nit)
