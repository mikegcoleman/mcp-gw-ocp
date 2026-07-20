"""
Entra ID OIDC sidecar for the MCP Gateway.

- Validates Entra JWTs in the `authenticate` MCP tool against the tenant's
  JWKS endpoint.
- Serves /.well-known/oauth-protected-resource (RFC 9728) so MCP clients
  discover the authorization server.
- Exposes `get_connection_headers` which looks up per-user PATs from Azure
  Key Vault and returns them as Authorization headers.

Env:
  ENTRA_TENANT_ID     Required. Tenant GUID (or domain).
  ENTRA_AUDIENCE      Required. Application (client) ID of the gateway app reg.
                      For v2.0 tokens Entra puts the client_id in the `aud` claim.
  ENTRA_CLIENT_ID     Required. Same as ENTRA_AUDIENCE (advertised in PRM).
  ENTRA_RESOURCE_URI  Optional. Application ID URI (default: api://{CLIENT_ID}).
  GATEWAY_RESOURCE    Required. Public URL of the gateway (advertised in PRM).
  AZURE_KEYVAULT_URL  Required. Full URL of the Azure Key Vault.
  DCR_PROXY_URL       Optional. Base URL of the Entra DCR proxy (e.g.
                      https://<gateway-host>/dcr). When set, the PRM
                      advertises the proxy as the authorization server instead
                      of Entra directly, enabling RFC 7591 DCR for MCP clients
                      (Claude Code, Claude Desktop, VS Code).
"""

import logging
import os
import re

import jwt
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from fastmcp import FastMCP
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("entra-sidecar")

# ── Required config ─────────────────────────────────────────────────────────
TENANT_ID = os.environ["ENTRA_TENANT_ID"]
AUDIENCE = os.environ["ENTRA_AUDIENCE"]
CLIENT_ID = os.environ["ENTRA_CLIENT_ID"]
RESOURCE_URI = os.environ.get("ENTRA_RESOURCE_URI", f"api://{CLIENT_ID}")
GATEWAY_RESOURCE = os.environ["GATEWAY_RESOURCE"]
KEYVAULT_URL = os.environ["AZURE_KEYVAULT_URL"]
DCR_PROXY_URL = os.environ.get("DCR_PROXY_URL", "").rstrip("/")

# ── Derived ─────────────────────────────────────────────────────────────────
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
# When DCR_PROXY_URL is set, advertise the proxy as the authorization server so
# MCP clients (Claude Code, Claude Desktop) can complete DCR → PKCE OAuth flows.
# Without it they see Entra directly, which has no registration_endpoint.
AUTH_SERVER = DCR_PROXY_URL if DCR_PROXY_URL else ISSUER
JWKS_URI = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
SCOPE_VALUE = f"{RESOURCE_URI}/access"

# Cache JWKS — refreshes every ~1h per Entra's headers
jwks_client = PyJWKClient(JWKS_URI, cache_keys=True, lifespan=3600)

# Key Vault client — uses DefaultAzureCredential, which picks up
# AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET from env vars.
_azure_credential = DefaultAzureCredential()
kv_client = SecretClient(vault_url=KEYVAULT_URL, credential=_azure_credential)

mcp = FastMCP("entra-sidecar")


def _sanitize_principal(principal_id: str) -> str:
    """Make a principal_id safe for use as a Key Vault secret name suffix.

    KV secret names allow alphanumerics + hyphens, 1-127 chars. Replace
    everything else with `-`, collapse runs, trim, and cap length.
    Entra `oid` values are already KV-safe (hex + hyphens).
    """
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", principal_id)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:100] or "anonymous"


# ── Gateway auth ────────────────────────────────────────────────────────────


def _validate_token(token: str) -> dict | None:
    """Return decoded claims if the token is a valid Entra JWT for this app, else None."""
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        return claims
    except jwt.ExpiredSignatureError:
        log.info("auth rejected: expired token")
    except jwt.InvalidAudienceError:
        log.info("auth rejected: wrong audience")
    except jwt.InvalidIssuerError:
        log.info("auth rejected: wrong issuer")
    except jwt.InvalidTokenError as e:
        log.info("auth rejected: %s", e)
    except Exception as e:
        log.warning("auth error: %s", e)
    return None


