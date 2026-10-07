"""`franky connect jira` (OAuth login, refresh, lock) and the private-repo-gated Atlassian tools."""

import base64
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.parse
from http.server import HTTPServer
from urllib.request import urlopen

import pytest
from click.testing import CliRunner

from franky import atlassian, cli, egress
from franky.atlassian import store_path as real_store_path  # captured before conftest patches it
from franky.config import load_config
from franky.container import build_docker_argv
from franky.idempotency import repo_is_private
from franky.jira import jira_secret_strings
from franky.prompt import ATLASSIAN_TOOLS_BLOCK, build_review_pr_prompt
from franky.result import ConfigError, NetworkError

PRM = "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"
ISSUER = "https://auth.atlassian.com/ISSUER"
META = "https://auth.atlassian.com/.well-known/oauth-authorization-server/ISSUER"
META_ISSUER_FORM = ISSUER + "/.well-known/oauth-authorization-server"
REG = ISSUER + "/dcr/register"
TOKEN = "https://auth.atlassian.com/oauth/token"
REVOKE = "https://auth.atlassian.com/oauth/revoke"
AUTHZ = "https://auth.atlassian.com/authorize"
META_BODY = {
    "authorization_endpoint": AUTHZ,
    "token_endpoint": TOKEN,
    "registration_endpoint": REG,
    "revocation_endpoint": REVOKE,
}
T0 = 10_000.0


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "x", {}, None)


class Net:
    """Fake opener: routes by URL, records every request."""

    def __init__(self, **overrides):
        self.routes = {
            PRM: {"authorization_servers": [ISSUER]},
            META: dict(META_BODY),
            REG: {"client_id": "cid"},
            TOKEN: {
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "scope": atlassian.READ_SCOPES,
            },
            REVOKE: {},
        }
        self.routes.update(overrides)
        self.log = []

    def __call__(self, req, timeout=None):
        self.log.append(req)
        out = self.routes[req.full_url]
        if callable(out):
            out = out(req)
        if isinstance(out, Exception):
            raise out
        return _Resp(out if isinstance(out, bytes) else json.dumps(out).encode())

    def calls(self, url):
        return [r for r in self.log if r.full_url == url]


def _form(req):
    return dict(urllib.parse.parse_qsl(req.data.decode()))


def _good_receive(server):
    return {"code": "code-1", "state": server.expected_state}


def _connect(net, **kw):
    lines = []
    opened = []
    kw.setdefault("receive", _good_receive)
    kw.setdefault("open_browser", opened.append)
    atlassian.connect({}, opener=net, echo=lines.append, now=lambda: T0, **kw)
    return lines, opened


def _stored():
    return json.loads(atlassian.store_path({}).read_text())


# ---- store path ----------------------------------------------------------------------------


def test_store_path_sits_beside_the_config_file(tmp_path):
    env = {"FRANKY_CONFIG_FILE": str(tmp_path / "cfg" / "config")}
    assert real_store_path(env) == tmp_path / "cfg" / "atlassian-jira.json"
    assert atlassian.lock_path({}).name == "atlassian-jira.lock"


# ---- discovery -----------------------------------------------------------------------------


def test_discovery_uses_the_rfc8414_form_then_falls_back_to_the_issuer_form():
    net = Net()
    _connect(net)
    assert [r.full_url for r in net.log[:3]] == [PRM, META, REG]

    net = Net(**{META: _http_error(META, 404), META_ISSUER_FORM: dict(META_BODY)})
    _connect(net)
    assert [r.full_url for r in net.log[:4]] == [PRM, META, META_ISSUER_FORM, REG]


def test_discovery_non_404_error_does_not_fall_back():
    net = Net(**{META: _http_error(META, 500)})
    with pytest.raises(NetworkError):
        _connect(net)
    assert META_ISSUER_FORM not in [r.full_url for r in net.log]


@pytest.mark.parametrize(
    "key", ["authorization_endpoint", "token_endpoint", "registration_endpoint"]
)
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/x",
        "http://auth.atlassian.com/x",
        "https://atlassian.com.evil.io/x",
    ],
)
def test_discovery_refuses_non_atlassian_or_non_https_endpoints(key, url):
    net = Net(**{META: {**META_BODY, key: url}})
    with pytest.raises(ConfigError):
        _connect(net)
    assert not net.calls(REG) and not net.calls(TOKEN)
    assert not atlassian.store_path({}).exists()


def test_discovery_refuses_a_foreign_issuer_and_a_foreign_revocation_endpoint():
    with pytest.raises(ConfigError):
        _connect(Net(**{PRM: {"authorization_servers": ["https://evil.example.com/i"]}}))
    with pytest.raises(ConfigError):
        _connect(Net(**{META: {**META_BODY, "revocation_endpoint": "https://evil.example.com/r"}}))


# ---- registration, authorize URL, PKCE -----------------------------------------------------


