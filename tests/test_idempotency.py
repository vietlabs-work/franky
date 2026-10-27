"""Unit tests for the best-effort idempotency pre-check (network-free via an injected opener)."""

import base64
import io
import json
import urllib.error

import pytest

from franky.idempotency import (
    fetch_repo_file,
    fetch_signoff_content_rules,
    fetch_signoff_globs,
    find_open_pr,
)


class _FakeResp:
    """A minimal context-manager response: json.load reads .read(), status is inspectable."""

    def __init__(self, payload, status=200):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))
        self.status = status

    def read(self, *a):
        return self._buf.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_found_returns_html_url():
    def opener(req, timeout=None):
        # The owner-prefixed head filter must be present on the request URL.
        assert "head=me:franky/issue-42" in req.full_url
        return _FakeResp([{"html_url": "https://github.com/me/repo/pull/7"}])

    url = find_open_pr("me/repo", "franky/issue-42", {"GH_TOKEN": "ghp_x"}, opener=opener)
    assert url == "https://github.com/me/repo/pull/7"


def test_not_found_empty_list_returns_none():
    def opener(req, timeout=None):
        return _FakeResp([])

    assert find_open_pr("me/repo", "franky/x", {"GH_TOKEN": "t"}, opener=opener) is None


def test_error_degrades_to_none():
    def opener(req, timeout=None):
        raise OSError("network down")

    assert find_open_pr("me/repo", "franky/x", {"GH_TOKEN": "t"}, opener=opener) is None


def test_no_token_still_attempts_and_no_auth_header():
    captured = {}

    def opener(req, timeout=None):
        captured["has_auth"] = "Authorization" in req.headers
        return _FakeResp([])

    # No GH_TOKEN -> no Authorization header, and a clean None result.
    assert find_open_pr("me/repo", "franky/x", {}, opener=opener) is None
    assert captured["has_auth"] is False


def test_non_200_returns_none():
    def opener(req, timeout=None):
        return _FakeResp([{"html_url": "x"}], status=404)

    assert find_open_pr("me/repo", "franky/x", {"GH_TOKEN": "t"}, opener=opener) is None


# --- fetch_pr_head_sha meta_sink -------------------------------------------------------------

from franky.idempotency import fetch_pr_head_sha  # noqa: E402

_SHA = "a" * 40


def _pr_opener(payload, calls):
    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp(payload)

    return opener


def test_meta_sink_filled_from_one_response():
    calls, sink = [], {}
    payload = {
        "title": "ABC-1 fix",
        "body": "see OPS-9",
        "head": {"sha": _SHA, "ref": "abc-1-fix"},
        "base": {"sha": "b" * 40},
        "user": {"login": "owner-x"},
    }
    sha = fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, calls), meta_sink=sink)
    assert sha == _SHA
    assert sink == {
        "title": "ABC-1 fix",
        "body": "see OPS-9",
        "head_ref": "abc-1-fix",
        "base_sha": "b" * 40,
        "private": False,
        "author": "owner-x",
    }
    assert len(calls) == 1


def test_meta_sink_null_body_becomes_empty_string():
    sink = {}
    payload = {"title": None, "body": None, "head": {"sha": _SHA, "ref": 5}}
    fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), meta_sink=sink)
    assert sink == {
        "title": "",
        "body": "",
        "head_ref": "",
        "base_sha": "",
        "private": False,
        "author": None,
    }


def test_meta_sink_private_only_for_literal_true():
    for value, expected in (
        (True, True),
        (False, False),
        ("true", False),
        (1, False),
        (None, False),
    ):
        sink = {}
        payload = {"head": {"sha": _SHA}, "base": {"repo": {"private": value}}}
        fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), meta_sink=sink)
        assert sink["private"] is expected
    for base in (None, "x", {"repo": "x"}, {}):
        sink = {}
        payload = {"head": {"sha": _SHA}, "base": base}
        fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), meta_sink=sink)
        assert sink["private"] is False


def test_meta_sink_untouched_on_invalid_or_missing_sha():
    for head in ({"sha": "", "ref": "x"}, {"ref": "x"}, "nope"):
        sink = {}
        payload = {"title": "t", "body": "b", "head": head}
        assert (
            fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), meta_sink=sink)
            is None
        )
        assert sink == {}


def test_fetch_pr_head_sha_without_sink_unchanged():
    payload = {"head": {"sha": _SHA}}
    assert fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, [])) == _SHA


BASE = "b" * 40


def _file_payload(text, **over):
    raw = text.encode()
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode(),
        **over,
    }


def test_signoff_globs_returns_lines_without_comments_and_blanks():
    seen = []

    def opener(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        return _FakeResp(_file_payload("# header\n\n**/pay/**\n  /top.txt  \n"))

    assert fetch_signoff_globs("me/repo", BASE, {"GH_TOKEN": "ghp_x"}, opener=opener) == [
        "**/pay/**",
        "/top.txt",
    ]
    assert seen == [
        (
            f"https://api.github.com/repos/me/repo/contents/.github/human-signoff-paths?ref={BASE}",
            "Bearer ghp_x",
        )
    ]


def _http_error(code):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "x", None, None)  # type: ignore[arg-type]

    return opener