@mcp.tool()
def authenticate(headers: dict, method: str = "", path: str = "") -> dict:
    """Validate the incoming Entra JWT and return a normalized principal."""
    auth_values = headers.get("authorization") or headers.get("Authorization") or []
    if isinstance(auth_values, str):
        auth_values = [auth_values]

    token = None
    for v in auth_values:
        if isinstance(v, str) and v.lower().startswith("bearer "):
            token = v[7:].strip()
            break

    if not token:
        return {"error": {"code": "missing_credentials", "message": "No Authorization header"}}

    claims = _validate_token(token)
    if claims is None:
        return {"error": {"code": "invalid_credentials", "message": "JWT validation failed"}}

    # Prefer `oid` — tenant-scoped object id, immutable across UPN renames,
    # already KV-secret-name-safe (hex + hyphens).
    principal_id = (
        claims.get("oid")
        or claims.get("upn")
        or claims.get("preferred_username")
        or claims["sub"]
    )

    return {
        "result": {
            "id": principal_id,
            "type": "user",
            "auth_method": "entra_oidc",
            # Top-level tenant_id must match the MCPGateway's tenant or the data plane's
            # tenant guard rejects the request (404 gateway_not_found). Gateways created
            # without an explicit tenant are tenant "default"; override via GATEWAY_TENANT_ID.
            "tenant_id": os.environ.get("GATEWAY_TENANT_ID", "default"),
            "roles": claims.get("roles", []),
            "metadata": {
                "tenant_id": claims.get("tid"),
                "object_id": claims.get("oid"),
                "scopes": (claims.get("scp") or "").split() if claims.get("scp") else [],
            },
        }
    }


# ── Credential delegation ────────────────────────────────────────────────────


@mcp.tool()
async def get_connection_headers(
    principal_id: str,
    server_name: str,
    tenant_id: str = "",
    server_url: str = "",
    client_id: str = "",
    scopes: list | None = None,
    # The gateway also sends the inbound request's headers/method/path. FastMCP uses
    # strict-schema validation (additionalProperties: false), so these MUST be accepted
    # here or the whole call is rejected and credential injection fails *silently*
    # (the upstream then gets no auth → 401 → its tools never appear). Accept and ignore.
    headers: dict | None = None,
    method: str = "",
    path: str = "",
) -> dict:
    """Look up the caller's per-server credential in Key Vault.

    Two storage paths, checked in this order:

    1. OAuth bundle — KV secret `oauth-token-{server}-{principal}` (future phase).
    2. Static PAT — KV secret `{server_name}-pat-{principal}` populated manually
       by your ops team. This is the active path for the current deployment.

    If neither exists, return an empty result. The gateway treats that as
    "no delegation needed" — correct for public upstreams like DuckDuckGo.
    """
    if not principal_id or not server_name:
        return {"error": {"code": "invalid_request", "message": "principal_id and server_name are required"}}

    sanitized = _sanitize_principal(principal_id)

    # ── OAuth bundle path (future phase) ────────────────────────────────
    try:
        from datastores.token_store import get_token_store
        from oauth.oauth_client import is_token_expired, refresh_access_token, stamp_token_expiry

        bundle = get_token_store().get_token(principal_id, server_name)
    except Exception as e:
        log.warning("oauth bundle lookup raised: %s", e)
        bundle = None

    if bundle:
        if is_token_expired(bundle) and bundle.get("refresh_token"):
            log.info("oauth bundle expired for server=%s principal=%s — refreshing", server_name, sanitized)
            try:
                refreshed = await refresh_access_token(
                    token_endpoint=bundle["dcr_token_endpoint"],
                    client_id=bundle["dcr_client_id"],
                    refresh_token=bundle["refresh_token"],
                    extra_headers=bundle.get("catalog_headers") or None,
                )
                merged = {**bundle, **refreshed}
                merged = stamp_token_expiry(merged)
                get_token_store().save_token(principal_id, server_name, merged)
                bundle = merged
            except Exception as e:
                log.error("oauth refresh failed for server=%s: %s", server_name, e)
                return {"error": {"code": "expired_credentials", "message": "OAuth refresh failed"}}

        log.info("delegated oauth credential: server=%s principal=%s", server_name, sanitized)
        return {"result": {"Authorization": f"Bearer {bundle['access_token']}"}}

    # ── Static PAT path ──────────────────────────────────────────────────
    secret_name = f"{server_name}-pat-{sanitized}"
    try:
        pat = kv_client.get_secret(secret_name).value
    except ResourceNotFoundError:
        log.info("no credential configured: %s — proceeding without delegation", secret_name)
        return {"result": {}}
    except Exception as e:
        log.error("KV lookup failed for %s: %s", secret_name, e)
        return {"error": {"code": "internal_error", "message": "credential lookup failed"}}

    log.info("delegated PAT credential: server=%s principal=%s", server_name, sanitized)
    return {"result": {"Authorization": f"Bearer {pat}"}}


