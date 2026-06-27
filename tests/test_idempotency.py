"""Unit tests for the best-effort idempotency pre-check (network-free via an injected opener)."""

import io
import json

from franky.idempotency import find_open_pr


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