def test_registration_body_and_loopback_redirect():
    net = Net()
    seen = {}
    _connect(net, receive=lambda s: (seen.update(port=s.server_port), _good_receive(s))[1])
    (req,) = net.calls(REG)
    body = json.loads(req.data)
    assert body == {
        "client_name": "franky",
        "redirect_uris": [f"http://127.0.0.1:{seen['port']}/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": atlassian.READ_SCOPES,
    }
    assert req.get_header("Content-type") == "application/json"


def test_authorize_url_pkce_and_printed_even_when_the_browser_opens():
    net = Net()
    lines, opened = _connect(net)
    url = next(ln for ln in lines if ln.startswith("Open") or AUTHZ in ln).split("\n")[-1]
    assert opened == [url]  # printed AND opened
    parts = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert url.startswith(AUTHZ + "?")
    assert q["response_type"] == "code" and q["client_id"] == "cid"
    assert q["scope"] == atlassian.READ_SCOPES and q["code_challenge_method"] == "S256"
    assert q["resource"] == "https://mcp.atlassian.com/v2/mcp"
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith(
        "/callback"
    )
    (exchange,) = net.calls(TOKEN)
    verifier = _form(exchange)["code_verifier"]
    want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == want.decode() and "=" not in q["code_challenge"]
    assert len(verifier) >= 43


def test_no_browser_prints_the_url_and_takes_the_pasted_redirect():
    net = Net()
    lines = []
    opened = []

    def prompt(text):
        url = next(ln for ln in lines if AUTHZ in ln).split("\n")[-1]
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
        return f"  http://127.0.0.1:1/callback?code=pasted&state={state}  "

    def no_receive(server):
        raise AssertionError("no loopback wait in --no-browser mode")

    atlassian.connect(
        {},
        opener=net,
        echo=lines.append,
        open_browser=opened.append,
        prompt=prompt,
        receive=no_receive,
        no_browser=True,
        now=lambda: T0,
    )
    assert not opened and any(AUTHZ in ln for ln in lines)
    assert _form(net.calls(TOKEN)[0])["code"] == "pasted"
    assert _stored()["refresh_token"] == "rt-1"


@pytest.mark.parametrize(
    "params",
    [
        {"code": "c", "state": "WRONG"},
        {"error": "access_denied", "state": "x"},
        {"state": "x"},
        {},
    ],
)
def test_wrong_state_error_or_missing_code_stores_nothing(params):
    net = Net()
    with pytest.raises(ConfigError):
        _connect(net, receive=lambda s: params)
    assert not net.calls(TOKEN) and not atlassian.store_path({}).exists()


def test_atlassian_error_page_shows_its_error_code():
    net = Net()
    page = {"error": "invalid_request", "error_description": "Incorrect request parameters"}
    with pytest.raises(ConfigError, match="login failed: invalid_request") as exc:
        _connect(net, receive=lambda s: page)
    assert "Rovo MCP server > Domain settings" in str(exc.value)
    assert not net.calls(TOKEN) and not atlassian.store_path({}).exists()


def test_pasted_error_page_keeps_the_existing_connection():
    _connect(Net())
    before = _stored()
    net = Net()
    page = "https://id.atlassian.com/error?error=invalid_request&error_description=Incorrect"
    with pytest.raises(ConfigError, match="login failed: invalid_request"):
        _connect(net, prompt=lambda text: page, no_browser=True)
    assert not net.calls(TOKEN) and _stored() == before


def test_token_exchange_body_and_client_secret():
    net = Net(**{REG: {"client_id": "cid", "client_secret": "csec"}})
    _connect(net)
    (req,) = net.calls(TOKEN)
    f = _form(req)
    assert f["grant_type"] == "authorization_code" and f["code"] == "code-1"
    assert f["client_id"] == "cid" and f["client_secret"] == "csec"
    assert f["resource"] == "https://mcp.atlassian.com/v2/mcp"
    assert f["redirect_uri"].startswith("http://127.0.0.1:")
    assert req.get_header("Accept") == "application/json"
    assert _stored()["client_secret"] == "csec"


@pytest.mark.parametrize("tok", [{"access_token": "a"}, {"refresh_token": "r"}, {}])
def test_exchange_without_both_tokens_stores_nothing(tok):
    with pytest.raises(ConfigError):
        _connect(Net(**{TOKEN: tok}))
    assert not atlassian.store_path({}).exists()


def test_saved_file_is_0600_has_the_keys_and_nothing_secret_is_printed():
    lines, _ = _connect(Net())
    path = atlassian.store_path({})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rec = _stored()
    assert set(rec) == {
        "version",
        "client_id",
        "client_secret",
        "token_endpoint",
        "revocation_endpoint",
        "refresh_token",
        "access_token",
        "expires_at",
        "scope",
        "connected_at",
        "previous_access_tokens",
    }
    assert rec["expires_at"] == T0 + 3600 and rec["connected_at"] == T0
    assert rec["client_secret"] is None and rec["previous_access_tokens"] == []
    assert "at-1" not in "\n".join(lines) and "rt-1" not in "\n".join(lines)


def test_broad_scope_grant_warns_but_still_connects(capsys):
    _connect(
        Net(**{TOKEN: {"access_token": "a", "refresh_token": "r", "scope": "read:x write:jira:x"}})
    )
    assert "WARNING" in capsys.readouterr().err and atlassian.store_path({}).exists()


def test_read_scope_grant_does_not_warn(capsys):
    _connect(Net())
    assert capsys.readouterr().err == ""


# ---- loopback handler ----------------------------------------------------------------------


def _loopback(path, state="good"):
    server = HTTPServer(("127.0.0.1", 0), atlassian._Callback)
    server.params, server.expected_state = {}, state
    out = {}

    def get():
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as r:
                out["status"], out["body"] = r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            out["status"], out["body"] = exc.code, exc.read().decode()

    t = threading.Thread(target=get)
    t.start()
    params = atlassian._receive(server)
    t.join(5)
    server.server_close()
    return params, out


def test_loopback_handler_good_state():
    params, out = _loopback("/callback?code=abc&state=good")
    assert params == {"code": "abc", "state": "good"}
    assert out == {"status": 200, "body": "franky: connected, you can close this tab"}


def test_loopback_ignores_other_paths_until_the_callback():
    server = HTTPServer(("127.0.0.1", 0), atlassian._Callback)
    server.params, server.expected_state = {}, "s"
    results = []

    def get(path):
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)
        except urllib.error.HTTPError as exc:
            results.append(exc.code)

    def run():
        get("/favicon.ico")
        get("/callback?code=c&state=s")

    t = threading.Thread(target=run)
    t.start()
    assert atlassian._receive(server)["code"] == "c"
    t.join(5)
    server.server_close()
    assert results == [404]


