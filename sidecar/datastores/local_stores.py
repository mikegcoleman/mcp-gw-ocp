"""
File-based token and OAuth state stores for local development.

Drop-in replacements for the Key Vault stores in kv_stores.py.
Stores JSON files under a configurable base directory (default: /tmp/mcp-gateway/).

No Azure credentials required. Not suitable for production (no encryption,
no multi-replica coordination, ephemeral on pod restart).

Activated explicitly via MCP_GATEWAY_STORE_TYPE=local.
"""

import json
import os

from shared.logging_config import get_logger

logger = get_logger("local_stores")


class LocalTokenStore:
    """File-based token store for local development."""

    def __init__(self, base_dir: str = "/tmp/mcp-gateway/tokens"):
        self._base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        logger.info("[local-token-store] initialized at %s", base_dir)

    def _token_path(self, principal: str, server_name: str) -> str:
        safe_principal = principal.replace("/", "_")
        d = os.path.join(self._base_dir, safe_principal)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{server_name}.json")

    def save_token(self, principal: str, server_name: str, token_data: dict) -> None:
        path = self._token_path(principal, server_name)
        with open(path, "w") as f:
            json.dump(token_data, f)
        logger.info("[local-token-store] saved token for server=%s", server_name)

    def get_token(self, principal: str, server_name: str) -> dict | None:
        path = self._token_path(principal, server_name)
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def delete_token(self, principal: str, server_name: str) -> None:
        path = self._token_path(principal, server_name)
        try:
            os.remove(path)
            logger.info("[local-token-store] deleted token for server=%s", server_name)
        except FileNotFoundError:
            pass

    def list_tokens(self, principal: str) -> dict[str, dict]:
        safe_principal = principal.replace("/", "_")
        d = os.path.join(self._base_dir, safe_principal)
        tokens: dict[str, dict] = {}
        if not os.path.isdir(d):
            return tokens
        for filename in os.listdir(d):
            if not filename.endswith(".json"):
                continue
            server_name = filename[:-5]
            try:
                with open(os.path.join(d, filename)) as f:
                    tokens[server_name] = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("[local-token-store] failed to read token for server=%s", server_name)
        return tokens


class LocalOAuthStateStore:
    """File-based OAuth state store for local development."""

    def __init__(self, base_dir: str = "/tmp/mcp-gateway/oauth-state"):
        self._base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        logger.info("[local-oauth-state-store] initialized at %s", base_dir)

    def _state_path(self, state_token: str) -> str:
        return os.path.join(self._base_dir, f"{state_token}.json")

    def save_state(self, state_token: str, state_data: dict) -> None:
        path = self._state_path(state_token)
        with open(path, "w") as f:
            json.dump(state_data, f)
        logger.info("[local-oauth-state-store] saved state")

    def pop_state(self, state_token: str) -> dict | None:
        path = self._state_path(state_token)
        try:
            with open(path) as f:
                data = json.load(f)
            os.remove(path)
            return data
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.error("[local-oauth-state-store] corrupted state, treating as missing")
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return None

    def delete_state(self, state_token: str) -> None:
        path = self._state_path(state_token)
        try:
            os.remove(path)
            logger.info("[local-oauth-state-store] deleted state")
        except FileNotFoundError:
            pass
