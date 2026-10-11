"""Unit tests for the best-effort idempotency pre-check (network-free via an injected opener)."""

import base64
import io
import json
import urllib.error

import pytest

from franky.idempotency import fetch_signoff_globs, find_open_pr


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
    }
    sha = fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, calls), meta_sink=sink)
    assert sha == _SHA
    assert sink == {
        "title": "ABC-1 fix",
        "body": "see OPS-9",
        "head_ref": "abc-1-fix",
        "base_sha": "b" * 40,
        "private": False,
    }
    assert len(calls) == 1


def test_meta_sink_null_body_becomes_empty_string():
    sink = {}
    payload = {"title": None, "body": None, "head": {"sha": _SHA, "ref": 5}}
    fetch_pr_head_sha("me/repo", 7, {}, opener=_pr_opener(payload, []), meta_sink=sink)
    assert sink == {"title": "", "body": "", "head_ref": "", "base_sha": "", "private": False}


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