# ---- access_token --------------------------------------------------------------------------


def _no_network(req, timeout=None):
    raise AssertionError("no network expected")


def test_access_token_cached_when_it_has_enough_life_left(connect_atlassian):
    connect_atlassian(expires_at=T0 + 3000)
    assert atlassian.access_token({}, now=lambda: T0, opener=_no_network).token == "at-0"


def test_access_token_refreshes_and_persists_the_rotation(connect_atlassian):
    connect_atlassian(expires_at=T0 + 100, client_secret="csec")
    net = Net(**{TOKEN: {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 1800}})
    assert atlassian.access_token({}, now=lambda: T0, opener=net).token == "at-1"
    f = _form(net.calls(TOKEN)[0])
    assert f == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-0",
        "client_id": "cid",
        "resource": "https://mcp.atlassian.com/v2/mcp",
        "client_secret": "csec",
    }
    rec = _stored()
    assert rec["access_token"] == "at-1" and rec["refresh_token"] == "rt-1"
    assert rec["expires_at"] == T0 + 1800 and rec["previous_access_tokens"] == ["at-0"]
    assert stat.S_IMODE(atlassian.store_path({}).stat().st_mode) == 0o600


def test_refresh_without_a_new_refresh_token_keeps_the_old_one(connect_atlassian):
    connect_atlassian(expires_at=T0)
    net = Net(**{TOKEN: {"access_token": "at-1", "expires_in": 3600}})
    assert atlassian.access_token({}, now=lambda: T0, opener=net).token == "at-1"
    assert _stored()["refresh_token"] == "rt-0"


def test_previous_access_tokens_are_capped_at_five(connect_atlassian):
    connect_atlassian(expires_at=T0, previous_access_tokens=["p1", "p2", "p3", "p4", "p5"])
    net = Net(**{TOKEN: {"access_token": "at-1", "refresh_token": "rt-1"}})
    atlassian.access_token({}, now=lambda: T0, opener=net)
    assert _stored()["previous_access_tokens"] == ["p2", "p3", "p4", "p5", "at-0"]


@pytest.mark.parametrize(
    "outcome",
    [
        _http_error(TOKEN, 400),
        _http_error(TOKEN, 401),
        OSError("down"),
        b"not json",
        b"[]",
        {},
        {"access_token": ""},
    ],
)
def test_refresh_failure_returns_none_and_leaves_the_file_untouched(connect_atlassian, outcome):
    connect_atlassian(expires_at=T0)
    before = atlassian.store_path({}).read_bytes()
    net = Net(**{TOKEN: outcome})
    assert atlassian.access_token({}, now=lambda: T0, opener=net).token is None
    assert atlassian.store_path({}).read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("refresh_token"),
        lambda r: r.update(version=2),
        lambda r: r.update(access_token=""),
        lambda r: r.update(expires_at="soon"),
        lambda r: r.update(previous_access_tokens="x"),
        lambda r: r.update(token_endpoint="http://auth.atlassian.com/oauth/token"),
        lambda r: r.update(token_endpoint="https://evil.example.com/oauth/token"),
        lambda r: r.update(client_secret=5),
    ],
)
def test_missing_or_malformed_connection_is_not_connected(connect_atlassian, mutate):
    rec = connect_atlassian()
    mutate(rec)
    atlassian.store_path({}).write_text(json.dumps(rec))
    assert atlassian.access_token({}, now=lambda: T0, opener=_no_network).token is None
    assert atlassian.stored_secrets({}) == []
    assert atlassian.status({}).startswith("not connected")


def test_garbage_and_missing_file_are_not_connected():
    assert atlassian.access_token({}, opener=_no_network).reason == "missing"
    path = atlassian.store_path({})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    got = atlassian.access_token({}, opener=_no_network)
    assert got.token is None and got.reason == "missing"


@pytest.mark.parametrize(
    "outcome, reason",
    [
        (_http_error(TOKEN, 400), "expired"),
        (_http_error(TOKEN, 401), "expired"),
        (OSError("down"), "network"),
        ({}, "network"),
    ],
)
def test_refresh_failure_carries_a_reason_code(connect_atlassian, outcome, reason):
    connect_atlassian(expires_at=T0)
    got = atlassian.access_token({}, now=lambda: T0, opener=Net(**{TOKEN: outcome}))
    assert got.token is None and got.reason == reason


def test_a_broad_grant_is_flagged_on_the_token(connect_atlassian):
    connect_atlassian(expires_at=T0)
    net = Net(**{TOKEN: {"access_token": "a", "scope": "read:x write:jira:x", "expires_in": 3600}})
    got = atlassian.access_token({}, now=lambda: T0, opener=net, echo=lambda m: None)
    assert got.token == "a" and got.broad


def test_two_threads_with_an_expired_token_cause_exactly_one_refresh(connect_atlassian):
    connect_atlassian(expires_at=T0)
    count = []

    def slow(req):
        count.append(1)
        time.sleep(0.3)
        return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}

    net = Net(**{TOKEN: slow})
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(atlassian.access_token({}, now=lambda: T0, opener=net))
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert [r.token for r in results] == ["at-1", "at-1"] and len(count) == 1


