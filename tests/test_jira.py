"""Tests for franky.jira: fetch_jira_issue, _flatten_adf, error handling.

No real network - every HTTP call is injected via the `opener` parameter.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from franky.jira import (
    JIRA_API_TOKEN_VAR,
    JIRA_BASE_URL_VAR,
    JIRA_EMAIL_VAR,
    _flatten_adf,
    fetch_jira_issue,
)

import urllib.error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_TOKEN = "tok-secret-abc123"
FAKE_EMAIL = "user@example.com"
FAKE_BASE = "https://example.atlassian.net"

_GOOD_ENV = {
    JIRA_BASE_URL_VAR: FAKE_BASE,
    JIRA_EMAIL_VAR: FAKE_EMAIL,
    JIRA_API_TOKEN_VAR: FAKE_TOKEN,
}


def _make_opener(body: bytes, status: int = 200):
    """Return a callable that mimics urllib.request.urlopen returning `body`."""

    class _FakeResp:
        def __init__(self):
            self._data = BytesIO(body)

        def read(self):
            return self._data.read()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def opener(req, *, timeout=None):
        if status != 200:
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=status,
                msg="err",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )
        return _FakeResp()

    return opener


def _adf_body(paragraphs: list[str]) -> dict:
    """Build a minimal ADF document with paragraph nodes."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": p}],
            }
            for p in paragraphs
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_adf_description():
    payload = {
        "fields": {
            "summary": "Do a thing",
            "description": _adf_body(["First paragraph.", "Second paragraph."]),
        }
    }
    opener = _make_opener(json.dumps(payload).encode())
    result = fetch_jira_issue("FOO-123", _GOOD_ENV, opener=opener)
    assert "[FOO-123]" in result
    assert "Do a thing" in result
    assert "First paragraph." in result
    assert "Second paragraph." in result


def test_null_description_returns_summary_only():
    payload = {"fields": {"summary": "Summary only", "description": None}}
    opener = _make_opener(json.dumps(payload).encode())
    result = fetch_jira_issue("FOO-456", _GOOD_ENV, opener=opener)
    assert "[FOO-456]" in result
    assert "Summary only" in result
    # No trailing newlines or junk after the summary
    assert result.strip() == result
    assert "\n" not in result


def test_string_description_used_directly():
    payload = {"fields": {"summary": "Plain text", "description": "Some plain text desc."}}
    opener = _make_opener(json.dumps(payload).encode())
    result = fetch_jira_issue("BAR-7", _GOOD_ENV, opener=opener)
    assert "[BAR-7]" in result
    assert "Some plain text desc." in result


# ---------------------------------------------------------------------------
# Missing creds
# ---------------------------------------------------------------------------


def _env_without(var: str) -> dict:
    return {k: v for k, v in _GOOD_ENV.items() if k != var}


@pytest.mark.parametrize("missing_var", [JIRA_BASE_URL_VAR, JIRA_EMAIL_VAR, JIRA_API_TOKEN_VAR])
def test_missing_cred_raises_naming_var(missing_var):
    opener = _make_opener(b"")
    with pytest.raises(ValueError, match=missing_var):
        fetch_jira_issue("FOO-1", _env_without(missing_var), opener=opener)


@pytest.mark.parametrize("missing_var", [JIRA_BASE_URL_VAR, JIRA_EMAIL_VAR, JIRA_API_TOKEN_VAR])
def test_missing_cred_message_does_not_contain_token(missing_var):
    opener = _make_opener(b"")
    with pytest.raises(ValueError) as exc_info:
        fetch_jira_issue("FOO-1", _env_without(missing_var), opener=opener)
    assert FAKE_TOKEN not in str(exc_info.value)


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def test_http_base_url_rejected():
    env = {**_GOOD_ENV, JIRA_BASE_URL_VAR: "http://example.atlassian.net"}
    opener = _make_opener(b"")
    with pytest.raises(ValueError, match="https://"):
        fetch_jira_issue("FOO-1", env, opener=opener)


def test_schemeless_base_url_rejected():
    env = {**_GOOD_ENV, JIRA_BASE_URL_VAR: "example.atlassian.net"}
    opener = _make_opener(b"")
    with pytest.raises(ValueError, match="https://"):
        fetch_jira_issue("FOO-1", env, opener=opener)


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


def test_http_404_raises_not_found():
    opener = _make_opener(b"", status=404)
    with pytest.raises(ValueError, match="not found"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_http_401_raises_auth_message():
    opener = _make_opener(b"", status=401)
    with pytest.raises(ValueError, match="auth failed"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_http_403_raises_auth_message():
    opener = _make_opener(b"", status=403)
    with pytest.raises(ValueError, match="auth failed"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_http_500_raises_generic_fetch_failed():
    opener = _make_opener(b"", status=500)
    with pytest.raises(ValueError, match="fetch failed"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_network_error_raises_could_not_reach():
    def opener(req, *, timeout=None):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(ValueError, match="could not reach JIRA"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_network_error_does_not_contain_token():
    def opener(req, *, timeout=None):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(ValueError) as exc_info:
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)
    assert FAKE_TOKEN not in str(exc_info.value)


def test_unparseable_json_raises():
    opener = _make_opener(b"not json at all")
    with pytest.raises(ValueError, match="unparseable JSON"):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_valid_json_missing_summary_raises_clean_valueerror():
    # A valid-JSON response lacking fields.summary must surface as ValueError (clean
    # ClickException), NOT a KeyError/TypeError traceback (not caught by the CLI handler).
    for payload in ({}, {"fields": {}}, {"fields": None}):
        opener = _make_opener(json.dumps(payload).encode())
        with pytest.raises(ValueError, match="missing fields.summary"):
            fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)


def test_http_404_does_not_contain_token():
    opener = _make_opener(b"", status=404)
    with pytest.raises(ValueError) as exc_info:
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)
    assert FAKE_TOKEN not in str(exc_info.value)


def test_http_401_does_not_contain_token():
    opener = _make_opener(b"", status=401)
    with pytest.raises(ValueError) as exc_info:
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)
    assert FAKE_TOKEN not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_url_contains_key_and_fields():
    """The URL passed to the opener must contain the key and fields=summary,description."""
    captured = {}

    payload = {"fields": {"summary": "S", "description": None}}

    class _FakeResp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def opener(req, *, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp()

    fetch_jira_issue("FOO-99", _GOOD_ENV, opener=opener)
    assert "FOO-99" in captured["url"]
    assert "fields=summary,description" in captured["url"]
    assert captured["auth"].startswith("Basic ")


# ---------------------------------------------------------------------------
# ADF flattening (direct unit tests for the less-common node types)
# ---------------------------------------------------------------------------


def test_flatten_adf_list_items_and_hardbreak():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                        ],
                    },
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "b"},
                ],
            },
        ],
    }
    out = _flatten_adf(doc)
    assert "- one" in out
    assert "- two" in out
    assert "a\nb" in out


def test_flatten_adf_unknown_node_recurses_into_content():
    # An unknown node type must not drop its nested text - it recurses into content.
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "someFutureNode",
                "content": [{"type": "text", "text": "still here"}],
            }
        ],
    }
    assert "still here" in _flatten_adf(doc)