def test_signoff_globs_404_means_no_list_and_other_errors_fail_closed():
    assert fetch_signoff_globs("me/repo", BASE, {}, opener=_http_error(404)) == []
    assert fetch_signoff_globs("me/repo", BASE, {}, opener=_http_error(500)) is None
    assert fetch_signoff_globs("me/repo", BASE, {}, opener=_http_error(403)) is None


@pytest.mark.parametrize(
    "payload",
    [
        _file_payload("a", type="dir"),
        _file_payload("a", size=64 * 1024 + 1),
        _file_payload("a", encoding="none"),
        _file_payload("a", content="!!not base64!!"),
        _file_payload("a", content="!!!!"),
        _file_payload("a", content=base64.b64encode(b"\xff\xfe").decode()),
        ["not", "a", "dict"],
    ],
)
def test_signoff_globs_bad_payload_fails_closed(payload):
    assert (
        fetch_signoff_globs("me/repo", BASE, {}, opener=lambda r, timeout=None: _FakeResp(payload))
        is None
    )


def test_signoff_globs_non_200_and_bad_sha_fail_closed():
    assert (
        fetch_signoff_globs(
            "me/repo", BASE, {}, opener=lambda r, timeout=None: _FakeResp(_file_payload("a"), 204)
        )
        is None
    )

    def never(req, timeout=None):
        raise AssertionError("a bad sha must not reach the network")

    assert fetch_signoff_globs("me/repo", "main", {}, opener=never) is None
    assert fetch_signoff_globs("me/repo", "", {}, opener=never) is None


def test_content_rules_split_glob_and_fixed_string():
    seen = []

    def opener(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResp(
            _file_payload("# c\n\n**/src/main/** CurrencyHelper\n/a.txt  two words \n")
        )

    assert fetch_signoff_content_rules("me/repo", BASE, {}, opener=opener) == [
        ("**/src/main/**", "CurrencyHelper"),
        ("/a.txt", "two words"),
    ]
    assert seen == [
        f"https://api.github.com/repos/me/repo/contents/.github/human-signoff-content?ref={BASE}"
    ]
    assert fetch_signoff_content_rules("me/repo", BASE, {}, opener=_http_error(404)) == []
    assert fetch_signoff_content_rules("me/repo", BASE, {}, opener=_http_error(500)) is None
    # a glob with no string is malformed: fail closed
    bad = lambda r, timeout=None: _FakeResp(_file_payload("**/src/main/**\n"))  # noqa: E731
    assert fetch_signoff_content_rules("me/repo", BASE, {}, opener=bad) is None


def test_fetch_repo_file_returns_bytes_empty_for_404_and_honors_the_cap():
    ok = lambda r, timeout=None: _FakeResp(_file_payload("raw \xff"))  # noqa: E731
    assert fetch_repo_file("me/repo", "a b/c.txt", BASE, {}, opener=ok) == "raw \xff".encode()
    assert fetch_repo_file("me/repo", "a", BASE, {}, opener=_http_error(404)) == b""
    assert fetch_repo_file("me/repo", "a", BASE, {}, opener=_http_error(500)) is None
    big = lambda r, timeout=None: _FakeResp(_file_payload("a", size=11))  # noqa: E731
    assert fetch_repo_file("me/repo", "a", BASE, {}, max_bytes=10, opener=big) is None


def test_fetch_repo_file_can_treat_a_404_as_unreadable():
    assert fetch_repo_file("me/repo", "a", BASE, {}, absent=None, opener=_http_error(404)) is None
    assert fetch_repo_file("me/repo", "a", BASE, {}, opener=_http_error(404)) == b""


# --- fetch_pr_head_sha error_sink ------------------------------------------------------------


def _raising_opener(exc):
    def opener(req, timeout=None):
        raise exc

    return opener


def test_error_sink_names_the_failure_without_its_message():
    import urllib.error

    cases = [
        (_raising_opener(urllib.error.HTTPError("u", 502, "x", {}, None)), "HTTP 502"),
        (_raising_opener(TimeoutError("https://api.github.com/secret-path")), "TimeoutError"),
        (lambda req, timeout=None: _FakeResp({"head": {}}, status=200), "bad response"),
        (lambda req, timeout=None: _FakeResp({}, status=404), "HTTP 404"),
    ]
    for opener, want in cases:
        why = {}
        assert fetch_pr_head_sha("me/repo", 7, {}, opener=opener, error_sink=why) is None
        assert why == {"reason": want}


def test_error_sink_is_untouched_on_success():
    why = {}
    payload = {"head": {"sha": _SHA}}
    assert (
        fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), error_sink=why) == _SHA
    )
    assert why == {}
