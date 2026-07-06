"""
OAuth broker plugin.

Provides OAuth app listing, authorization flows, revocation, and the
callback server that receives IdP redirects.

Authorization uses the full RFC-compliant OAuth flow:
discovery → DCR → PKCE → auth URL → callback → token exchange.

This module's tools are registered on the main sidecar FastMCP instance
by ``register_tools(mcp)`` called from server.py. They are present in
the current deployment for future use; the active delegation path for
this phase is the static PAT lookup in ``get_connection_headers``.
"""

import asyncio
import html
import ipaddress
import os
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp.server.context import Context
from mcp.server.session import ServerSession

from shared.envelope import success_envelope, error_envelope
from shared.logging_config import get_logger
from datastores.oauth_state_store import get_oauth_state_store
from oauth.oauth_client import (
    DCRClient,
    build_auth_url,
    discover_oauth,
    exchange_code_for_token,
    generate_pkce,
    get_token_expiry_state,
    perform_dcr,
)
from datastores.token_store import get_token_store

logger = get_logger("oauth.server")

# ---------------------------------------------------------------------------
# OAuth state
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OAuthCacheEntry:
    """Cached discovery + DCR data for a single app/server_url pairing."""

    dcr_client: DCRClient
    resource: str
    scopes: tuple[str, ...]
    server_url: str
    headers_fingerprint: str = ""


def _headers_fingerprint(headers: dict[str, str] | None) -> str:
    """Stable fingerprint for a headers dict; used as part of the OAuth cache key."""
    if not headers:
        return ""
    return "|".join(f"{k}={v}" for k, v in sorted(headers.items()))


_BLOCKED_METADATA_HOSTS = frozenset({
    "169.254.169.254",
    "fd00:ec2::254",
    "metadata.google.internal",
    "metadata",
})