def _blocked_while_locked(fn):
    """Run `fn` in a thread while this thread holds the lock; it must wait for the release."""
    done = threading.Event()
    t = threading.Thread(target=lambda: (fn(), done.set()))
    with atlassian._locked({}):
        t.start()
        assert not done.wait(0.4), "ran while the lock was held"
    assert done.wait(5)
    t.join(5)


def test_a_held_lock_blocks_refresh_and_disconnect(connect_atlassian):
    connect_atlassian(expires_at=T0)
    net = Net()
    _blocked_while_locked(lambda: atlassian.access_token({}, now=lambda: T0, opener=net))
    assert len(net.calls(TOKEN)) == 1
    _blocked_while_locked(lambda: atlassian.disconnect({}, opener=net))
    assert not atlassian.store_path({}).exists()


def test_a_held_lock_blocks_the_connect_save():
    net = Net()
    path = atlassian.store_path({})

    def run():
        atlassian.connect(
            {}, opener=net, echo=lambda m: None, open_browser=lambda u: None, receive=_good_receive
        )

    done = threading.Event()
    t = threading.Thread(target=lambda: (run(), done.set()))
    with atlassian._locked({}):
        t.start()
        assert not done.wait(0.6) and not path.exists()
    assert done.wait(5) and path.exists()
    t.join(5)


# ---- status / disconnect / secrets ---------------------------------------------------------


def test_status_is_secret_free_and_needs_no_network(connect_atlassian):
    assert atlassian.status({}) == "not connected"
    connect_atlassian(expires_at=T0 + 1800, client_secret="csec")
    text = atlassian.status({}, now=lambda: T0)
    assert text.startswith("connected, access token valid for 30 min, scopes offline_access ")
    assert "connected 1970-01-01" in text
    assert not any(s in text for s in ("rt-0", "at-0", "csec"))
    assert "expired" in atlassian.status({}, now=lambda: T0 + 5000)


def test_disconnect_revokes_then_unlinks(connect_atlassian):
    connect_atlassian(client_secret="csec")
    net = Net()
    assert atlassian.disconnect({}, opener=net) is True
    f = _form(net.calls(REVOKE)[0])
    assert f == {"token": "rt-0", "client_id": "cid", "client_secret": "csec"}
    assert not atlassian.store_path({}).exists()
    assert atlassian.disconnect({}, opener=net) is False


def test_disconnect_ignores_a_failed_revocation(connect_atlassian):
    connect_atlassian()
    assert atlassian.disconnect({}, opener=Net(**{REVOKE: OSError("down")})) is True
    assert not atlassian.store_path({}).exists()


def test_stored_secrets_cover_every_credential_form(connect_atlassian):
    assert atlassian.stored_secrets({}) == []
    connect_atlassian(client_secret="csec", previous_access_tokens=["p1", "p2"])
    got = set(atlassian.stored_secrets({}))
    want = {"rt-0", "at-0", "p1", "p2", "csec", "Bearer at-0", "Bearer p1", "Bearer p2"}
    assert want <= got


def _kill_env(monkeypatch):
    monkeypatch.setattr(cli, "load_config_file", lambda env: None)
    monkeypatch.setattr(cli, "_profile_secrets", lambda env: [])


def test_kill_secrets_include_previous_access_tokens_after_a_rotation(
    monkeypatch, connect_atlassian
):
    connect_atlassian(expires_at=T0)
    _kill_env(monkeypatch)
    atlassian.access_token({}, now=lambda: T0, opener=Net())
    secrets, _ = cli._kill_secrets("claude")
    assert {"at-1", "at-0", "Bearer at-0", "rt-1"} <= set(secrets)
    # Still scrubbed when the config cannot load.
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    secrets, complete = cli._kill_secrets("claude")
    assert {"at-0", "rt-1"} <= set(secrets) and not complete
    assert {"at-0", "rt-1"} <= set(cli.cfg_secrets_safe())


# ---- jira_secret_strings / config ---------------------------------------------------------


def test_jira_secret_strings_cover_the_classic_forms():
    b64 = base64.b64encode(b"dev@example.com:jira-tok").decode()
    env = {"JIRA_EMAIL": "dev@example.com", "JIRA_API_TOKEN": " jira-tok "}
    assert jira_secret_strings(env) == ["dev@example.com", "jira-tok", b64, f"Basic {b64}"]
    assert jira_secret_strings({}) == []
    assert jira_secret_strings({"JIRA_API_TOKEN": "t"}) == ["t"]


def _env(**extra):
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-fake",
        "JIRA_BASE_URL": "https://acme.atlassian.net",
        "JIRA_EMAIL": "dev@example.com",
        "JIRA_API_TOKEN": "jira-tok",
    }
    env.update(extra)
    return env


def test_config_enable_atlassian_adds_only_the_header_and_the_mcp_host():
    cfg = load_config("claude", _env())
    assert "jira-tok" in cfg.secret_values() and not cfg.atlassian_tools  # redacted tools off
    cfg.enable_atlassian("tok")
    assert cfg.passthrough_env["FRANKY_ATLASSIAN_MCP_HEADER"] == "Bearer tok"
    assert not any(k.startswith("JIRA_") for k in cfg.passthrough_env)
    assert cfg.extra_allowed_domains == ["mcp.atlassian.com"] and cfg.atlassian_tools
    assert {"tok", "Bearer tok", "jira-tok"} <= set(cfg.secret_values())
    cfg.enable_atlassian("tok")
    assert cfg.extra_allowed_domains == ["mcp.atlassian.com"]


