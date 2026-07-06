"""
OAuth client — discovery, DCR, PKCE, and token exchange.

Implements the MCP Authorization spec (RFC 9728 + RFC 8414 + RFC 7591 + RFC 7636).
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from shared.logging_config import get_logger, log_duration
from shared.utils import redact_token

logger = get_logger("oauth.client")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OAuthDiscovery:
    """Result of OAuth discovery against an MCP server."""

    __slots__ = (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "scopes",
        "resource",
    )

    def __init__(
        self,
        authorization_endpoint: str,
        token_endpoint: str,
        registration_endpoint: str,
        scopes: list[str],
        resource: str,
    ):
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.registration_endpoint = registration_endpoint
        self.scopes = scopes
        self.resource = resource


class DCRClient:
    """Credentials returned by Dynamic Client Registration."""

    __slots__ = (
        "client_id",
        "authorization_endpoint",
        "token_endpoint",
    )

    def __init__(
        self,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
    ):
        self.client_id = client_id
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint


@dataclass(frozen=True, slots=True)
class TokenExpiryState:
    """Normalized view of a token's expiry metadata."""

    expiry_known: bool
    expires_at: int | None
    expired: bool


# ---------------------------------------------------------------------------
# OAuth Discovery (RFC 9728 + RFC 8414)
# ---------------------------------------------------------------------------

_INIT_PAYLOAD = (
    '{"jsonrpc":"2.0","method":"initialize",'
    '"params":{"protocolVersion":"2024-11-05","capabilities":{},'
    '"clientInfo":{"name":"mcp-gateway","version":"1.0.0"}},"id":1}'
)


def _resolve_url(base: str, value: str) -> str:
    """Resolve ``value`` against ``base`` per RFC 3986."""
    return urljoin(base, value)


def _build_rfc8414_well_known(issuer_url: str) -> str:
    """Construct the RFC 8414 §3.1 well-known URL for an issuer.

    Inserts ``/.well-known/oauth-authorization-server`` between the host and
    path components of the issuer URL. For ``https://host/foo/bar`` returns
    ``https://host/.well-known/oauth-authorization-server/foo/bar``.
    """
    parsed = urlparse(issuer_url)
    if parsed.scheme != "https":
        raise RuntimeError(f"AS issuer URL must use https scheme: {issuer_url}")
    host = (parsed.netloc or "").lower()
    path = parsed.path
    if path == "/":
        path = ""
    return f"https://{host}/.well-known/oauth-authorization-server{path}"


