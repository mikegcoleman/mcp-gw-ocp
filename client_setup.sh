#!/usr/bin/env bash
# Provision the two demo client sandboxes (alice, bob).
#
# Run on the DEMO HOST. Creates an sbx sandbox for each user (in ~/src/<user>)
# and registers the MCP gateway. The DCR proxy enables full OAuth — each user
# will be prompted to sign in with their Entra account the first time they
# open the sandbox in Claude Code.
#
# Usage:
#   ./client_setup.sh                              # looks up gateway URL from cluster
#   GATEWAY_URL=<url> ./client_setup.sh            # skip oc lookup
#
# Users:
#   alice  msmikecol@hotmail.com   mcp-team-a → GitHub + DuckDuckGo + Opine
#   bob    mike.coleman@docker.co  mcp-team-b → GitHub + DuckDuckGo + Granola
set -euo pipefail

BASE_DIR="${BASE_DIR:-$HOME/src}"
GATEWAY_NAMESPACE="${GATEWAY_NAMESPACE:-mcp-gateway}"
GATEWAY_CR="${GATEWAY_CR:-pov-gateway}"

# Resolve gateway URL from cluster if not provided
if [ -z "${GATEWAY_URL:-}" ]; then
  echo "Looking up gateway URL from cluster..."
  GATEWAY_URL=$(oc get mcpgw "$GATEWAY_CR" -n "$GATEWAY_NAMESPACE" \
    -o jsonpath='{.status.endpoints.sk}')
fi
echo "Gateway URL : $GATEWAY_URL"
echo "Base dir    : $BASE_DIR"
echo ""

setup_user() {
  local name="$1"
  local workdir="$BASE_DIR/$name"
  echo "=== $name ==="

  mkdir -p "$workdir"

  # Create (or reuse) the sandbox
  if out=$(sbx create --name "$name" claude "$workdir" 2>&1); then
    echo "[$name] sandbox created"
  elif echo "$out" | grep -qi "already exists"; then
    echo "[$name] sandbox already exists, reusing"
  else
    echo "[$name] ERROR: $out" >&2; exit 1
  fi

  # Register the gateway — DCR proxy handles OAuth; no headersHelper needed
  sbx exec "$name" -- claude mcp remove pov-gateway --scope user 2>/dev/null || true
  sbx exec "$name" -- claude mcp add --transport http pov-gateway "$GATEWAY_URL" --scope user
  echo "[$name] gateway registered → $GATEWAY_URL"
  echo ""
}

setup_user alice
setup_user bob

echo "Both sandboxes ready."
echo ""
echo "  alice → ~/src/alice  (msmikecol@hotmail.com,  mcp-team-a → Opine)"
echo "  bob   → ~/src/bob    (mike.coleman@docker.co, mcp-team-b → Granola)"
echo ""
echo "Each user will be prompted to sign in with their Entra account the first"
echo "time Claude Code connects to the gateway."