def test_egress_gains_mcp_atlassian_com_and_no_other_atlassian_host():
    cfg = load_config("claude", _env())
    base = set(egress.build_allowlist(cfg.engine, cfg.passthrough_env, cfg.extra_allowed_domains))
    cfg.enable_atlassian("tok")
    hosts = set(egress.build_allowlist(cfg.engine, cfg.passthrough_env, cfg.extra_allowed_domains))
    assert hosts - base == {"mcp.atlassian.com"}


# ---- builders ------------------------------------------------------------------------------


def test_builders_are_deterministic_and_value_free():
    assert atlassian.claude_mcp_json() == atlassian.claude_mcp_json()
    server = json.loads(atlassian.claude_mcp_json())["mcpServers"]["atlassian"]
    assert server == {
        "type": "http",
        "url": "https://mcp.atlassian.com/v2/mcp",
        "headers": {"Authorization": "${FRANKY_ATLASSIAN_MCP_HEADER}"},
    }
    names = atlassian.claude_disallowed().split(",")
    assert names == ["mcp__atlassian__" + n for n in atlassian.CLAUDE_DENY_PATTERNS]
    codex = atlassian.codex_override()
    assert codex.startswith("mcp_servers.atlassian={ url = ")
    assert 'env_http_headers = { Authorization = "FRANKY_ATLASSIAN_MCP_HEADER" }' in codex
    assert f"enabled_tools = {json.dumps(list(atlassian.READ_TOOLS))}" in codex
    assert not any(json.dumps(n) in codex for n in atlassian.WRITE_TOOLS)


# ---- private-repo gate ---------------------------------------------------------------------


class _GhResp:
    def __init__(self, body, status=200):
        self.status, self._body = status, body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gh(payload, status=200):
    def opener(req, timeout=None):
        assert req.full_url == "https://api.github.com/repos/me/repo"
        assert req.get_header("Authorization") == "Bearer ghp_fake"
        return _GhResp(json.dumps(payload).encode(), status)

    return opener


def test_repo_is_private_only_for_literal_true():
    env = {"GH_TOKEN": "ghp_fake"}
    assert repo_is_private("me/repo", env, opener=_gh({"private": True}))
    assert not repo_is_private("me/repo", env, opener=_gh({"private": False}))
    assert not repo_is_private("me/repo", env, opener=_gh({"private": "true"}))
    assert not repo_is_private("me/repo", env, opener=_gh({}))
    assert not repo_is_private("me/repo", env, opener=_gh({"private": True}, status=404))
    assert not repo_is_private("me/repo", env, opener=_gh([]))

    def boom(req, timeout=None):
        raise OSError("down")

    assert not repo_is_private("me/repo", env, opener=boom)


@pytest.fixture
def jira_env(monkeypatch):
    for k, v in _env().items():
        monkeypatch.setenv(k, v)


def _no_lookup(*a, **k):
    raise AssertionError("no GitHub lookup expected")


def test_enable_silently_off_without_a_repo(monkeypatch, capsys, jira_env, connect_atlassian):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", _no_lookup)
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, None, [])
    assert not cfg.atlassian_tools and capsys.readouterr().err == ""


def test_enable_without_a_connection_hints_only_when_jira_is_configured(
    monkeypatch, capsys, jira_env
):
    monkeypatch.setattr(cli, "repo_is_private", _no_lookup)  # no file -> no GitHub lookup
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [])
    assert not cfg.atlassian_tools
    assert "run `franky connect jira`" in capsys.readouterr().err
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(k)
    cli._enable_atlassian(cfg, "me/repo", [])
    assert capsys.readouterr().err == ""


def test_enable_off_for_a_repo_that_is_not_confirmed_private(
    monkeypatch, capsys, connect_atlassian
):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", lambda *a, **k: False)
    monkeypatch.setattr(cli.atlassian, "access_token", _no_lookup)  # no refresh either
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [])
    assert not cfg.atlassian_tools
    assert "repo is not confirmed private" in capsys.readouterr().err


def test_enable_off_when_the_connection_is_expired_or_revoked(
    monkeypatch, capsys, connect_atlassian
):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", lambda *a, **k: True)
    monkeypatch.setattr(
        cli.atlassian,
        "access_token",
        lambda env, **k: atlassian.Token(hint=atlassian.HINT_REJECTED),
    )
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [])
    assert not cfg.atlassian_tools
    err = capsys.readouterr().err
    assert "connection expired or revoked" in err and "franky connect jira" in err


def test_enable_success_adds_only_the_header_and_the_mcp_host(monkeypatch, connect_atlassian):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", lambda *a, **k: True)
    monkeypatch.setattr(
        cli.atlassian, "access_token", lambda env, **k: atlassian.Token("tok", expires_at=T0 + 3600)
    )
    cfg = load_config("claude", _env())
    before = set(cfg.passthrough_env)
    secrets: list[str] = []
    cli._enable_atlassian(cfg, "me/repo", secrets)
    assert cfg.atlassian_tools
    assert set(cfg.passthrough_env) - before == {"FRANKY_ATLASSIAN_MCP_HEADER"}
    assert not any(k.startswith("JIRA_") for k in cfg.passthrough_env)
    assert cfg.extra_allowed_domains == ["mcp.atlassian.com"]
    assert {"tok", "Bearer tok"} <= set(secrets)


def test_enable_reuses_a_known_privacy_answer(monkeypatch, connect_atlassian):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", _no_lookup)
    monkeypatch.setattr(
        cli.atlassian, "access_token", lambda env, **k: atlassian.Token("tok", expires_at=T0 + 3600)
    )
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [], private=True)
    assert cfg.atlassian_tools
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [], private=False)
    assert not cfg.atlassian_tools


# ---- argv and prompt per engine ------------------------------------------------------------