async def _fetch_as_metadata(
    client: httpx.AsyncClient,
    auth_server_url: str,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Fetch RFC 8414 AS metadata, with a 404-only fallback to the bare base URL.

    Some servers return the MCP resource path as the ``authorization_servers``
    entry in their protected-resource metadata but only serve AS metadata at
    the bare base URL. When the primary lookup returns HTTP 404, retry at the
    base URL so that pattern keeps working.
    """
    request_headers = _merge_headers({"Accept": "application/json"}, extra_headers)
    primary = _build_rfc8414_well_known(auth_server_url)
    logger.info("[oauth-discovery] GET AS metadata %s", primary)
    resp = await client.get(primary, headers=request_headers)

    if resp.status_code == 200:
        return resp.json()

    if resp.status_code != 404:
        raise RuntimeError(
            f"Failed to fetch AS metadata from {primary}: HTTP {resp.status_code}"
        )

    parsed = urlparse(auth_server_url)
    if not parsed.path or parsed.path == "/":
        raise RuntimeError(f"Failed to fetch AS metadata from {primary}: HTTP 404")

    base = f"{parsed.scheme}://{(parsed.netloc or '').lower()}"
    fallback = _build_rfc8414_well_known(base)
    logger.warning(
        "[oauth-discovery] AS metadata 404 at %s; retrying at base URL %s",
        primary, fallback,
    )
    fb_resp = await client.get(fallback, headers=request_headers)
    if fb_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch AS metadata from {primary}: HTTP 404 (fallback "
            f"{fallback} also failed: HTTP {fb_resp.status_code})"
        )
    return fb_resp.json()


def _parse_www_authenticate(header: str) -> dict[str, str]:
    """Extract key=value params from a Bearer WWW-Authenticate header."""
    params: dict[str, str] = {}
    value = header
    if value.lower().startswith("bearer "):
        value = value[7:]
    for part in value.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        params[key.strip()] = val.strip().strip('"')
    return params


def _merge_headers(*sources: dict[str, str] | None) -> dict[str, str]:
    """Merge header dicts left-to-right; later sources win on key collision."""
    merged: dict[str, str] = {}
    for src in sources:
        if src:
            merged.update(src)
    return merged


async def discover_oauth(
    server_url: str,
    extra_headers: dict[str, str] | None = None,
) -> OAuthDiscovery:
    """Probe an MCP server and discover its OAuth endpoints.

    Flow:
    1. POST MCP initialize to *server_url* — expect 401
    2. Parse ``WWW-Authenticate`` for ``resource_metadata`` URL
    3. GET ``/.well-known/oauth-protected-resource`` — get ``authorization_server``
    4. GET ``/.well-known/oauth-authorization-server`` — get endpoints
    """
    parsed = urlparse(server_url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port and parsed.port not in (80, 443):
        base_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        logger.info("[oauth-discovery] POST %s (expecting 401)", server_url)
        with log_duration(logger, "[oauth-discovery] initial 401 probe"):
            resp = await client.post(
                server_url,
                content=_INIT_PAYLOAD,
                headers=_merge_headers(
                    {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    extra_headers,
                ),
            )

        if resp.status_code != 401:
            raise RuntimeError(
                f"Expected 401 from {server_url}, got {resp.status_code} — server may not require OAuth"
            )

        www_auth = resp.headers.get("www-authenticate", "")
        params = _parse_www_authenticate(www_auth) if www_auth else {}
        resource_metadata_url = params.get("resource_metadata", "")
        if resource_metadata_url:
            resource_metadata_url = _resolve_url(server_url, resource_metadata_url)

        auth_server_url = base_url
        scopes: list[str] = []

        rm_request_headers = _merge_headers({"Accept": "application/json"}, extra_headers)
        if resource_metadata_url:
            logger.info("[oauth-discovery] GET resource metadata %s", resource_metadata_url)
            rm_resp = await client.get(resource_metadata_url, headers=rm_request_headers)
            if rm_resp.status_code == 200:
                rm = rm_resp.json()
                auth_server_url = rm.get("authorization_server") or ""
                if not auth_server_url and rm.get("authorization_servers"):
                    auth_server_url = rm["authorization_servers"][0]
                if auth_server_url:
                    auth_server_url = _resolve_url(server_url, auth_server_url)
                scopes = rm.get("scopes", [])
                resource = rm.get("resource", base_url)
                if resource:
                    resource = _resolve_url(server_url, resource)
            else:
                resource = base_url
        else:
            well_known = f"{base_url}/.well-known/oauth-protected-resource"
            logger.info("[oauth-discovery] fallback GET %s", well_known)
            rm_resp = await client.get(well_known, headers=rm_request_headers)
            resource = base_url
            if rm_resp.status_code == 200:
                rm = rm_resp.json()
                auth_server_url = rm.get("authorization_server") or ""
                if not auth_server_url and rm.get("authorization_servers"):
                    auth_server_url = rm["authorization_servers"][0]
                if auth_server_url:
                    auth_server_url = _resolve_url(server_url, auth_server_url)
                scopes = rm.get("scopes", [])
                resource = rm.get("resource", base_url)
                if resource:
                    resource = _resolve_url(server_url, resource)

        if not auth_server_url:
            auth_server_url = base_url

        as_meta = await _fetch_as_metadata(client, auth_server_url, extra_headers=extra_headers)
        authorization_endpoint = as_meta.get("authorization_endpoint", "")
        token_endpoint = as_meta.get("token_endpoint", "")
        registration_endpoint = as_meta.get("registration_endpoint", "")

        if not authorization_endpoint or not token_endpoint:
            raise RuntimeError(
                f"AS metadata missing required endpoints (authorization_endpoint={authorization_endpoint!r}, "
                f"token_endpoint={token_endpoint!r})"
            )

        if not scopes:
            scopes = as_meta.get("scopes_supported", [])

        if not scopes and "scope" in params:
            scopes = params["scope"].split()

        logger.info(
            "[oauth-discovery] complete: auth=%s token=%s reg=%s scopes=%s",
            authorization_endpoint, token_endpoint, registration_endpoint, scopes,
        )

        return OAuthDiscovery(
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
            scopes=scopes,
            resource=resource,
        )


# ---------------------------------------------------------------------------
# DCR (RFC 7591)
# ---------------------------------------------------------------------------


async def perform_dcr(
    discovery: OAuthDiscovery,
    server_name: str,
    redirect_uri: str,
    extra_headers: dict[str, str] | None = None,
) -> DCRClient:
    """Register a public OAuth client via Dynamic Client Registration."""
    if not discovery.registration_endpoint:
        raise RuntimeError(f"No registration endpoint for {server_name}")

    body = {
        "client_name": f"MCP Gateway - {server_name}",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_uri": "https://github.com/docker/mcp-gateway",
        "software_id": "mcp-gateway",
        "software_version": "1.0.0",
        "contacts": ["support@docker.com"],
    }
    if discovery.scopes:
        body["scope"] = " ".join(discovery.scopes)

    async with httpx.AsyncClient(timeout=30) as client:
        logger.info("[oauth-dcr] POST %s", discovery.registration_endpoint)
        with log_duration(logger, "[oauth-dcr] POST register"):
            resp = await client.post(
                discovery.registration_endpoint,
                json=body,
                headers=_merge_headers({"Accept": "application/json"}, extra_headers),
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"DCR failed for {server_name}: HTTP {resp.status_code} — {resp.text}"
            )

        data = resp.json()
        client_id = data.get("client_id", "")
        if not client_id:
            raise RuntimeError(f"DCR response missing client_id for {server_name}")

        logger.info("[oauth-dcr] registered client_id=%s for %s", client_id, server_name)
        return DCRClient(
            client_id=client_id,
            authorization_endpoint=discovery.authorization_endpoint,
            token_endpoint=discovery.token_endpoint,
        )


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256).

    Returns ``(code_verifier, code_challenge)``.
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


def build_auth_url(
    dcr_client: DCRClient,
    redirect_uri: str,
    scopes: str,
    state: str,
    code_challenge: str,
    resource: str = "",
) -> str:
    """Build the OAuth authorization URL with PKCE.

    Merges PKCE/OAuth params with any query string already present on the
    AS-metadata-discovered ``authorization_endpoint``.
    """
    new_params: dict[str, str] = {
        "response_type": "code",
        "client_id": dcr_client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        new_params["scope"] = scopes
    if resource:
        new_params["resource"] = resource

    parsed = urlparse(dcr_client.authorization_endpoint)
    merged = list(parse_qsl(parsed.query, keep_blank_values=True))
    existing_keys = {k for k, _ in merged}
    for k, v in new_params.items():
        if k in existing_keys:
            merged = [(mk, v if mk == k else mv) for mk, mv in merged]
        else:
            merged.append((k, v))

    return urlunparse(parsed._replace(query=urlencode(merged)))


# ---------------------------------------------------------------------------
# Token Exchange
# ---------------------------------------------------------------------------


async def exchange_code_for_token(
    dcr_client: DCRClient,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Exchange an authorization code + PKCE verifier for tokens."""
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": dcr_client.client_id,
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        logger.info("[oauth-token] POST %s", dcr_client.token_endpoint)
        with log_duration(logger, "[oauth-token] POST token exchange"):
            resp = await client.post(
                dcr_client.token_endpoint,
                data=body,
                headers=_merge_headers({"Accept": "application/json"}, extra_headers),
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed: HTTP {resp.status_code}")

        token_data = resp.json()
        if "access_token" not in token_data:
            raise RuntimeError("Token response missing access_token")

        logger.info(
            "[oauth-token] token exchange successful, type=%s token=%s",
            token_data.get("token_type", "unknown"),
            redact_token(token_data.get("access_token", "")),
        )
        return stamp_token_expiry(token_data)


# ---------------------------------------------------------------------------
# Token expiry helpers
# ---------------------------------------------------------------------------


def stamp_token_expiry(token_data: dict) -> dict:
    """Add an ``expires_at`` epoch timestamp derived from ``expires_in``."""
    expires_in = token_data.get("expires_in")
    if expires_in and "expires_at" not in token_data:
        token_data["expires_at"] = int(time.time()) + int(expires_in)
    return token_data


def get_token_expiry_state(token_data: dict, buffer_seconds: int = 60) -> TokenExpiryState:
    """Classify expiry metadata without overloading missing/invalid values."""
    expires_at_raw = token_data.get("expires_at")
    if expires_at_raw in (None, "", 0):
        return TokenExpiryState(expiry_known=False, expires_at=None, expired=False)

    try:
        expires_at = int(expires_at_raw)
    except (TypeError, ValueError):
        logger.warning(
            "[oauth-expiry] ignoring invalid expires_at value of type %s",
            type(expires_at_raw).__name__,
        )
        return TokenExpiryState(expiry_known=False, expires_at=None, expired=False)

    if expires_at <= 0:
        return TokenExpiryState(expiry_known=False, expires_at=None, expired=False)

    return TokenExpiryState(
        expiry_known=True,
        expires_at=expires_at,
        expired=time.time() >= (expires_at - buffer_seconds),
    )


def is_token_expired(token_data: dict, buffer_seconds: int = 60) -> bool:
    """Return True if the token is expired or will expire within *buffer_seconds*."""
    return get_token_expiry_state(token_data, buffer_seconds=buffer_seconds).expired


# ---------------------------------------------------------------------------
# Token Refresh
# ---------------------------------------------------------------------------


async def refresh_access_token(
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Use a refresh token to obtain a new access token."""
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        logger.info("[oauth-refresh] POST %s", token_endpoint)
        with log_duration(logger, "[oauth-refresh] POST token refresh"):
            resp = await client.post(
                token_endpoint,
                data=body,
                headers=_merge_headers({"Accept": "application/json"}, extra_headers),
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code}")

        token_data = resp.json()
        if "access_token" not in token_data:
            raise RuntimeError("Refresh response missing access_token")

        logger.info(
            "[oauth-refresh] token refresh successful, type=%s token=%s",
            token_data.get("token_type", "unknown"),
            redact_token(token_data.get("access_token", "")),
        )
        return stamp_token_expiry(token_data)
