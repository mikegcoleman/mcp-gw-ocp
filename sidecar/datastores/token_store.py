"""
Token store factory for per-(principal, server) OAuth token bundles.

Selects a backend by env var ``MCP_GATEWAY_STORE_TYPE``. Backends share a
common interface: ``save_token``, ``get_token``, ``delete_token``, and
``list_tokens``.

  - ``kv``    → Azure Key Vault (requires ``AZURE_KEYVAULT_URL``)
  - ``local`` → file-based fallback for dev / tests
  - ``auto``  → KV if ``AZURE_KEYVAULT_URL`` set, otherwise ``local``
"""

import os

from shared.logging_config import get_logger

logger = get_logger("token_store")

_token_store = None


def get_token_store():
    """Return the module-level token store singleton, creating it on first call."""
    global _token_store
    if _token_store is not None:
        return _token_store

    store_type = os.environ.get("MCP_GATEWAY_STORE_TYPE", "auto")
    kv_url = os.environ.get("AZURE_KEYVAULT_URL", "")

    if store_type == "auto":
        store_type = "kv" if kv_url else "local"

    if store_type == "kv":
        if not kv_url:
            raise RuntimeError("AZURE_KEYVAULT_URL must be set for the KV token store")
        from datastores.kv_stores import KeyVaultTokenStore
        logger.info("[token-store] using Azure Key Vault store")
        _token_store = KeyVaultTokenStore(vault_url=kv_url)
        return _token_store

    if store_type == "local":
        from datastores.local_stores import LocalTokenStore
        logger.info("[token-store] using local file-based store")
        _token_store = LocalTokenStore()
        return _token_store

    raise RuntimeError(f"Unknown MCP_GATEWAY_STORE_TYPE: {store_type}")
