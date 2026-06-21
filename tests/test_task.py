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
