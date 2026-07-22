# Milestone 3 — Group-Based MCP Server Access Control

This guide builds on Milestone 2 (Entra ID auth + per-user Key Vault credentials). It adds
**role-based server visibility**: users in different Entra groups see different sets of MCP
servers, enforced by the gateway's built-in policy engine.

No sidecar code changes are required — Entra App Roles already surface in the JWT `roles` claim,
and the sidecar's `authenticate()` tool already extracts and returns them to the gateway.

---

## Scenario

| User | Role | Sees |
|------|------|------|
| user-a | `mcp-team-a` | DuckDuckGo + GitHub + **Granola** (`team-a-granola`) |
| user-b | `mcp-team-b` | DuckDuckGo + GitHub + **Notion** (`team-b-notion`) |

Substitute `user-a` and `user-b` with actual Entra users in your tenant. They can be existing
accounts or new ones created for this deployment — the only requirement is that each is assigned
to the correct Entra App Role (see Step 2).

DuckDuckGo and GitHub are open to all authenticated users. Granola and Notion are team-scoped:
a user without the matching role simply does not see those servers in their tool list (no error,
just invisible).

> **Server name prefixes:** team-managed servers are named `team-a-<server>` / `team-b-<server>`
> to prevent collisions if two teams independently add a server with the same base name. Tools are
> exposed with the full server name as prefix: `team-a-granola__list_meetings`,
> `team-b-notion__search`, etc.

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
- team-a-granola has `role: mcp-team-a` → visible only to members of `mcp-team-a`
- team-b-notion has `role: mcp-team-b` → visible only to members of `mcp-team-b`

**OAuth PKCE for team servers:** Granola and Notion use `auth_delegation: gateway` with OAuth.
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

# As user-a (<USER_A_EMAIL> — expects mcp-team-a):
az login --allow-no-subscriptions --username <USER_A_EMAIL>
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# Expected: ["MCPGateway.User", "mcp-team-a"]

# As user-b (<USER_B_EMAIL> — expects mcp-team-b):
az login --allow-no-subscriptions --username <USER_B_EMAIL>
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# Expected: ["MCPGateway.User", "mcp-team-b"]
```

If `roles` is missing or empty, the user is not yet assigned to the app role. Role assignments
can take a minute to propagate — re-acquire the token after a short wait.

---

## 3. Apply the catalog and policy

`catalog-and-gateway.yaml` now contains all four servers (duckduckgo, github, granola, notion)
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

# ---- Test as user-a (<USER_A_EMAIL> — should see duckduckgo, github, granola, NOT notion) ----
az login --allow-no-subscriptions --username <USER_A_EMAIL>
USER_A_TOKEN=$(az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv)

SID=$(curl -sS -k -D - -o /dev/null -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $USER_A_TOKEN" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')

curl -sS -k -o /dev/null -X POST "$GATEWAY_URL" -H "Authorization: Bearer $USER_A_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $USER_A_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq '[.result.tools[].name]'
# Expect: duckduckgo__search, github__*, team-a-granola__* present; NO team-b-notion__* tools

# ---- Test as user-b (<USER_B_EMAIL> — should see duckduckgo, github, team-b-notion, NOT team-a-granola) ----
az login --allow-no-subscriptions --username <USER_B_EMAIL>
USER_B_TOKEN=$(az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv)
# ... repeat the same handshake with USER_B_TOKEN ...
# Expect: duckduckgo__search, github__*, team-b-notion__* present; NO team-a-granola__* tools
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

### 5d. Tell the sidecar about the DCR proxy

The sidecar serves `/.well-known/oauth-protected-resource` — it must advertise the DCR proxy URL
so MCP clients discover DCR + PKCE endpoints instead of going to Entra directly. Patch it into
the sidecar Deployment:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')
DCR_URL="https://mcp-gw-dp.$CLUSTER_DOMAIN/dcr"

oc set env deploy/mcp-entra-sidecar DCR_PROXY_URL="$DCR_URL" -n mcp-gateway
oc rollout status deploy/mcp-entra-sidecar -n mcp-gateway
```

### 5e. Connect MCP clients — full OAuth (no helper script)

With the DCR proxy running, Claude Code, Claude Desktop, and VS Code can all authenticate
via a standard browser OAuth flow:

```bash
# One-line registration per user — no headersHelper needed
claude mcp add --transport http pov-gateway <GATEWAY_URL> --scope user
```

On first connection the client opens a browser window to sign in with the user's Entra account.
Each user's tool list will differ based on their role assignment — `mcp-team-a` members see
Granola, `mcp-team-b` members see Notion, everyone sees DuckDuckGo and GitHub.

