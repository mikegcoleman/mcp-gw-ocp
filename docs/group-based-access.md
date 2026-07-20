# Milestone 3 — Group-Based MCP Server Access Control

This guide builds on Milestone 2 (Entra ID auth + per-user Key Vault credentials). It adds
**role-based server visibility**: users in different Entra groups see different sets of MCP
servers, enforced by the gateway's built-in policy engine.

No sidecar code changes are required — Entra App Roles already surface in the JWT `roles` claim,
and the sidecar's `authenticate()` tool already extracts and returns them to the gateway.

---

## Demo scenario

| User | Email | Role | Sees |
|------|-------|------|------|
| alice | `msmikecol@hotmail.com` | `mcp-team-a` | DuckDuckGo + GitHub + **Opine** |
| bob | `mike.coleman@docker.co` | `mcp-team-b` | DuckDuckGo + GitHub + **Granola** |

DuckDuckGo and GitHub are open to all authenticated users. Opine and Granola are team-scoped:
a user without the matching role simply does not see those servers in their tool list (no error,
just invisible).

---

## Prerequisites

- Milestone 2 complete and verified: sidecar running, Entra JWT auth working, GitHub and
  DuckDuckGo reachable for an authenticated user.
- `oc` CLI authenticated to the cluster.
- `az` CLI authenticated to the Azure tenant.

---

## 1. How it works

```
User request with Entra JWT
        ↓
Sidecar authenticate() validates JWT → extracts principal.roles from JWT "roles" claim
        ↓
Gateway policy engine evaluates MCPGateway.spec.policies.rules against principal.roles
        ↓
Flip-to-deny: because allow rules exist, any server not matched by an allow rule is invisible
        ↓
tools/list response contains only the servers the user's roles permit
```

**Flip-to-deny** is the key behavior: the moment any `allow` rule exists in the ruleset, every
server that doesn't match an allow rule is hidden from that user. This means:

- duckduckgo and github have unconditional allow rules (no `role` field) → visible to everyone
- opine has `role: mcp-team-a` → visible only to alice
- granola has `role: mcp-team-b` → visible only to bob

