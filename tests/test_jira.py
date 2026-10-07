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
    extract_jira_keys,
    fetch_jira_issue,
    jira_configured,
)

import urllib.error

from franky.result import NetworkError

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

        def read(self, *_):
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
        def read(self, *_):
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


# ---------------------------------------------------------------------------
# extract_jira_keys / jira_configured / refuse_restricted / size cap
# ---------------------------------------------------------------------------


def test_extract_keys_title_branch_body_order():
    assert extract_jira_keys("ABC-1 fix", "ops-2-thing", "closes XY-3") == [
        "ABC-1",
        "OPS-2",
        "XY-3",
    ]


def test_extract_keys_browse_url_and_dedup():
    body = "see https://x.atlassian.net/browse/OPS-9 and OPS-9 again"
    assert extract_jira_keys("OPS-9", "", body) == ["OPS-9"]
    assert extract_jira_keys("", "", body) == ["OPS-9"]


def test_extract_keys_caps_at_three_in_order():
    assert extract_jira_keys("AA-1 BB-2 CC-3 DD-4", "", "") == ["AA-1", "BB-2", "CC-3"]


def test_extract_keys_ignores_non_tickets():
    assert extract_jira_keys("UTF-8 and SHA-256 foo_ABC-1", "", "") == []


def test_jira_configured():
    assert jira_configured(_GOOD_ENV)
    assert not jira_configured({**_GOOD_ENV, JIRA_EMAIL_VAR: "  "})
    assert not jira_configured({})


def _issue(**extra):
    return json.dumps({"fields": {"summary": "S", "description": None, **extra}}).encode()


def test_refuse_restricted_accepts_explicit_null_security():
    out = fetch_jira_issue(
        "FOO-1", _GOOD_ENV, opener=_make_opener(_issue(security=None)), refuse_restricted=True
    )
    assert out == "[FOO-1] S"


@pytest.mark.parametrize("extra", [{"security": {"name": "Secret"}}, {}])
def test_refuse_restricted_rejects_level_or_missing_key(extra):
    with pytest.raises(NetworkError) as exc:
        fetch_jira_issue(
            "FOO-1", _GOOD_ENV, opener=_make_opener(_issue(**extra)), refuse_restricted=True
        )
    assert "Secret" not in str(exc.value)


def test_refuse_restricted_requests_security_field_default_does_not():
    urls = []

    def opener(req, *, timeout=None):
        urls.append(req.full_url)
        return _make_opener(_issue(security={"name": "x"}))(req, timeout=timeout)

    fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener)  # default ignores security
    with pytest.raises(NetworkError):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener, refuse_restricted=True)
    assert "security" not in urls[0] and urls[1].endswith(",security")


def test_response_over_256_kib_is_refused():
    big = _issue(pad="x" * (256 * 1024))
    with pytest.raises(NetworkError):
        fetch_jira_issue("FOO-1", _GOOD_ENV, opener=_make_opener(big))


@pytest.mark.parametrize(
    ("head_ref", "expected"),
    [
        ("abc-123-fix", ["ABC-123"]),
        ("feature/abc-123-x", ["ABC-123"]),
        ("dependabot/pip/requests-2.32.0", []),
        ("renovate/node-18.x", []),
        ("release/v1-2", []),
    ],
)
def test_extract_keys_branch_rules(head_ref, expected):
    assert extract_jira_keys("", head_ref, "") == expected


def test_extract_keys_browse_url_skips_denylist_but_bare_does_not():
    assert extract_jira_keys("", "", "https://x.atlassian.net/browse/RFC-42") == ["RFC-42"]
    assert extract_jira_keys("", "", "see RFC-42 for details") == []


def _err(call, **kw):
    with pytest.raises(NetworkError) as info:
        call(**kw)
    return info.value


def _fetch(opener, **kw):
    return fetch_jira_issue("FOO-1", _GOOD_ENV, opener=opener, refuse_restricted=True, **kw)


@pytest.mark.parametrize(
    ("status", "reason", "stop"),
    [(404, "not_found", False), (500, "network", False), (401, "auth", True), (403, "auth", True)],
)
def test_http_errors_carry_reason(status, reason, stop):
    with pytest.raises(Exception) as info:
        _fetch(_make_opener(b"", status=status))
    assert (info.value.reason, info.value.stop) == (reason, stop)


def test_connection_failure_and_config_and_restricted_reasons():
    def boom(req, *, timeout=None):
        raise urllib.error.URLError("down")

    with pytest.raises(NetworkError) as info:
        _fetch(boom)
    assert (info.value.reason, info.value.stop) == ("network", True)
    with pytest.raises(NetworkError) as info:
        fetch_jira_issue("FOO-1", {**_GOOD_ENV, JIRA_BASE_URL_VAR: "http://x.test"})
    assert (info.value.reason, info.value.stop) == ("config", True)
    with pytest.raises(NetworkError) as info:
        _fetch(_make_opener(_issue(security={"name": "secret"})))
    assert (info.value.reason, info.value.stop) == ("restricted", False)


def test_no_redirect_handler_refuses_a_3xx():
    import urllib.request

    from franky.jira import _NoRedirect

    req = urllib.request.Request("https://example.atlassian.net/x")
    with pytest.raises(NetworkError, match="redirect"):
        _NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://evil.test/")


def test_refuse_restricted_default_opener_does_not_follow_redirects(monkeypatch):
    from franky import jira

    calls = []

    def fake(req, timeout=None):
        calls.append(timeout)
        return _make_opener(_issue(security=None))(req, timeout=timeout)

    monkeypatch.setattr(jira, "_no_redirect_open", fake)
    assert fetch_jira_issue("FOO-1", _GOOD_ENV, refuse_restricted=True, timeout=5) == "[FOO-1] S"
    assert calls == [5]
