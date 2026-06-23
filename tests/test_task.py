import pytest

from franky.task import PROSE_MAX_CHARS, parse_pr_task, parse_task

ALLOWED = ["me/repo", "octocat/hello"]


def test_parse_gh_issue_url():
    url = "https://github.com/octocat/hello/issues/42"
    spec = parse_task(url, None, ALLOWED)
    assert spec.source == "issue"
    assert spec.repo == "octocat/hello"
    assert spec.text == url


def test_parse_prose_with_repo():
    spec = parse_task("add a --json flag", "me/repo", ALLOWED)
    assert spec.source == "prose"
    assert spec.repo == "me/repo"
    assert spec.text == "add a --json flag"


def test_prose_without_repo_raises():
    with pytest.raises(ValueError, match="--repo"):
        parse_task("do the thing", None, ALLOWED)


def test_repo_not_in_allowlist_raises():
    with pytest.raises(ValueError, match="allowlist"):
        parse_task("do the thing", "stranger/repo", ALLOWED)


def test_issue_repo_off_allowlist_raises():
    url = "https://github.com/stranger/repo/issues/1"
    with pytest.raises(ValueError, match="allowlist"):
        parse_task(url, None, ALLOWED)


def test_url_vs_flag_conflict_raises():
    url = "https://github.com/octocat/hello/issues/42"
    with pytest.raises(ValueError, match="conflict"):
        parse_task(url, "me/repo", ALLOWED)


def test_url_with_matching_flag_ok():
    url = "https://github.com/octocat/hello/issues/42"
    spec = parse_task(url, "octocat/hello", ALLOWED)
    assert spec.repo == "octocat/hello"


def test_prose_length_cap():
    long = "x " * 5000
    spec = parse_task(long, "me/repo", ALLOWED)
    assert len(spec.text) <= PROSE_MAX_CHARS


# ---------------------------------------------------------------------------
# JIRA input
# ---------------------------------------------------------------------------


def test_jira_valid_key():
    spec = parse_task("jira FOO-123", "me/repo", ALLOWED)
    assert spec.source == "jira"
    assert spec.repo == "me/repo"
    assert spec.text == "FOO-123"


def test_jira_case_insensitive_keyword():
    spec = parse_task("JIRA FOO-123", "me/repo", ALLOWED)
    assert spec.source == "jira"
    assert spec.text == "FOO-123"


def test_jira_invalid_key_raises():
    with pytest.raises(ValueError, match="invalid JIRA key"):
        parse_task("jira foo_123", "me/repo", ALLOWED)


def test_jira_missing_repo_raises():
    with pytest.raises(ValueError, match="--repo"):
        parse_task("jira FOO-123", None, ALLOWED)


def test_jira_off_allowlist_repo_raises():
    with pytest.raises(ValueError, match="allowlist"):
        parse_task("jira FOO-123", "stranger/repo", ALLOWED)


def test_jira_multiword_remainder_falls_through_to_prose():
    # "jira is flaky, fix it" is NOT a jira task (multi-word after keyword)
    spec = parse_task("jira is flaky, fix it", "me/repo", ALLOWED)
    assert spec.source == "prose"


def test_jira_does_not_match_gh_issue_url():
    # A real GitHub URL must never accidentally match the jira branch.
    url = "https://github.com/me/repo/issues/1"
    spec = parse_task(url, None, ALLOWED)
    assert spec.source == "issue"


# ---------------------------------------------------------------------------
# PR URL parsing (the `iterate` command)
# ---------------------------------------------------------------------------


def test_parse_pr_task_happy():
    spec = parse_pr_task("https://github.com/octocat/hello/pull/42", ALLOWED)
    assert spec.source == "pr"
    assert spec.repo == "octocat/hello"
    # text is the canonical URL reconstructed from the captured groups.
    assert spec.text == "https://github.com/octocat/hello/pull/42"


def test_parse_pr_task_off_allowlist_raises():
    with pytest.raises(ValueError, match="allowlist"):
        parse_pr_task("https://github.com/stranger/repo/pull/1", ALLOWED)


def test_parse_pr_task_rejects_non_pr_url():
    # An issue URL is NOT a PR URL - iterate must refuse it.
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_task("https://github.com/me/repo/issues/1", ALLOWED)


def test_parse_pr_task_rejects_prose():
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_task("fix the flaky test", ALLOWED)


def test_parse_pr_task_anchored_rejects_trailing_path():
    # Anchoring is load-bearing: a trailing path/traversal must NOT slip an off-allowlist
    # repo past the gate (the agent clones + pushes, so a repo/URL mismatch is a write escape).
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_task("https://github.com/me/repo/pull/1/../../stranger/evil/pull/2", ALLOWED)
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_task("https://github.com/me/repo/pull/1/files", ALLOWED)


def test_parse_pr_task_canonicalizes_away_query_via_anchor():
    # A trailing query is rejected by the anchored regex (the operator pastes a clean URL);
    # this proves raw input never reaches downstream with extra cruft.
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_task("https://github.com/me/repo/pull/1?diff=split", ALLOWED)