# ── OAuth broker tools (future phase) ───────────────────────────────────────

from oauth import oauth_server as _oauth_server

_oauth_server.init()
_oauth_server.register_tools(mcp)


# ── Telemetry stubs ─────────────────────────────────────────────────────────


@mcp.tool(name="record-counter")
def record_counter(name: str, value: int, attributes: dict | None = None) -> dict:
    return {"result": {}}


@mcp.tool(name="record-histogram")
def record_histogram(name: str, value: float, attributes: dict | None = None) -> dict:
    return {"result": {}}


@mcp.tool(name="record-gauge")
def record_gauge(name: str, value: int, attributes: dict | None = None) -> dict:
    return {"result": {}}


# ── RFC 9728 PRM endpoint ────────────────────────────────────────────────────


_PRM_PREFIX = "/.well-known/oauth-protected-resource"


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """Serve the RFC 9728 Protected Resource Metadata document.

    Per RFC 9728 §3.3, the `resource` field MUST equal the URL the client
    used to fetch this document minus the `.well-known/oauth-protected-resource`
    prefix. The gateway emits per-resource PRM URLs, so we mirror the sub-path back.
    """
    path = request.url.path
    if path.startswith(_PRM_PREFIX):
        sub = path[len(_PRM_PREFIX):]
    else:
        sub = ""

    base = GATEWAY_RESOURCE.rstrip("/")
    resource = f"{base}{sub}" if sub else base

    # NOTE: the gateway data plane overrides this `resource` field with the gateway URL when it
    # proxies the PRM (RFC 9728 §3.3 requires resource == the fetched URL), so changing it here
    # has no effect on what clients see. Documented because it matters for Entra: clients that
    # send the RFC 8707 `resource=` param (e.g. Claude Code) then present the gateway URL, which
    # Entra rejects against the app-scoped scope (AADSTS9010010). Not fixable at the sidecar.

    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [AUTH_SERVER],
            "scopes_supported": [SCOPE_VALUE],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def healthz(_: Request) -> Response:
    return Response("ok", media_type="text/plain")


def build_app() -> Starlette:
    # host_origin_protection disabled: the gateway reaches this sidecar over the in-cluster
    # Kubernetes Service DNS name, which FastMCP's default DNS-rebinding protection would reject
    # with HTTP 421 (Misdirected Request). Safe here — the sidecar is only reachable on the pod
    # network, not exposed via a Route.
    mcp_asgi = mcp.http_app(path="/mcp", host_origin_protection=False)
    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/{rest:path}", protected_resource_metadata),
            Route("/healthz", healthz),
            Mount("/", app=mcp_asgi),
        ],
        lifespan=mcp_asgi.lifespan,
    )
    return app


if __name__ == "__main__":
    import uvicorn

    log.info(
        "starting entra-sidecar: audience=%s issuer=%s gateway=%s",
        AUDIENCE,
        ISSUER,
        GATEWAY_RESOURCE,
    )
    uvicorn.run(build_app(), host="0.0.0.0", port=8080, log_level="info")
