"""Host-side Atlassian OAuth connection (`franky connect jira`) and the in-container MCP wiring.

WHY host-side: the refresh token never enters a container. The host keeps it in
~/.franky/atlassian-jira.json (0600), refreshes a short-lived access token before each task and
passes only that, as `FRANKY_ATLASSIAN_MCP_HEADER`, into a task on a private repo. The OAuth scopes
requested are read and search only; the write tools are denied in the engine config. If Atlassian
grants broader scopes, read-only is not enforced by Atlassian: `--status` shows what was granted.

Flow: protected-resource metadata (RFC 9728) -> authorization-server metadata (RFC 8414) ->
dynamic client registration -> authorization code + PKCE on a loopback redirect. Refresh tokens
rotate, so EVERY credential mutation (connect, refresh, disconnect) runs under one file lock.
Stdlib only; every network call and prompt is injectable so the tests stay hermetic.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import NamedTuple

import click

from .result import ConfigError, NetworkError
from .userconfig import _atomic_write_0600, config_file_path

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v2/mcp"
ATLASSIAN_MCP_HOST = "mcp.atlassian.com"
# Holds "Bearer <access token>". The FRANKY_ prefix keeps profiles from claiming it; the name has
# no TOKEN/KEY/AUTH/SECRET because Claude Code reads its covered credential names as empty in MCP
# header expansion.
ATLASSIAN_MCP_HEADER_VAR = "FRANKY_ATLASSIAN_MCP_HEADER"
READ_SCOPES = (
    "offline_access read:jira:agent-interface search:jira:agent-interface "
    "read:confluence:agent-interface search:confluence:agent-interface"
)
# Claude denies write tools by pattern (Claude Code accepts a glob in the tool-name position of a
# deny rule). Codex gets a READ allowlist instead, so an unknown future write tool is blocked too.
CLAUDE_DENY_PATTERNS = (
    "create*",
    "edit*",
    "update*",
    "add*",
    "transition*",
    "delete*",
    "executeWrite",
    "executeDestructive",
)
READ_TOOLS = (
    "getJiraIssue",
    "searchJiraIssuesUsingJql",
    "getVisibleJiraProjects",
    "getJiraIssueRemoteIssueLinks",
    "getJiraProjectIssueTypesMetadata",
    "getJiraIssueTypeMetaWithFields",
    "getIssueLinkTypes",
    "getTransitionsForJiraIssue",
    "lookupJiraAccountId",
    "getConfluencePage",
    "getConfluenceSpaces",
    "getPagesInConfluenceSpace",
    "getConfluencePageDescendants",
    "getConfluencePageFooterComments",
    "getConfluencePageInlineComments",
    "getConfluenceCommentChildren",
    "searchConfluenceUsingCql",
    "getCompassComponent",
    "getCompassComponents",
    "getCompassCustomFieldDefinitions",
    "getTeamworkGraphContext",
    "getTeamworkGraphObject",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
    "search",
    "fetch",
    "getContentFormatGuide",
    "discover",
    "executeRead",
    "getConfluenceContent",
    "searchConfluence",
)
# Known write tools (v1 and v2 names): pinned by tests against the two lists above.
WRITE_TOOLS = (
    "addCommentToJiraIssue",
    "addWorklogToJiraIssue",
    "createJiraIssue",
    "editJiraIssue",
    "transitionJiraIssue",
    "createIssueLink",
    "createConfluencePage",
    "updateConfluencePage",
    "createConfluenceFooterComment",
    "createConfluenceInlineComment",
    "createCompassComponent",
    "createCompassComponentRelationship",
    "createCompassCustomFieldDefinition",
    "addTeamworkGraphContext",
    "executeWrite",
    "executeDestructive",
    "createConfluenceContent",
    "updateConfluenceContent",
    "addOrEditJiraIssueComment",
    "addOrEditJiraIssueWorklog",
    "createJiraIssueLink",
    "createConfluenceComment",
)
_BROAD_SCOPE_PREFIXES = ("write:", "delete:", "manage:")
_TIMEOUT = 15
_KEEP_PREVIOUS = 5
_BROWSER_WAIT = 300
_LOCK_WAIT = 30
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")
HINT_REJECTED = "Atlassian tools off (connection expired or revoked) - run `franky connect jira`"
HINT_NETWORK = "Atlassian tools off (token refresh failed: network)"
HINT_BUSY = "Atlassian tools off (connection busy)"
WARN_SAVE = (
    "franky: WARNING could not save the refreshed Atlassian token; "
    "run `franky connect jira` if the next run fails"
)


def store_path(env: Mapping[str, str] | None = None) -> Path:
    return config_file_path(None if env is None else dict(env)).parent / "atlassian-jira.json"


def lock_path(env: Mapping[str, str] | None = None) -> Path:
    return store_path(env).with_suffix(".lock")


class _Busy(ConfigError):
    """The connection lock stayed held past the wait cap."""


@contextlib.contextmanager
def _locked(env):
    """One exclusive lock around every credential mutation (flock is per open file)."""
    path = lock_path(env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _Busy("the Atlassian connection is busy, try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _check_endpoint(url, what: str) -> str:
    """Only https endpoints on atlassian.com: the refresh token is POSTed to these."""
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    host = parsed.hostname or ""
    ok = parsed.scheme == "https" and parsed.netloc.lower() == host
    if not (ok and (host == "atlassian.com" or host.endswith(".atlassian.com"))):
        raise ConfigError(f"refusing {what}: not an https atlassian.com endpoint")
    return url


def _load(env) -> dict | None:
    """The stored connection, or None when absent or malformed (fail closed)."""
    try:
        rec = json.loads(store_path(env).read_text(encoding="utf-8"))
        strs = ("client_id", "token_endpoint", "refresh_token", "access_token", "scope")
        if rec["version"] != 1 or not all(isinstance(rec[k], str) and rec[k] for k in strs):
            return None
        for k in ("expires_at", "connected_at"):
            if not isinstance(rec[k], int | float) or isinstance(rec[k], bool):
                return None
        if not isinstance(rec["previous_access_tokens"], list):
            return None
        if not all(isinstance(t, str) for t in rec["previous_access_tokens"]):
            return None
        if rec["client_secret"] is not None and not isinstance(rec["client_secret"], str):
            return None
        _check_endpoint(rec["token_endpoint"], "token endpoint")
        if rec["revocation_endpoint"] is not None:
            _check_endpoint(rec["revocation_endpoint"], "revocation endpoint")
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save(env, rec: dict) -> None:
    _atomic_write_0600(store_path(env), json.dumps(rec, indent=2) + "\n")


def _call(opener, req, what: str) -> dict:
    """One JSON request; any failure is a NetworkError that never echoes the response body."""
    try:
        with opener(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err = NetworkError(f"{what} failed: HTTP {exc.code}")
        err.status = exc.code  # type: ignore[attr-defined]
        raise err from exc
    except Exception as exc:
        raise NetworkError(f"{what} failed: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise NetworkError(f"{what} failed: unexpected response")
    return data


def _form(opener, url: str, fields: dict, what: str) -> dict:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    return _call(opener, req, what)


def _get(opener, url: str, what: str) -> dict:
    return _call(opener, urllib.request.Request(url, headers={"Accept": "application/json"}), what)


def _discover(opener) -> dict:
    mcp = urllib.parse.urlparse(ATLASSIAN_MCP_URL)
    prm_url = f"{mcp.scheme}://{mcp.netloc}/.well-known/oauth-protected-resource{mcp.path}"
    servers = _get(opener, prm_url, "resource metadata").get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise ConfigError("resource metadata names no authorization server")
    issuer = urllib.parse.urlparse(_check_endpoint(servers[0], "authorization server"))
    base = f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server"
    try:
        meta = _get(opener, base + issuer.path, "authorization server metadata")
    except NetworkError as exc:
        if "HTTP 404" not in str(exc):
            raise
        meta = _get(opener, f"{issuer.geturl()}/.well-known/oauth-authorization-server", "metadata")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        _check_endpoint(meta.get(key), key)
    if meta.get("revocation_endpoint") is not None:
        _check_endpoint(meta["revocation_endpoint"], "revocation_endpoint")
    return meta


def _warn_if_broad(scope: str) -> None:
    if scope == "unknown":
        click.echo(
            "franky: WARNING granted scopes unknown; Franky still denies write tools", err=True
        )
    elif any(s.startswith(_BROAD_SCOPE_PREFIXES) for s in scope.split()):
        click.echo(
            "franky: WARNING the Atlassian grant includes write scopes; Atlassian does not enforce "
            "read-only here, Franky only denies the write tools. Re-run `franky connect jira` and "
            "grant read access.",
            err=True,
        )


class _Callback(BaseHTTPRequestHandler):
    timeout = 10  # socket read timeout: a silent connection cannot stall the single-threaded wait

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        # Only the request that carries OUR state ends the wait; anything else is ignored.
        good = params.get("state") == self.server.expected_state  # type: ignore[attr-defined]
        if good:
            self.server.params = params  # type: ignore[attr-defined]
        body = b"franky: connected, you can close this tab" if good else b"franky: bad state"
        self.send_response(200 if good else 400)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _receive(server: HTTPServer) -> dict:
    """Wait for the browser redirect on the loopback server; {} on timeout."""
    deadline = time.monotonic() + _BROWSER_WAIT
    while not server.params and time.monotonic() < deadline:  # type: ignore[attr-defined]
        server.timeout = max(0.1, deadline - time.monotonic())
        server.handle_request()
    return server.params  # type: ignore[attr-defined]


def connect(
    env: Mapping[str, str],
    *,
    opener: Callable = urllib.request.urlopen,
    open_browser: Callable = webbrowser.open,
    prompt: Callable = click.prompt,
    receive: Callable = _receive,
    no_browser: bool = False,
    now: Callable = time.time,
    echo: Callable = lambda m: click.echo(m, err=True),
) -> None:
    """Run the browser OAuth flow and store the connection. Nothing is stored on any failure."""
    meta = _discover(opener)
    # Bind BEFORE registering: the redirect URI (with its port) is part of the registration.
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    try:
        state = secrets.token_urlsafe(16)
        server.params, server.expected_state = {}, state  # type: ignore[attr-defined]
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        reg = json.dumps(
            {
                "client_name": "franky",
                "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": READ_SCOPES,
            }
        ).encode()
        client = _call(
            opener,
            urllib.request.Request(
                meta["registration_endpoint"],
                data=reg,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            ),
            "client registration",
        )
        client_id, client_secret = client.get("client_id"), client.get("client_secret")
        if not isinstance(client_id, str) or not client_id:
            raise ConfigError("client registration returned no client_id")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        auth_url = (
            meta["authorization_endpoint"]
            + "?"
            + urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "scope": READ_SCOPES,
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode(),
                    "code_challenge_method": "S256",
                    "resource": ATLASSIAN_MCP_URL,
                }
            )
        )
        echo(f"Open this URL to connect Franky to Atlassian:\n{auth_url}")
        if no_browser:
            pasted = prompt("Paste the redirect URL from the browser address bar").strip()
            params = {
                k: v[0]
                for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).items()
            }
        else:
            with contextlib.suppress(Exception):
                open_browser(auth_url)
            params = receive(server)
    finally:
        server.server_close()
    if params.get("state") != state:
        raise ConfigError("Atlassian login failed: no authorization code or state mismatch")
    error = params.get("error")
    if error:
        shown = error if _ERROR_CODE.fullmatch(error) else "error"
        raise ConfigError(f"Atlassian login failed: {shown}")
    if not params.get("code"):
        raise ConfigError("Atlassian login failed: no authorization code or state mismatch")
    fields = {
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": ATLASSIAN_MCP_URL,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    tok = _form(opener, meta["token_endpoint"], fields, "token exchange")
    if not all(isinstance(tok.get(k), str) and tok[k] for k in ("access_token", "refresh_token")):
        raise ConfigError("token exchange returned no refresh token (is offline_access granted?)")
    scope = tok["scope"] if isinstance(tok.get("scope"), str) and tok["scope"] else "unknown"
    _warn_if_broad(scope)
    t = now()
    rec = {
        "version": 1,
        "client_id": client_id,
        "client_secret": client_secret
        if isinstance(client_secret, str) and client_secret
        else None,
        "token_endpoint": meta["token_endpoint"],
        "revocation_endpoint": meta.get("revocation_endpoint"),
        "refresh_token": tok["refresh_token"],
        "access_token": tok["access_token"],
        "expires_at": t + _expires_in(tok),
        "scope": scope,
        "connected_at": t,
        "previous_access_tokens": [],
    }
    with _locked(env):
        old = _load(env)
        if old:
            # Reconnect: the old login stays redactable (tasks may still run on it) and is revoked.
            rec["previous_access_tokens"] = [
                *old["previous_access_tokens"],
                old["access_token"],
            ][-_KEEP_PREVIOUS:]
        _save(env, rec)
        if old:
            _revoke(opener, old)


def _revoke(opener, rec: dict) -> None:
    """Best-effort revoke of a stored refresh token; every error is ignored."""
    if not rec["revocation_endpoint"]:
        return
    fields = {"token": rec["refresh_token"], "client_id": rec["client_id"]}
    if rec["client_secret"]:
        fields["client_secret"] = rec["client_secret"]
    with contextlib.suppress(Exception):
        _form(opener, rec["revocation_endpoint"], fields, "revocation")


def _expires_in(tok: dict) -> float:
    v = tok.get("expires_in")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 3600.0


class Token(NamedTuple):
    """Outcome of `access_token`: `token` is None when tools stay off, then `hint` says why
    (None = not connected). `warning` is a stderr line to print even when a token is returned."""

    token: str | None = None
    hint: str | None = None
    warning: str | None = None
    expires_at: float | None = None


def access_token(
    env: Mapping[str, str],
    *,
    now: Callable = time.time,
    opener: Callable = urllib.request.urlopen,
    min_ttl: float = 2100,
) -> Token:
    """An access token with at least `min_ttl` seconds left, refreshing if needed. When the refresh
    fails the file is left untouched and the Token carries the reason."""
    try:
        with _locked(env):
            rec = _load(env)
            if rec is None:
                return Token()
            if rec["expires_at"] - now() >= min_ttl:
                return Token(rec["access_token"], expires_at=rec["expires_at"])
            fields = {
                "grant_type": "refresh_token",
                "refresh_token": rec["refresh_token"],
                "client_id": rec["client_id"],
                "resource": ATLASSIAN_MCP_URL,
            }
            if rec["client_secret"]:
                fields["client_secret"] = rec["client_secret"]
            try:
                tok = _form(opener, rec["token_endpoint"], fields, "token refresh")
            except NetworkError as exc:
                rejected = getattr(exc, "status", None) in (400, 401)
                return Token(hint=HINT_REJECTED if rejected else HINT_NETWORK)
            new = tok.get("access_token")
            if not isinstance(new, str) or not new:
                return Token(hint=HINT_NETWORK)
            old = rec["access_token"]
            rotated = tok.get("refresh_token")
            scope = tok.get("scope") if isinstance(tok.get("scope"), str) else rec["scope"]
            expires_at = now() + _expires_in(tok)
            rec.update(
                access_token=new,
                expires_at=expires_at,
                refresh_token=rotated
                if isinstance(rotated, str) and rotated
                else rec["refresh_token"],
                scope=scope,
                # Tasks still running on an older token must stay redactable: keep the last few.
                previous_access_tokens=[*rec["previous_access_tokens"], old][-_KEEP_PREVIOUS:],
            )
            warning = None
            try:
                _save(env, rec)
            except Exception:
                warning = WARN_SAVE  # this run still gets the new token
            _warn_if_broad(scope)
            return Token(new, warning=warning, expires_at=expires_at)
    except _Busy:
        return Token(hint=HINT_BUSY)
    except Exception:
        return Token(hint=HINT_NETWORK)


def status(env: Mapping[str, str], *, now: Callable = time.time) -> str:
    """One secret-free line, no network."""
    rec = _load(env)
    if rec is None:
        if store_path(env).exists():
            return "not connected (stored connection is malformed)"
        return "not connected"
    left = int(rec["expires_at"] - now())
    valid = f"access token valid for {left // 60} min" if left > 0 else "access token expired"
    when = time.strftime("%Y-%m-%d", time.gmtime(rec["connected_at"]))
    return f"connected, {valid}, scopes {rec['scope']}, connected {when}"


def disconnect(env: Mapping[str, str], *, opener: Callable = urllib.request.urlopen) -> bool:
    """Best-effort revoke, then delete the connection. True when a connection file existed."""
    with _locked(env):
        existed = store_path(env).exists()
        rec = _load(env)
        if rec:
            _revoke(opener, rec)
        store_path(env).unlink(missing_ok=True)
    return existed


def stored_secrets(env: Mapping[str, str]) -> list[str]:
    """Every secret string of the stored connection, for redaction and scrubbing."""
    rec = _load(env)
    if rec is None:
        return []
    tokens = [rec["access_token"], *rec["previous_access_tokens"]]
    out = [
        rec["refresh_token"],
        rec["client_secret"] or "",
        *tokens,
        *(f"Bearer {t}" for t in tokens),
    ]
    return list(dict.fromkeys(v for v in out if v))


def claude_mcp_json() -> str:
    """Inline `claude --mcp-config` JSON; the header is a `${VAR}` placeholder, never a value."""
    return json.dumps(
        {
            "mcpServers": {
                "atlassian": {
                    "type": "http",
                    "url": ATLASSIAN_MCP_URL,
                    "headers": {"Authorization": "${" + ATLASSIAN_MCP_HEADER_VAR + "}"},
                }
            }
        },
        separators=(",", ":"),
    )


def claude_disallowed() -> str:
    return ",".join("mcp__atlassian__" + name for name in CLAUDE_DENY_PATTERNS)


def codex_override() -> str:
    """`codex -c` value for the same server; codex reads the header from the env by name."""
    # json.dumps strings and lists are valid TOML.
    url, var = json.dumps(ATLASSIAN_MCP_URL), json.dumps(ATLASSIAN_MCP_HEADER_VAR)
    return (
        f"mcp_servers.atlassian={{ url = {url}, env_http_headers = {{ Authorization = {var} }}, "
        f"enabled_tools = {json.dumps(list(READ_TOOLS))} }}"
    )
