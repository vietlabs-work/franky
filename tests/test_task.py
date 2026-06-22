import pytest

from franky.task import PROSE_MAX_CHARS, parse_task

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
