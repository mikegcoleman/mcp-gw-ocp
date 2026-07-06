"""
OAuth state store factory for pending OAuth authorization flows.

Selects a backend by env var ``MCP_GATEWAY_STORE_TYPE``. Backends share a
common interface: ``save_state``, ``pop_state``, ``delete_state``.

Persisting pending state across replicas (rather than holding it in
memory) lets the OAuth callback land on a different pod from the one
that initiated the authorize flow.
"""

import os

from shared.logging_config import get_logger

logger = get_logger("oauth_state_store")

_oauth_state_store = None


def get_oauth_state_store():
    """Return the module-level OAuth state store singleton, creating it on first call."""
    global _oauth_state_store
    if _oauth_state_store is not None:
        return _oauth_state_store

    store_type = os.environ.get("MCP_GATEWAY_STORE_TYPE", "auto")
    kv_url = os.environ.get("AZURE_KEYVAULT_URL", "")

    if store_type == "auto":
        store_type = "kv" if kv_url else "local"

    if store_type == "kv":
        if not kv_url:
            raise RuntimeError("AZURE_KEYVAULT_URL must be set for the KV state store")
        from datastores.kv_stores import KeyVaultOAuthStateStore
        logger.info("[oauth-state-store] using Azure Key Vault store")
        _oauth_state_store = KeyVaultOAuthStateStore(vault_url=kv_url)
        return _oauth_state_store

    if store_type == "local":
        from datastores.local_stores import LocalOAuthStateStore
        logger.info("[oauth-state-store] using local file-based store")
        _oauth_state_store = LocalOAuthStateStore()
        return _oauth_state_store

    raise RuntimeError(f"Unknown MCP_GATEWAY_STORE_TYPE: {store_type}")
