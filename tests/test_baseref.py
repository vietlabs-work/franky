"""Unit tests for the best-effort base-commit resolution/existence check (network-free via an
injected opener) - `franky job replay`'s (issue #70) host-side helpers.
"""

import io
import json
import urllib.error

from franky.baseref import commit_exists, is_valid_sha, resolve_base_sha


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


# ---------------------------------------------------------------------------
# resolve_base_sha
# ---------------------------------------------------------------------------


def test_resolve_base_sha_happy_path():
    def opener(req, timeout=None):
        assert "commits?per_page=1" in req.full_url
        return _FakeResp([{"sha": "abc123def4567890"}])

    assert resolve_base_sha("me/repo", {}, opener=opener) == "abc123def4567890"


def test_resolve_base_sha_non_200_returns_none():
    def opener(req, timeout=None):
        return _FakeResp([{"sha": "abc123"}], status=500)

    assert resolve_base_sha("me/repo", {}, opener=opener) is None


def test_resolve_base_sha_empty_list_returns_none():
    def opener(req, timeout=None):
        return _FakeResp([])

    assert resolve_base_sha("me/repo", {}, opener=opener) is None


def test_resolve_base_sha_error_degrades_to_none():
    def opener(req, timeout=None):
        raise OSError("network down")

    assert resolve_base_sha("me/repo", {}, opener=opener) is None


def test_resolve_base_sha_adds_bearer_when_token_present():
    captured = {}

    def opener(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp([{"sha": "deadbeef"}])

    resolve_base_sha("me/repo", {"GH_TOKEN": "ghp_x"}, opener=opener)
    assert captured["auth"] == "Bearer ghp_x"


def test_resolve_base_sha_no_token_no_auth_header():
    captured = {}

    def opener(req, timeout=None):
        captured["has_auth"] = "Authorization" in req.headers
        return _FakeResp([{"sha": "deadbeef"}])

    resolve_base_sha("me/repo", {}, opener=opener)
    assert captured["has_auth"] is False


# ---------------------------------------------------------------------------
# commit_exists
# ---------------------------------------------------------------------------


def test_commit_exists_200_returns_true():
    def opener(req, timeout=None):
        assert "commits/abc1234" in req.full_url
        return _FakeResp({"sha": "abc1234"})

    assert commit_exists("me/repo", "abc1234", {}, opener=opener) is True


def test_commit_exists_404_returns_false():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    assert commit_exists("me/repo", "abc1234567", {}, opener=opener) is False


def test_commit_exists_422_returns_false():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

    assert commit_exists("me/repo", "abc1234567", {}, opener=opener) is False


def test_commit_exists_other_http_error_returns_none():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

    assert commit_exists("me/repo", "abc1234567", {}, opener=opener) is None


def test_commit_exists_generic_error_returns_none():
    def opener(req, timeout=None):
        raise OSError("network down")

    assert commit_exists("me/repo", "abc1234567", {}, opener=opener) is None


def test_commit_exists_malformed_sha_rejected_without_calling_opener():
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp({"sha": "x"})

    assert commit_exists("me/repo", "../../etc", {}, opener=opener) is None
    assert commit_exists("me/repo", "nothex!!", {}, opener=opener) is None
    assert commit_exists("me/repo", "", {}, opener=opener) is None
    assert calls == []  # opener must never be invoked for a shape-invalid sha


def test_commit_exists_non_str_sha_returns_none_never_raises():
    # A corrupt/hand-edited record could store base_sha as a JSON number (truthy, non-str);
    # the isinstance guard must keep this from raising TypeError inside the regex match.
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp({"sha": "x"})

    assert commit_exists("me/repo", 12345, {}, opener=opener) is None
    assert commit_exists("me/repo", None, {}, opener=opener) is None
    assert calls == []


# ---------------------------------------------------------------------------
# is_valid_sha
# ---------------------------------------------------------------------------


def test_is_valid_sha():
    assert is_valid_sha("abc1234") is True
    assert is_valid_sha("a" * 40) is True
    assert is_valid_sha("abc") is False  # too short (< 7)
    assert is_valid_sha("a" * 41) is False  # too long (> 40)
    assert is_valid_sha("nothex!") is False
    assert is_valid_sha("") is False
    # Non-str inputs must never raise - just return False.
    assert is_valid_sha(12345) is False
    assert is_valid_sha(None) is False