> **Multi-gateway limitation.** `PROXY_BASE_URL` is a static env var — it hardcodes the proxy
> to one gateway's hostname. If you deploy multiple gateways on the same cluster, each needs its
> own proxy instance and its own Route. The clean long-term fix is for the proxy to handle
> `/.well-known/oauth-protected-resource/*` as a wildcard, extract the gateway suffix from the
> path dynamically, and return it as the `resource` value in the PRM response — then one proxy
> instance serves all gateways. That change lives in the proxy source, not in the manifests here.

---

## 6. OAuth first-use flow

Granola and Notion use `auth_delegation: gateway` with OAuth PKCE. On the first tool call
after connecting:

1. The gateway calls the upstream with no credentials → upstream returns 401
2. The gateway's OAuth broker calls the sidecar's `team-a-granola-authorize` (or `team-b-notion-authorize`)
   primordial tool
3. The sidecar performs OAuth discovery on the upstream, registers dynamically (DCR), and
   generates a PKCE challenge
4. The MCP client receives an authorization URL and opens it in a browser
5. The user completes consent in the upstream's OAuth UI
6. The callback returns the code; the sidecar exchanges it for a token and stores it in
   Key Vault
7. Subsequent calls inject the token automatically — no user action required

> **Note on OAuth DCR:** Granola and Notion must support RFC 7591 Dynamic Client Registration
> for the sidecar's broker to register automatically. Verify this with each service's
> documentation before the demo. If DCR is not supported, a static OAuth client ID/secret can
> be pre-configured in the catalog's `oauth` block.

---

## 7. GitOps pipeline — team-managed changes via GitHub Actions

The `deploy-team-a.yml` workflow lets the team-a owner push changes to this repo and have them
applied to the cluster automatically — no manual `oc apply` needed.

**Ownership split:** the team-a owner manages their catalog ConfigMap and policy ConfigMap. The
MCPGateway CR (`mcpgateway.yaml`) and GatewayServiceConfig remain IT-owned; the team pipeline
never touches them.

```
team-a pushes ──► catalog-team-a.yaml          (team-a-granola catalog entry)
                + manifests/team-a-policy.yaml  (tool-level deny rules)

IT controls   ──► mcpgateway.yaml               (server visibility + OAuth primordials)
                + gatewayserviceconfig.yaml      (plugin wiring, Entra config)
```

### 7a. Apply the pipeline RBAC

```bash
kubectl apply -f manifests/rbac-pipeline.yaml -n mcp-gateway
```

This creates the `team-a-pipeline` ServiceAccount with scoped RBAC:
- MCPServer full CRUD (for in-cluster server pods)
- ConfigMap create/patch (for `catalog-team-a` and `team-a-policy`)

### 7b. Mint a token and add GitHub secrets

```bash
OCP_TOKEN_TEAM_A=$(kubectl create token team-a-pipeline -n mcp-gateway --duration=8760h)
OCP_SERVER=$(oc whoami --show-server)
```

Add two secrets to the GitHub repo (**Settings → Secrets and variables → Actions**):

| Secret | Value |
|--------|-------|
| `OCP_SERVER` | output of `oc whoami --show-server` |
| `OCP_TOKEN_TEAM_A` | output of the `kubectl create token` command above |

### 7c. How it works