def _run(monkeypatch, engine, *, on, profile_path=None, private_prompt=False):
    cfg = load_config(engine, _env(CODEX_API_KEY="codex-fake"))
    if on:
        cfg.enable_atlassian("tok")
    cfg.claude_mcp_config_path = profile_path
    seen = {}

    def fake(cfg_, argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return 0, ""

    monkeypatch.setattr(cli, "run_in_container", fake)
    cli._run_pass(cfg, "do it", "img", "proxy", private_prompt=private_prompt)
    return seen


def test_claude_inline_config_one_strict_flag_and_the_write_tool_deny_list(monkeypatch):
    argv = _run(monkeypatch, "claude", on=True)["argv"]
    i = argv.index("--mcp-config")
    assert argv[i + 1] == atlassian.claude_mcp_json()
    assert argv[i + 2] == "--strict-mcp-config" and argv.count("--strict-mcp-config") == 1
    deny = argv[argv.index("--disallowedTools") + 1]
    assert deny.split(",") == [f"mcp__atlassian__{p}" for p in atlassian.CLAUDE_DENY_PATTERNS]
    assert ATLASSIAN_TOOLS_BLOCK in " ".join(argv)


def test_claude_profile_path_then_inline_then_one_strict(monkeypatch):
    argv = _run(monkeypatch, "claude", on=True, profile_path="/home/franky/p.json")["argv"]
    i = argv.index("--mcp-config")
    assert argv[i : i + 4] == [
        "--mcp-config",
        "/home/franky/p.json",
        atlassian.claude_mcp_json(),
        "--strict-mcp-config",
    ]
    assert argv.count("--strict-mcp-config") == 1


def test_claude_tools_off_is_unchanged(monkeypatch):
    argv = _run(monkeypatch, "claude", on=False)["argv"]
    assert "--mcp-config" not in argv and "--disallowedTools" not in argv
    assert "atlassian" not in " ".join(argv)
    argv = _run(monkeypatch, "claude", on=False, profile_path="/home/franky/p.json")["argv"]
    assert argv[argv.index("--mcp-config") + 1 :][:2] == [
        "/home/franky/p.json",
        "--strict-mcp-config",
    ]


def test_codex_last_override_is_the_atlassian_server(monkeypatch):
    argv = _run(monkeypatch, "codex", on=True)["argv"]
    assert argv[-2] == "-c" and argv[-1] == atlassian.codex_override()
    assert "env_http_headers" in argv[-1] and "enabled_tools" in argv[-1]
    assert "disabled_tools" not in argv[-1]
    assert "tok" not in argv[-1].replace("tools", "")  # the value never rides the argv


@pytest.mark.parametrize("engine", ["pi", "opencode"])
def test_pi_and_opencode_are_unchanged(monkeypatch, engine):
    env = _env(
        OPENROUTER_API_KEY="sk-or", FRANKY_MODEL="moonshotai/kimi-k3", MOONSHOT_API_KEY="sk-m"
    )
    runs = []
    for flag in (True, False):
        cfg = load_config(engine, env)
        if flag:
            cfg.enable_atlassian("tok")
        seen = {}
        monkeypatch.setattr(
            cli, "run_in_container", lambda c, a, **k: (seen.update(a=a), (0, ""))[1]
        )
        cli._run_pass(cfg, "do it", "img", "proxy")
        runs.append(seen["a"])
    strip = lambda argv: [a.replace(ATLASSIAN_TOOLS_BLOCK, "") for a in argv]  # noqa: E731
    assert strip(runs[0]) == runs[1]
    assert "--mcp-config" not in runs[0] and not any(a.startswith("mcp_servers") for a in runs[0])


def test_private_prompt_block_rides_the_tar_not_argv(monkeypatch):
    seen = _run(monkeypatch, "claude", on=True, private_prompt=True)
    assert "Atlassian (JIRA" not in " ".join(seen["argv"])
    with tarfile.open(fileobj=io.BytesIO(seen["kw"]["private_prompt_tar"]), mode="r:gz") as t:
        assert ATLASSIAN_TOOLS_BLOCK in t.extractfile("franky-private-prompt").read().decode()


def test_prompt_block_wording():
    assert "READ ONLY" in ATLASSIAN_TOOLS_BLOCK and "`atlassian` MCP tools" in ATLASSIAN_TOOLS_BLOCK
    assert "FRANKY_TICKET" in ATLASSIAN_TOOLS_BLOCK and "security level" in ATLASSIAN_TOOLS_BLOCK
    assert "acli" not in ATLASSIAN_TOOLS_BLOCK


def test_review_prompt_line_flips_with_jira_tools():
    args = ("me/repo", "https://github.com/me/repo/pull/1", "", "n0nce")
    off = build_review_pr_prompt(*args)
    on = build_review_pr_prompt(*args, jira_tools=True)
    assert "JIRA is not reachable" in off and "Atlassian tools" not in off
    assert "JIRA is not reachable" not in on
    assert "Fetch linked tickets with the Atlassian tools" in on


def test_container_argv_carries_the_header_name_only():
    cfg = load_config("claude", _env())
    cfg.enable_atlassian("tok-s3cret")
    argv = build_docker_argv("franky", cfg.passthrough_env, ["claude"])
    assert "FRANKY_ATLASSIAN_MCP_HEADER" in argv
    assert not any(a.startswith("JIRA_") for a in argv)
    assert "tok-s3cret" not in " ".join(argv)
    assert "JIRA_API_TOKEN" not in argv


# ---- CLI -----------------------------------------------------------------------------------


def test_cli_connect_jira_status_connect_and_disconnect(monkeypatch, connect_atlassian):
    runner = CliRunner()
    res = runner.invoke(cli.main, ["connect", "jira", "--status"])
    assert res.exit_code == 0 and res.output.strip() == "not connected"

    seen = {}
    monkeypatch.setattr(
        atlassian,
        "connect",
        lambda env, **kw: (seen.update(kw), connect_atlassian())[1],
    )
    res = runner.invoke(cli.main, ["connect", "jira", "--no-browser"])
    assert res.exit_code == 0 and seen == {"no_browser": True}
    assert "connected" in res.output and "rt-0" not in res.output and "at-0" not in res.output

    monkeypatch.setattr(atlassian, "disconnect", lambda env: True)
    res = runner.invoke(cli.main, ["connect", "jira", "--disconnect"])
    assert res.exit_code == 0 and "disconnected" in res.output


def test_cli_connect_failure_uses_the_error_exit_code(monkeypatch):
    def boom(env, **kw):
        raise ConfigError("Atlassian login failed: access_denied")

    monkeypatch.setattr(atlassian, "connect", boom)
    res = CliRunner().invoke(cli.main, ["connect", "jira"])
    assert res.exit_code == 3 and "access_denied" in res.output


def test_config_init_points_at_connect_jira_without_the_admin_step(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg" / "config"
    monkeypatch.setenv("FRANKY_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    answers = "claude\nme/repo\nghp_x\nclaude-x\ny\nhttps://a.atlassian.net\nme@x.com\njtok\nn\n"
    res = CliRunner().invoke(cli.main, ["config", "init"], input=answers)
    assert "franky connect jira" in res.output
    assert "admin" not in res.output.lower() and "ATLASSIAN_MCP_API_TOKEN" not in res.output
    assert os.path.exists(cfg)


# ---- hardening: loopback, lock, refresh outcomes, reconnect, status, gates ----------------


def _serve(server, paths, results):
    def run():
        for path in paths:
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as r:
                    results.append(r.status)
            except urllib.error.HTTPError as exc:
                results.append(exc.code)

    t = threading.Thread(target=run)
    t.start()
    return t


def test_loopback_wrong_state_does_not_end_the_wait_and_a_good_request_succeeds():
    server = HTTPServer(("127.0.0.1", 0), atlassian._Callback)
    server.params, server.expected_state = {}, "good"
    results = []
    t = _serve(server, ["/callback?code=x&state=evil", "/callback?code=c&state=good"], results)
    params = atlassian._receive(server)
    t.join(5)
    server.server_close()
    assert results == [400, 200] and params["code"] == "c"


def test_loopback_silent_connection_cannot_stall_past_the_handler_timeout(monkeypatch):
    import socket

    monkeypatch.setattr(atlassian._Callback, "timeout", 0.3)
    server = HTTPServer(("127.0.0.1", 0), atlassian._Callback)
    server.params, server.expected_state = {}, "good"
    silent = socket.create_connection(("127.0.0.1", server.server_port))
    results = []
    t = _serve(server, ["/callback?code=c&state=good"], results)
    start = time.monotonic()
    params = atlassian._receive(server)
    assert params["code"] == "c" and time.monotonic() - start < 5
    t.join(5)
    silent.close()
    server.server_close()


def test_error_with_escape_characters_is_not_echoed():
    evil = "\x1b[31mboom\x1b[0m"
    with pytest.raises(ConfigError) as exc:
        _connect(Net(), receive=lambda s: {"error": evil, "state": s.expected_state})
    assert "\x1b" not in str(exc.value) and "boom" not in str(exc.value)
    with pytest.raises(ConfigError, match="access_denied"):
        _connect(Net(), receive=lambda s: {"error": "access_denied", "state": s.expected_state})


def test_lock_wait_is_capped(connect_atlassian, monkeypatch):
    connect_atlassian(expires_at=T0)
    monkeypatch.setattr(atlassian, "_LOCK_WAIT", 0.2)
    with atlassian._locked({}):
        got = []
        t = threading.Thread(
            target=lambda: got.append(atlassian.access_token({}, now=lambda: T0, opener=Net()))
        )
        t.start()
        t.join(5)
        assert got[0].token is None and got[0].hint == atlassian.HINT_BUSY
        errs = []

        def dis():
            try:
                atlassian.disconnect({}, opener=Net())
            except ConfigError as e:
                errs.append(e)

        t = threading.Thread(target=dis)
        t.start()
        t.join(5)
        assert len(errs) == 1
        with pytest.raises(ConfigError):
            # connect is refused too (the same thread cannot take a second flock handle)
            _connect(Net())


@pytest.mark.parametrize("code", [400, 401])
def test_refresh_rejected_has_the_expired_hint(connect_atlassian, code):
    connect_atlassian(expires_at=T0)
    got = atlassian.access_token(
        {}, now=lambda: T0, opener=Net(**{TOKEN: _http_error(TOKEN, code)})
    )
    assert got.token is None and got.hint == atlassian.HINT_REJECTED
    assert "expired or revoked" in got.hint and "franky connect jira" in got.hint


@pytest.mark.parametrize("outcome", [OSError("down"), _http_error(TOKEN, 503), b"nope"])
def test_refresh_network_failure_has_the_network_hint(connect_atlassian, outcome):
    connect_atlassian(expires_at=T0)
    got = atlassian.access_token({}, now=lambda: T0, opener=Net(**{TOKEN: outcome}))
    assert got.token is None and got.hint == "Atlassian tools off (token refresh failed: network)"


def test_refresh_ok_but_save_failed_still_returns_the_token_with_a_warning(
    connect_atlassian, monkeypatch
):
    connect_atlassian(expires_at=T0)
    monkeypatch.setattr(atlassian, "_save", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    got = atlassian.access_token({}, now=lambda: T0, opener=Net())
    assert got.token == "at-1" and "could not save the refreshed Atlassian token" in got.warning
    assert got.expires_at == T0 + 3600


def test_connect_without_a_scope_stores_unknown_and_warns(capsys):
    _connect(Net(**{TOKEN: {"access_token": "a", "refresh_token": "r"}}))
    assert _stored()["scope"] == "unknown"
    assert "granted scopes unknown; Franky still denies write tools" in capsys.readouterr().err
    assert "scopes unknown" in atlassian.status({}, now=lambda: T0)


def test_reconnect_carries_old_tokens_and_revokes_the_old_refresh_token(connect_atlassian):
    connect_atlassian(previous_access_tokens=["p1"])
    net = Net()
    _connect(net)
    rec = _stored()
    assert rec["refresh_token"] == "rt-1" and rec["previous_access_tokens"] == ["p1", "at-0"]
    assert _form(net.calls(REVOKE)[0])["token"] == "rt-0"
    # A failing revoke never blocks the reconnect; the cap holds.
    connect_atlassian(previous_access_tokens=["a", "b", "c", "d", "e"])
    _connect(Net(**{REVOKE: OSError("down")}))
    assert _stored()["previous_access_tokens"] == ["b", "c", "d", "e", "at-0"]


@pytest.mark.parametrize("bad", ["yesterday", None, True])
def test_status_flags_a_malformed_connected_at(connect_atlassian, bad):
    rec = connect_atlassian()
    rec["connected_at"] = bad
    atlassian.store_path({}).write_text(json.dumps(rec))
    assert atlassian.status({}) == "not connected (stored connection is malformed)"


def test_claude_patterns_and_codex_allowlist_cover_every_known_write_tool():
    import fnmatch

    pats = atlassian.CLAUDE_DENY_PATTERNS
    denied = lambda n: any(fnmatch.fnmatchcase(n, p) for p in pats)  # noqa: E731
    for name in atlassian.WRITE_TOOLS:
        assert denied(name) or name not in atlassian.READ_TOOLS, name
        assert name not in atlassian.READ_TOOLS, name
    for name in atlassian.READ_TOOLS:
        assert not denied(name), name
    assert pats == (
        "create*",
        "edit*",
        "update*",
        "add*",
        "transition*",
        "delete*",
        "executeWrite",
        "executeDestructive",
    )
    assert set(atlassian.READ_TOOLS) >= {"getJiraIssue", "discover", "executeRead", "search"}
    assert all(denied(n) for n in atlassian.WRITE_TOOLS)


@pytest.mark.parametrize("engine", ["pi", "opencode"])
def test_enable_is_off_for_other_engines_before_any_lookup(monkeypatch, capsys, engine):
    monkeypatch.setattr(cli, "repo_is_private", _no_lookup)
    monkeypatch.setattr(cli.atlassian, "access_token", _no_lookup)
    cfg = load_config(
        engine,
        _env(
            OPENROUTER_API_KEY="sk-or", FRANKY_MODEL="moonshotai/kimi-k3", MOONSHOT_API_KEY="sk-m"
        ),
    )
    cli._enable_atlassian(cfg, "me/repo", [])
    assert not cfg.atlassian_tools and capsys.readouterr().err == ""


def test_enable_prints_the_rejected_busy_and_save_lines(monkeypatch, capsys, connect_atlassian):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", lambda *a, **k: True)
    for tok, text in [
        (atlassian.Token(hint=atlassian.HINT_NETWORK), "token refresh failed: network"),
        (atlassian.Token(hint=atlassian.HINT_BUSY), "connection busy"),
    ]:
        monkeypatch.setattr(cli.atlassian, "access_token", lambda env, t=tok, **k: t)
        cfg = load_config("claude", _env())
        cli._enable_atlassian(cfg, "me/repo", [])
        assert not cfg.atlassian_tools and text in capsys.readouterr().err
    tok = atlassian.Token("tok", warning=atlassian.WARN_SAVE, expires_at=3 * 3600 + 5 * 60)
    monkeypatch.setattr(cli.atlassian, "access_token", lambda env, **k: tok)
    cfg = load_config("claude", _env())
    cli._enable_atlassian(cfg, "me/repo", [])
    err = capsys.readouterr().err
    assert cfg.atlassian_tools and "could not save the refreshed Atlassian token" in err
    assert "Atlassian tools on (token valid until 03:05 UTC)" in err


def test_already_open_build_makes_no_privacy_lookup(monkeypatch, tmp_path, connect_atlassian):
    connect_atlassian()
    monkeypatch.setattr(cli, "repo_is_private", _no_lookup)
    monkeypatch.setattr(cli, "find_open_pr", lambda *a, **k: "https://github.com/me/repo/pull/9")
    env = _env(GH_TOKEN="ghp_x", FRANKY_ALLOWED_REPOS="me/repo", CLAUDE_CODE_OAUTH_TOKEN="c")
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "run_in_container", _no_lookup)
    res = CliRunner().invoke(
        cli.main, ["build", "do it", "--repo", "me/repo", "--engine", "claude", "--json"]
    )
    assert '"already_open"' in res.output, res.output


def test_stored_login_is_in_secret_values_even_with_tools_off(connect_atlassian):
    connect_atlassian(refresh_token="rt-planted", previous_access_tokens=["prev-planted"])
    cfg = load_config("claude", _env())
    assert not cfg.atlassian_tools
    assert {"rt-planted", "prev-planted", "Bearer prev-planted"} <= set(cfg.secret_values())