**OAuth PKCE for team servers:** Opine and Granola use `auth_delegation: gateway` with OAuth.
On the first tool call, the upstream returns 401 and the gateway's OAuth broker calls the sidecar
to start a PKCE flow. After the user completes consent, the token is stored in Key Vault and
injected automatically on subsequent calls. See [Step 6](#6-oauth-first-use-flow) below.

---

## 2. Azure setup

Follow **[docs/azure-setup.md §1c-2](azure-setup.md#1c-2-add-team-scoped-app-roles-milestone-3)**
to create the `mcp-team-a` and `mcp-team-b` app roles in your Entra app registration.

Then follow **[docs/azure-setup.md §1g-2](azure-setup.md#1g-2-create-entra-security-groups-and-add-users-milestone-3)**
to create the `mcp-team-a` and `mcp-team-b` security groups, add alice and bob to them, and
assign each group to its app role.

**Verify the roles appear in tokens** before deploying:

```bash
APPID=<your-application-client-id>

# As alice:
az login --allow-no-subscriptions --username mikegcoleman+alice@gmail.com
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# Expected: ["MCPGateway.User", "mcp-team-a"]

# As bob:
az login --allow-no-subscriptions --username mikegcoleman+bob@gmail.com
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# Expected: ["MCPGateway.User", "mcp-team-b"]
```

If `roles` is missing or empty, the user is not yet assigned to the app role. Role assignments
can take a minute to propagate — re-acquire the token after a short wait.

---

## 3. Apply the catalog and policy

`catalog-and-gateway.yaml` now contains all four servers (duckduckgo, github, opine, granola)
and the `policies.rules` block. Apply it and restart the control plane to pick up the new
catalog entries:

```bash
oc apply -f catalog-and-gateway.yaml

# CP restart is required because the catalog ConfigMap changed (new server entries).
# Policy-only changes to the MCPGateway CR would NOT require this.
oc rollout restart deploy/mcp-gw-cp -n mcp-gateway
oc rollout status deploy/mcp-gw-cp -n mcp-gateway --timeout=120s

# Wait for the gateway to reach Active
oc get mcpgw pov-gateway -n mcp-gateway -w
```

---

## 4. Verify server visibility

Use the MCP session handshake (same pattern as README Step 11c) with each user's token. The
`tools/list` response should only contain servers the user's role permits.

```bash
GATEWAY_URL=$(oc get mcpgw pov-gateway -n mcp-gateway -o jsonpath='{.status.endpoints.sk}')
APPID=<your-application-client-id>

# ---- Test as alice (msmikecol@hotmail.com — should see duckduckgo, github, opine, NOT granola) ----
az login --allow-no-subscriptions --username msmikecol@hotmail.com
ALICE_TOKEN=$(az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv)

SID=$(curl -sS -k -D - -o /dev/null -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')

curl -sS -k -o /dev/null -X POST "$GATEWAY_URL" -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq '[.result.tools[].name]'
# Expect: duckduckgo__search, github__*, opine__* present; NO granola__* tools

# ---- Test as bob (mike.coleman@docker.co — should see duckduckgo, github, granola, NOT opine) ----
az login --allow-no-subscriptions --username mike.coleman@docker.co
BOB_TOKEN=$(az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv)
# ... repeat the same handshake with BOB_TOKEN ...
# Expect: duckduckgo__search, github__*, granola__* present; NO opine__* tools
```

---

## 5. Deploy the Entra DCR proxy (enables full OAuth in MCP clients)

The DCR proxy bridges RFC 7591 Dynamic Client Registration to Entra, so Claude Code and
Claude Desktop can complete a full OAuth PKCE flow without a `headersHelper` script.
It runs as a lightweight pod and serves on `/dcr` under the existing DP hostname — no
new Route hostname or TLS certificate is needed.

### 5a. Build the DCR proxy image

The DCR proxy source is in `temp/mcp-gateway-entra-dcr-proxy/`. Build it into the cluster's
internal registry (same approach as the Entra sidecar in Milestone 2 Step 1B):

```bash
oc new-build --binary --strategy=docker --name=entra-dcr-proxy -n mcp-gateway
oc start-build entra-dcr-proxy \
  --from-dir=temp/mcp-gateway-entra-dcr-proxy \
  -n mcp-gateway --follow
```

> The Dockerfile uses `dhi.io/python:3.12` as its base. If the cluster build pod can't pull
> that image, swap it for `python:3.12-slim` in a local copy:
> ```bash
> sed 's|FROM dhi.io/python:3.12|FROM python:3.12-slim|' \
>   temp/mcp-gateway-entra-dcr-proxy/Dockerfile > /tmp/Dockerfile.build
> oc start-build entra-dcr-proxy --from-dir=temp/mcp-gateway-entra-dcr-proxy -n mcp-gateway \
>   --from-file=Dockerfile=/tmp/Dockerfile.build --follow
> ```

### 5b. Create the DCR proxy credentials secret

This was done as part of Azure setup (azure-setup.md §1h). Verify the secret exists:

```bash
oc get secret entra-dcr-proxy-credentials -n mcp-gateway
```

If it's missing, create it:

```bash
oc create secret generic entra-dcr-proxy-credentials \
  --from-literal=entra-tenant-id=<TENANT_ID> \
  --from-literal=dcr-proxy-client-id=<DCR_PROXY_APP_CLIENT_ID> \
  --from-literal=dcr-proxy-client-secret=<DCR_PROXY_APP_CLIENT_SECRET> \
  --from-literal=entra-app-client-id=<GATEWAY_APP_CLIENT_ID> \
  -n mcp-gateway
```

See azure-setup.md §1h for where each value comes from.

### 5c. Deploy the proxy

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')

oc process -f manifests/entra-dcr-proxy.yaml \
  -p CLUSTER_DOMAIN="$CLUSTER_DOMAIN" \
  | oc apply -n mcp-gateway -f -

oc rollout status deploy/entra-dcr-proxy -n mcp-gateway
```

Verify the health endpoint is reachable through the Route:

```bash
curl -s "https://mcp-gw-dp.$CLUSTER_DOMAIN/dcr/health"
# expect: {"status":"ok"}
```

### 5d. Tell the gateway DP about the DCR proxy

The DP reads `DCR_PROXY_URL` from its environment to know where to forward Dynamic Client
Registration requests. Patch it into the running `GatewayServiceConfig`:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')
DCR_URL="https://mcp-gw-dp.$CLUSTER_DOMAIN/dcr"

# JSON Patch appends to extraEnv without overwriting existing entries
oc patch gatewayserviceconfig mcp-gw -n mcp-gateway --type=json -p \
  "[{\"op\":\"add\",\"path\":\"/spec/dataPlane/extraEnv/-\",\"value\":{\"name\":\"DCR_PROXY_URL\",\"value\":\"$DCR_URL\"}}]"

oc rollout status deploy/mcp-gw-dp -n mcp-gateway
```

### 5e. Connect MCP clients — full OAuth (no helper script)

With the DCR proxy running, Claude Code, Claude Desktop, and VS Code can all authenticate
via a standard browser OAuth flow:

```bash
# One-line registration per user — no headersHelper needed
claude mcp add --transport http pov-gateway <GATEWAY_URL> --scope user
```

On first connection the client opens a browser window to sign in with the user's Entra account.
Each user's tool list will differ based on their role assignment — alice sees Opine, bob sees
Granola, both see DuckDuckGo and GitHub.

Use `client_setup.sh` to provision the demo sandboxes for alice and bob:

```bash
./client_setup.sh
```

---

## 6. OAuth first-use flow

Opine and Granola use `auth_delegation: gateway` with OAuth PKCE. On the first tool call
after connecting:

1. The gateway calls the upstream with no credentials → upstream returns 401
2. The gateway's OAuth broker calls the sidecar's `opine-authorize` (or `granola-authorize`)
   primordial tool
3. The sidecar performs OAuth discovery on the upstream, registers dynamically (DCR), and
   generates a PKCE challenge
4. The MCP client receives an authorization URL and opens it in a browser
5. The user completes consent in the upstream's OAuth UI
6. The callback returns the code; the sidecar exchanges it for a token and stores it in
   Key Vault
7. Subsequent calls inject the token automatically — no user action required

> **Note on Opine OAuth DCR:** Opine must support RFC 7591 Dynamic Client Registration for the
> sidecar's broker to register automatically. Verify this with the Opine documentation before
> the demo. If DCR is not supported, a static OAuth client ID/secret can be pre-configured in
> the catalog's `oauth` block.

---

## 7. Modifying the policy

To add more servers or roles, extend `policies.rules` in `catalog-and-gateway.yaml` and
re-apply. The gateway's DP picks up policy changes immediately — no restart needed for
policy-only changes (only catalog ConfigMap changes require a CP restart).

**To clear all rules** and revert to allow-all, use an explicit empty list:

```yaml
policies:
  rules: []   # explicit empty list — omitting the policies block entirely is a no-op
```

**To add a server visible to both teams:**

```yaml
- serverName: new-shared-server
  effect: allow   # no role field = all authenticated users
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Team server not visible at all | No matching allow rule (flip-to-deny) | Add an allow rule for that server + role combo |
| Shared server (duckduckgo/github) gone for everyone | Their allow rules were removed | Re-add unconditional allow rules with no `role` field |
| Server visible to wrong user | Rule missing the `role` field | Add `role: mcp-team-x` to the rule |
| `roles` claim empty in token | User not assigned to the app role in Entra | Enterprise apps → Users and groups → assign the role |
| Policy change not taking effect | `policies` block omitted (no-op update) | Use `rules: []` to clear, or add explicit rules and re-apply |
| OAuth flow not triggering | `invokePrimordial` rule missing | Add `action: invokePrimordial` + `toolName: <server>-authorize` rule |
| GitHub tools present but calls fail with 401 | No `github-pat-<oid>` in Key Vault for this user | Add the secret per [azure-setup.md §3](azure-setup.md#3-load-pat-secrets-into-key-vault) |
| Catalog changes not taking effect | CP not restarted after ConfigMap change | `oc rollout restart deploy/mcp-gw-cp -n mcp-gateway` |
| Claude Code OAuth browser never opens | DCR proxy not deployed or `DCR_PROXY_URL` not patched into DP | Deploy `manifests/entra-dcr-proxy.yaml` and run the `oc patch` from Step 5c |
| DCR proxy pod crash-looping | `entra-dcr-proxy-credentials` secret missing or has wrong keys | Verify with `oc describe secret entra-dcr-proxy-credentials -n mcp-gateway` |
| `/dcr/health` returns 503 from Route | Proxy pod not ready | `oc get pod -l app=entra-dcr-proxy -n mcp-gateway` and check logs |