On every push to `main` that touches `catalog-team-a.yaml`, `manifests/team-a-policy.yaml`,
or `manifests/mcpserver-team-a-*.yaml`, the workflow:
1. Applies `catalog-team-a.yaml` (catalog ConfigMap for team-a's servers)
2. Applies `manifests/team-a-policy.yaml` (tool-level deny rules — see §8)
3. Applies any `mcpserver-team-a-*.yaml` manifests (for in-cluster servers)
4. Restarts the control plane to pick up catalog changes
5. Waits for the gateway to reach `Active`

Policy ConfigMap changes take effect within ~60 seconds (kubelet sync) without any restart.
The CP restart step is there for catalog changes and is safe to run idempotently.

---

## 8. Policy architecture — two layers

The gateway enforces policy at two independent layers. They are **additive**: a request must
pass both to succeed.

```
Request arrives at DP
        ↓
[Layer 1 — MCPGateway CR rules]   IT-owned • server visibility + top-level tool blocks
        ↓ (allowed by CR)
[Layer 2 — Sidecar evaluate_policy]   Team-owned • per-team tool-level deny rules
        ↓ (allowed by both)
Call reaches upstream server
```

**Deny-wins across both layers:** a deny anywhere blocks the call, regardless of other allows.

### Layer 1 — MCPGateway CR policies (IT-owned)

Controlled by IT via `mcpgateway.yaml`. Used for **server visibility** (which teams see which
servers) and cross-cutting tool blocks (deny for everyone, or deny for a role).

```yaml
# in mcpgateway.yaml → spec.policies.rules
- serverName: team-a-granola
  effect: allow
  role: mcp-team-a          # only alice's group sees this server

- serverName: duckduckgo
  effect: allow              # no role → visible to all authenticated users
```

To add a tool-level IT deny (blocks everyone, regardless of team):

```yaml
- serverName: team-a-granola
  toolName: list_meetings    # backend name, NOT the prefixed "team-a-granola__list_meetings"
  action: invoke
  effect: deny
  reason: "list_meetings restricted by central IT"
```

CR changes take effect immediately after `kubectl apply` — no CP restart needed for policy-only
changes (catalog ConfigMap changes still require a CP restart).

### Layer 2 — Sidecar ConfigMap policy (team-owned)

Controlled by alice via `manifests/team-a-policy.yaml`. The sidecar's `evaluate_policy` MCP
tool is called by the gateway DP for every tool invocation. It reads YAML rule files from
`/etc/mcp-policy/` (volume-mounted from the `team-a-policy` ConfigMap) on each call.

```yaml
# in manifests/team-a-policy.yaml → data.policy.yaml → rules
rules:
  - serverName: team-a-granola
    toolName: list_meetings
    effect: deny
    reason: "list_meetings temporarily restricted by team-a policy"
```

Rule fields:
| Field | Required | Notes |
|-------|----------|-------|
| `serverName` | yes | must match the backend name (`team-a-granola`, not the prefixed tool name) |
| `toolName` | no | omit to match all tools on this server |
| `effect` | yes | only `deny` is evaluated; allow is the default |
| `reason` | no | surfaced to the caller in the error response |

ConfigMap updates propagate to the sidecar pod within ~60 seconds (kubelet volume sync) — **no
sidecar or DP restart needed**. The sidecar re-reads the files on every `evaluate_policy` call.

### Demo flow — self-service tool block

1. user-a connects → `team-a-granola__list_meetings` works ✓
2. Uncomment the deny rule in `manifests/team-a-policy.yaml`, push to `main`
3. `deploy-team-a.yml` applies the ConfigMap update (~30s including CP rollout for catalog)
4. Within ~60s the kubelet syncs the volume; user-a's next `list_meetings` call is **denied**
   (response includes the `reason` string from the rule)
5. Revert: comment out the rule, push → pipeline applies → tool restored within ~60s

The team-a owner can self-serve this without touching `mcpgateway.yaml` or involving IT.

---

## 9. Modifying the policy

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
| OAuth flow not triggering | `invokePrimordial` rule missing, or `oauth` plugin not in pluginConfig | Add `action: invokePrimordial` + `toolName: team-a-granola-authorize` (or `team-b-notion-authorize`) rule; to enable the gateway OAuth broker add `oauth: {provider: mcp, server: <sidecar-url>}` to pluginConfig **and** set `MCP_GATEWAY_OAUTH_PORT=8082` on the **sidecar** (not the DP) |
| OAuth flow fails with `ForbiddenByRbac` / `setSecret` denied | SP has `Key Vault Secrets User` (read-only) but OAuth token write requires `Key Vault Secrets Officer` | `az role assignment create --assignee <SP_APP_ID> --role "Key Vault Secrets Officer" --scope <KV_ID>` (see azure-setup.md §2a) |
| GitHub tools present but calls fail with 401 | No `github-pat-<oid>` in Key Vault for this user | Add the secret per [azure-setup.md §3](azure-setup.md#3-load-pat-secrets-into-key-vault) |
| Catalog changes not taking effect | CP not restarted after ConfigMap change | `oc rollout restart deploy/mcp-gw-cp -n mcp-gateway` |
| Tool blocked unexpectedly (Layer 2) | A deny rule in `team-a-policy` ConfigMap | `kubectl get cm team-a-policy -n mcp-gateway -o jsonpath='{.data.policy\.yaml}'` to inspect rules |
| Policy ConfigMap change not taking effect | Kubelet volume sync delay (~60s) or pod restart needed | Wait ~60s after `kubectl apply`; verify with `kubectl exec <sidecar-pod> -- cat /etc/mcp-policy/policy.yaml` |
| `evaluate_policy` not called (no sidecar logs) | `plugins.policy` key missing from GatewayServiceConfig | Patch pluginConfig: `plugins.policy.provider: mcp` + `plugins.policy.server: http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp`; delete DP pod to force config reload |
| Claude Code OAuth browser never opens | DCR proxy not deployed or `DCR_PROXY_URL` not set on sidecar | Deploy `manifests/entra-dcr-proxy.yaml` and run `oc set env` from Step 5d |
| DCR proxy pod crash-looping | `entra-dcr-proxy-credentials` secret missing or has wrong keys | Verify with `oc describe secret entra-dcr-proxy-credentials -n mcp-gateway` |
| `/dcr/health` returns 503 from Route | Proxy pod not ready | `oc get pod -l app=entra-dcr-proxy -n mcp-gateway` and check logs |
