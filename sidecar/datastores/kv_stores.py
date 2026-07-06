"""
Azure Key Vault implementations of TokenStore and OAuthStateStore.

Selected by the factories in token_store.py / oauth_state_store.py when
MCP_GATEWAY_STORE_TYPE=kv. The in-memory fallback for dev/test lives
in datastores/local_stores.py.

Key naming inside Key Vault (KV secret names must be alphanumerics +
hyphens, 1-127 chars, so we use `-` as the separator throughout):

    oauth-token-{server}-{sanitized_principal}    per-(user, server) OAuth bundle (future phase)
    oauth-state-{state_token}                     pending OAuth flow state (future phase)
    {server}-pat-{sanitized_principal}            static PAT for delegation (current phase)

The bundle and state values are JSON-encoded strings. Tags carry the
principal/server metadata for list_tokens — KV doesn't support prefix
filtering on secret names so we use tags + a list_properties_of_secrets
scan for that path.
"""

import json
import os
import re
import time

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from shared.logging_config import get_logger, log_duration

logger = get_logger("kv_stores")


_TOKEN_PREFIX = "oauth-token-"
_STATE_PREFIX = "oauth-state-"


def _sanitize(value: str) -> str:
    """Map an arbitrary string onto KV-secret-name-safe characters."""
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", value)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "anonymous"


class KeyVaultTokenStore:
    """CRUD wrapper around Azure Key Vault for per-(principal, server) OAuth tokens."""

    def __init__(self, vault_url: str, credential=None):
        credential = credential or DefaultAzureCredential()
        self._client = SecretClient(vault_url=vault_url, credential=credential)

    def _secret_name(self, principal: str, server_name: str) -> str:
        return f"{_TOKEN_PREFIX}{_sanitize(server_name)}-{_sanitize(principal)}"

    def save_token(self, principal: str, server_name: str, token_data: dict) -> None:
        name = self._secret_name(principal, server_name)
        with log_duration(logger, "[kv-token-store] save_token"):
            self._client.set_secret(
                name,
                json.dumps(token_data),
                tags={"principal": _sanitize(principal), "server": _sanitize(server_name)},
            )
        logger.info("[kv-token-store] saved token for server=%s", server_name)

    def get_token(self, principal: str, server_name: str) -> dict | None:
        name = self._secret_name(principal, server_name)
        with log_duration(logger, "[kv-token-store] get_token"):
            try:
                resp = self._client.get_secret(name)
            except ResourceNotFoundError:
                return None
            return json.loads(resp.value)

    def delete_token(self, principal: str, server_name: str) -> None:
        name = self._secret_name(principal, server_name)
        with log_duration(logger, "[kv-token-store] delete_token"):
            try:
                self._client.begin_delete_secret(name).result()
                logger.info("[kv-token-store] deleted token for server=%s", server_name)
            except ResourceNotFoundError:
                return

    def list_tokens(self, principal: str) -> dict[str, dict]:
        """Return ``{server_name: token_data}`` for every token owned by *principal*.

        KV doesn't support server-side prefix filtering, so we scan all
        secret properties and filter by tag. For POC scale this is fine.
        """
        principal_tag = _sanitize(principal)
        tokens: dict[str, dict] = {}
        with log_duration(logger, "[kv-token-store] list_tokens"):
            for prop in self._client.list_properties_of_secrets():
                if not (prop.name or "").startswith(_TOKEN_PREFIX):
                    continue
                if (prop.tags or {}).get("principal") != principal_tag:
                    continue
                try:
                    resp = self._client.get_secret(prop.name)
                    server = (prop.tags or {}).get("server", "")
                    if server:
                        tokens[server] = json.loads(resp.value)
                except ResourceNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning("[kv-token-store] read failed for %s: %s", prop.name, exc)
        return tokens


class KeyVaultOAuthStateStore:
    """CRUD wrapper around Azure Key Vault for pending OAuth flow state.

    State tokens are short-lived (~10 min) and single-use. We set an
    expiration on each secret so abandoned flows get GC'd automatically.
    """

    _TTL_SECONDS = 600

    def __init__(self, vault_url: str, credential=None):
        credential = credential or DefaultAzureCredential()
        self._client = SecretClient(vault_url=vault_url, credential=credential)

    def _secret_name(self, state_token: str) -> str:
        return f"{_STATE_PREFIX}{_sanitize(state_token)}"

    def save_state(self, state_token: str, state_data: dict) -> None:
        name = self._secret_name(state_token)
        from datetime import datetime, timezone, timedelta
        expires_on = datetime.now(timezone.utc) + timedelta(seconds=self._TTL_SECONDS)
        with log_duration(logger, "[kv-oauth-state-store] save_state"):
            self._client.set_secret(name, json.dumps(state_data), expires_on=expires_on)
        logger.info("[kv-oauth-state-store] saved state (ttl=%ds)", self._TTL_SECONDS)

    def pop_state(self, state_token: str) -> dict | None:
        """Read and delete the state for a given token (single-use)."""
        name = self._secret_name(state_token)
        with log_duration(logger, "[kv-oauth-state-store] pop_state"):
            try:
                resp = self._client.get_secret(name)
            except ResourceNotFoundError:
                return None
            try:
                data = json.loads(resp.value)
            except json.JSONDecodeError:
                logger.error("[kv-oauth-state-store] corrupted state, treating as missing")
                data = None
            try:
                self._client.begin_delete_secret(name).result()
            except ResourceNotFoundError:
                logger.warning("[kv-oauth-state-store] state already deleted (duplicate callback)")
            return data

    def delete_state(self, state_token: str) -> None:
        name = self._secret_name(state_token)
        with log_duration(logger, "[kv-oauth-state-store] delete_state"):
            try:
                self._client.begin_delete_secret(name).result()
            except ResourceNotFoundError:
                return