def _is_safe_redirect_target(url: str) -> bool:
    """SSRF guard for server-side redirect-following.

    Rejects non-https schemes, cloud metadata hostnames, and hosts that parse
    as private / loopback / link-local / reserved IPs.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _BLOCKED_METADATA_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve_authorize_redirects(
    initial_url: str,
    headers: dict[str, str],
    max_hops: int = 5,
) -> str:
    """Follow header-driven redirects on the authorize URL server-side.

    Some providers inspect catalog-supplied headers and 302-redirect the
    authorize request to a tenant-specific URL. The user's browser can't
    carry our static catalog headers, so we resolve the chain here with the
    headers attached, then return the final self-sufficient URL.
    """
    if not _is_safe_redirect_target(initial_url):
        logger.warning(
            "[oauth-authorize] refusing to follow redirect for %s: URL failed SSRF guard",
            initial_url,
        )
        return initial_url
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            current = initial_url
            for _ in range(max_hops):
                resp = await client.get(current, headers=headers)
                if 300 <= resp.status_code < 400:
                    location = resp.headers.get("location")
                    if not location:
                        break
                    nxt = str(httpx.URL(current).join(location))
                    if not _is_safe_redirect_target(nxt):
                        logger.warning(
                            "[oauth-authorize] refusing redirect %s -> %s: target failed SSRF guard",
                            current, nxt,
                        )
                        return initial_url
                    current = nxt
                    continue
                break
            if current != initial_url:
                logger.info("[oauth-authorize] resolved authorize redirect: %s -> %s", initial_url, current)
            return current
    except Exception as exc:
        logger.warning(
            "[oauth-authorize] redirect resolution failed for %s: %s; returning original URL",
            initial_url, exc,
        )
        return initial_url


_oauth_cache: dict[str, OAuthCacheEntry] = {}
_dcr_cache: dict[str, DCRClient] = {}

OAUTH_CALLBACK_PORT = 0
OAUTH_CALLBACK_BASE_URL = ""
_oauth_initialized = False


def init():
    """Read env vars for the OAuth plugin. Called by server.py after import.

    If MCP_GATEWAY_OAUTH_PORT is not set the OAuth callback server is not
    started and OAuth flows are unavailable. PAT delegation is unaffected.
    """
    global OAUTH_CALLBACK_PORT, OAUTH_CALLBACK_BASE_URL, _oauth_initialized
    if _oauth_initialized:
        return
    port_str = os.environ.get("MCP_GATEWAY_OAUTH_PORT", "")
    OAUTH_CALLBACK_PORT = int(port_str) if port_str else 0
    OAUTH_CALLBACK_BASE_URL = os.environ.get("MCP_GATEWAY_OAUTH_CALLBACK_BASE_URL", "")
    _oauth_initialized = True
    if OAUTH_CALLBACK_PORT:
        _start_oauth_callback_server()


_oauth_callback_server: HTTPServer | None = None
_oauth_callback_thread: threading.Thread | None = None

_oauth_session: ServerSession | None = None
_oauth_events: asyncio.Queue | None = None
_oauth_event_loop: asyncio.AbstractEventLoop | None = None


async def _send_oauth_notifications() -> None:
    """Background coroutine that drains _oauth_events and sends log notifications."""
    assert _oauth_events is not None
    while True:
        event = await _oauth_events.get()
        if _oauth_session is None:
            logger.warning("[oauth] no session, dropping event")
            continue
        try:
            await _oauth_session.send_log_message(
                level="info",
                data=event,
                logger="oauth",
            )
            logger.info("[oauth] sent log notification")
        except Exception as exc:
            logger.error("[oauth] failed to send log notification: %s", exc)


def _do_token_exchange(state: str, code: str) -> tuple[str, str]:
    """Run the async token exchange from the synchronous callback thread."""
    state_data = get_oauth_state_store().pop_state(state)
    if state_data is None:
        raise ValueError("Unknown or expired state")

    app_name: str = state_data["app_name"]
    user_principal: str = state_data["user_principal"]
    code_verifier: str = state_data["code_verifier"]
    catalog_headers: dict[str, str] = state_data.get("catalog_headers") or {}
    dcr_client = DCRClient(
        client_id=state_data["dcr_client_id"],
        authorization_endpoint=state_data["dcr_authorization_endpoint"],
        token_endpoint=state_data["dcr_token_endpoint"],
    )

    if OAUTH_CALLBACK_BASE_URL:
        redirect_uri = f"{OAUTH_CALLBACK_BASE_URL}/callback"
    else:
        redirect_uri = f"http://localhost:{OAUTH_CALLBACK_PORT}/callback"

    loop = asyncio.new_event_loop()
    try:
        token_data = loop.run_until_complete(
            exchange_code_for_token(
                dcr_client, code, redirect_uri, code_verifier,
                extra_headers=catalog_headers or None,
            )
        )
    finally:
        loop.close()

    token_data["dcr_client_id"] = dcr_client.client_id
    token_data["dcr_token_endpoint"] = dcr_client.token_endpoint
    if catalog_headers:
        token_data["catalog_headers"] = catalog_headers

    get_token_store().save_token(user_principal, app_name, token_data)
    logger.info("[oauth-callback] token exchange + save completed")
    return app_name, user_principal


def _render_callback_page(*, success: bool, app_name: str = "") -> bytes:
    """Render an OAuth callback result page."""
    if success:
        icon_svg = (
            '<svg width="64" height="64" viewBox="0 0 64 64" fill="none">'
            '<circle cx="32" cy="32" r="32" fill="#16a34a"/>'
            '<path d="M20 33l8 8 16-16" stroke="#fff" stroke-width="4"'
            ' stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg>'
        )
        title = "Authorization Successful"
        detail = (
            f"<strong>{html.escape(app_name)}</strong> has been authorized."
            if app_name else "Authorization complete."
        )
        message = "You can close this window and return to the terminal."
    else:
        icon_svg = (
            '<svg width="64" height="64" viewBox="0 0 64 64" fill="none">'
            '<circle cx="32" cy="32" r="32" fill="#dc2626"/>'
            '<path d="M22 22l20 20M42 22L22 42" stroke="#fff" stroke-width="4"'
            ' stroke-linecap="round"/>'
            '</svg>'
        )
        title = "Authorization Failed"
        detail = "Token exchange failed."
        message = "Please close this window and try again."

    page = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — MCP Gateway</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: #f0f4f8;
    color: #1a1a2e;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
  }}
  .card {{
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
    max-width: 460px;
    width: 100%;
    text-align: center;
    overflow: hidden;
  }}
  .card-header {{
    background: #1D63ED;
    padding: 1.25rem;
  }}
  .card-header span {{
    color: #fff;
    font-size: 1rem;
    font-weight: 500;
    letter-spacing: 0.01em;
  }}
  .card-body {{
    padding: 2.5rem 2rem 2rem;
  }}
  .icon {{ margin-bottom: 1.25rem; }}
  h1 {{
    font-size: 1.375rem;
    font-weight: 700;
    margin-bottom: 0.75rem;
  }}
  .detail {{
    font-size: 0.9375rem;
    color: #4a5568;
    margin-bottom: 0.5rem;
    line-height: 1.5;
  }}
  .hint {{
    font-size: 0.8125rem;
    color: #94a3b8;
    margin-top: 1.5rem;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <span>MCP Gateway</span>
  </div>
  <div class="card-body">
    <div class="icon">{icon_svg}</div>
    <h1>{title}</h1>
    <p class="detail">{detail}</p>
    <p class="hint">{message}</p>
  </div>
</div>
</body>
</html>"""
    return page.encode("utf-8")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles GET /callback?code=...&state=..."""

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path not in ("/oauth/callback", "/callback"):
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]

        if not code or not state:
            self.send_error(400, "Missing code or state")
            return

        try:
            app_name, user_principal = _do_token_exchange(state, code)
        except ValueError:
            self.send_error(400, "Unknown or expired state")
            return
        except Exception as exc:
            logger.error("[oauth-callback] token exchange failed: %s", exc)
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_render_callback_page(success=False))
            return

        if _oauth_event_loop is not None and _oauth_events is not None:
            _oauth_event_loop.call_soon_threadsafe(
                _oauth_events.put_nowait,
                {"event": "auth_success", "server_name": app_name, "user_principal": user_principal},
            )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_render_callback_page(success=True, app_name=app_name))

    def log_message(self, format, *args):  # noqa: A002
        logger.info("[oauth-callback-server] %s", format % args)


def _start_oauth_callback_server() -> None:
    global _oauth_callback_server, _oauth_callback_thread
    if _oauth_callback_server is not None:
        return
    _oauth_callback_server = HTTPServer(("0.0.0.0", OAUTH_CALLBACK_PORT), _OAuthCallbackHandler)
    _oauth_callback_thread = threading.Thread(
        target=_oauth_callback_server.serve_forever, daemon=True
    )
    _oauth_callback_thread.start()
    logger.info("[oauth-callback-server] Listening on port %d", OAUTH_CALLBACK_PORT)


def _stop_oauth_callback_server() -> None:
    global _oauth_callback_server, _oauth_callback_thread
    if _oauth_callback_server is None:
        return
    _oauth_callback_server.shutdown()
    _oauth_callback_server = None
    _oauth_callback_thread = None
    logger.info("[oauth-callback-server] Stopped")


# ---------------------------------------------------------------------------
# OAuth tools
# ---------------------------------------------------------------------------


def _do_oauth_ls(user_principal: str = "", oauth_servers: list | None = None) -> dict:
    if not user_principal:
        return error_envelope("invalid_request", "user_principal is required")
    if oauth_servers is None:
        oauth_servers = []

    user_tokens: dict = {}
    try:
        user_tokens = get_token_store().list_tokens(user_principal)
    except Exception as exc:
        logger.warning("[oauth-ls] token store lookup failed: %s", exc)

    apps = []
    seen: set = set()
    for srv in oauth_servers:
        name = srv.get("name", "") if isinstance(srv, dict) else str(srv)
        if not name:
            continue
        seen.add(name)
        token_data = user_tokens.get(name)
        expiry_state = get_token_expiry_state(token_data) if token_data else None
        apps.append({
            "app": name,
            "authorized": token_data is not None and not (expiry_state.expired if expiry_state else False),
            "expires_at": expiry_state.expires_at if expiry_state else None,
            "expiry_known": expiry_state.expiry_known if expiry_state else False,
            "provider": srv.get("provider", "") if isinstance(srv, dict) else "",
            "scopes": srv.get("scopes", []) if isinstance(srv, dict) else [],
        })

    for server_name in user_tokens:
        if server_name not in seen:
            token_data = user_tokens[server_name]
            expiry_state = get_token_expiry_state(token_data) if token_data else None
            apps.append({
                "app": server_name,
                "authorized": not (expiry_state.expired if expiry_state else False),
                "expires_at": expiry_state.expires_at if expiry_state else None,
                "expiry_known": expiry_state.expiry_known if expiry_state else False,
            })

    logger.info("[oauth-ls] user_principal=%r -> %d apps", user_principal, len(apps))
    return success_envelope(apps)


def oauth_ls(user_principal: str = "", oauth_servers: list | None = None) -> dict:
    """List OAuth-capable servers and their authorization status for *user_principal*."""
    return _do_oauth_ls(user_principal, oauth_servers)


async def oauth_authorize(
        app: str,
        scopes: str = "",
        user_principal: str = "",
        server_url: str = "",
        disable_auto_open: bool = False,
        headers: dict[str, str] | None = None,
) -> dict:
    """Start OAuth authorization flow for an app. Returns auth URL."""
    logger.info(
        "[oauth-authorize] app=%r scopes=%r server_url=%r",
        app, scopes, server_url,
    )

    if not user_principal:
        return error_envelope("invalid_request", "user_principal is required for OAuth authorization")

    if not server_url:
        return error_envelope("invalid_request", f"server_url is required for OAuth authorization of '{app}'")

    if not OAUTH_CALLBACK_PORT:
        return error_envelope("not_configured", "OAuth callback server is not configured (MCP_GATEWAY_OAUTH_PORT not set)")

    if OAUTH_CALLBACK_BASE_URL:
        redirect_uri = f"{OAUTH_CALLBACK_BASE_URL}/callback"
    else:
        redirect_uri = f"http://localhost:{OAUTH_CALLBACK_PORT}/callback"

    try:
        headers_fp = _headers_fingerprint(headers)
        cache_entry = _oauth_cache.get(app)
        if (
            cache_entry is None
            or cache_entry.server_url != server_url
            or cache_entry.headers_fingerprint != headers_fp
        ):
            discovery = await discover_oauth(server_url, extra_headers=headers)
            dcr_client = await perform_dcr(discovery, app, redirect_uri, extra_headers=headers)
            cache_entry = OAuthCacheEntry(
                dcr_client=dcr_client,
                resource=discovery.resource,
                scopes=tuple(discovery.scopes),
                server_url=server_url,
                headers_fingerprint=headers_fp,
            )
            _oauth_cache[app] = cache_entry
            _dcr_cache[app] = dcr_client
        else:
            dcr_client = cache_entry.dcr_client

        if not scopes and cache_entry.scopes:
            scopes = " ".join(cache_entry.scopes)

        resource = cache_entry.resource

        code_verifier, code_challenge = generate_pkce()
        state = secrets.token_urlsafe(16)

        auth_url = build_auth_url(
            dcr_client, redirect_uri, scopes, state, code_challenge,
            resource=resource,
        )

        if headers:
            auth_url = await _resolve_authorize_redirects(auth_url, headers)

        get_oauth_state_store().save_state(state, {
            "app_name": app,
            "user_principal": user_principal,
            "code_verifier": code_verifier,
            "dcr_client_id": dcr_client.client_id,
            "dcr_authorization_endpoint": dcr_client.authorization_endpoint,
            "dcr_token_endpoint": dcr_client.token_endpoint,
            "catalog_headers": headers or {},
        })

        logger.info("[oauth-authorize] auth URL generated for app=%r", app)
        return success_envelope({"auth_url": auth_url})

    except Exception as exc:
        logger.error("[oauth-authorize] OAuth flow failed for %s: %s", app, exc)
        return error_envelope("oauth_error", str(exc))


def oauth_revoke(app: str, user_principal: str = "") -> dict:
    """Revoke OAuth access for an app (deletes the user's stored token)."""
    logger.info("[oauth-revoke] app=%r", app)
    if not user_principal:
        return error_envelope("invalid_request", "user_principal is required")
    try:
        get_token_store().delete_token(user_principal, app)
    except Exception as exc:
        logger.error("[oauth-revoke] token store delete failed: %s", exc)
        return error_envelope("internal", f"Failed to revoke token: {exc}")
    return success_envelope({})


async def oauth_start_callback_server(ctx: Context) -> dict:
    """Start the OAuth callback server and capture the MCP session for notifications."""
    global _oauth_session, _oauth_events, _oauth_event_loop

    _oauth_session = ctx.session
    _oauth_event_loop = asyncio.get_event_loop()
    if _oauth_events is None:
        _oauth_events = asyncio.Queue()
        asyncio.create_task(_send_oauth_notifications())

    if OAUTH_CALLBACK_PORT:
        _start_oauth_callback_server()
        return success_envelope({"port": OAUTH_CALLBACK_PORT})
    return error_envelope("not_configured", "MCP_GATEWAY_OAUTH_PORT not set")


def oauth_stop_callback_server() -> dict:
    """Stop the OAuth callback server."""
    _stop_oauth_callback_server()
    return success_envelope({})


def register_tools(mcp_instance) -> None:
    """Register the OAuth plugin's tools on the host FastMCP instance."""
    mcp_instance.tool(name="oauth-ls")(oauth_ls)
    mcp_instance.tool(name="oauth-authorize")(oauth_authorize)
    mcp_instance.tool(name="oauth-revoke")(oauth_revoke)
    mcp_instance.tool(name="oauth-start-callback-server")(oauth_start_callback_server)
    mcp_instance.tool(name="oauth-stop-callback-server")(oauth_stop_callback_server)
