# Milestone 2 — Entra ID auth + per-user credentials (plugin sidecar)

This continues from the main OpenShift guide (`README.md`). **It assumes Milestone 1 is
complete and verified:** OpenShift is up, the operator + gateway are installed, the data plane
is running with the built-in static **bearer token** auth, and the **DuckDuckGo** MCP server is
reachable through the gateway.

Milestone 2 swaps that single shared bearer token for **real per-user identity** (Entra ID JWTs)
and adds **per-user upstream credentials**: users authenticate once with their corporate Entra
account, and the gateway transparently injects *their* GitHub PAT (looked up in Azure Key Vault)
when calling the GitHub MCP server — no shared tokens, nothing in the client but the user's JWT.

This is delivered with the **Entra sidecar** (a FastMCP server that speaks MCP over HTTP). It runs
as its own Deployment and is wired into the gateway via `dataPlane.pluginConfig`, which points the
gateway's `gateway_auth` and credential-delegation plugins at the sidecar's in-cluster URL.

```
MCP client ──Bearer <Entra JWT>──▶ Data Plane
   DP ─authenticate(jwt)──────────▶ Entra sidecar ──▶ Entra JWKS   (who is the user? → principal.id = oid)
   DP ─get_connection_headers()───▶ Entra sidecar ──▶ Azure Key Vault  (github-pat-<oid>)
   DP ──Authorization: Bearer <user PAT>──▶ GitHub MCP server ──▶ GitHub API
```

## Nothing here re-does Milestone 1

The namespace, operator, gateway, SCC grants from Step 3, and the DuckDuckGo `MCPServer` all
already exist. The only changes Milestone 2 makes are **additive**: new Azure resources, the
sidecar Deployment, a GitHub `MCPServer`, one extra SCC grant, and a **one-time re-wire of the
gateway's `dataPlane.pluginConfig`** from the bearer plugin to the sidecar (Step 8).

> **Prerequisite — Azure.** You need an Entra app registration, an Azure Key Vault (RBAC mode),
> and per-user GitHub PATs stored as Key Vault secrets named `github-pat-<user-oid>`. See
> [`azure-setup.md`](azure-setup.md) for the full walkthrough and the values to record. The
> `az` CLI examples below assume that's done.

---

## Step 1 — Build and push the sidecar + GitHub server images

Build for **`linux/amd64`** (OpenShift/ARO nodes are amd64). If you build on Apple Silicon,
build multi-arch or the pull fails on the cluster with
`no image found in image index for architecture "amd64"`:

```bash
export REGISTRY=<your-registry>   # e.g. an external registry your cluster can pull from

docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REGISTRY/mcp-entra-sidecar:latest --push sidecar/

# GitHub MCP server (builds from upstream github/github-mcp-server — takes a few minutes)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REGISTRY/mcp-github:latest --push servers/github/
```

(DuckDuckGo was already built and deployed in Milestone 1 — nothing to do for it here.)

## Step 2 — Store the Azure service-principal credentials

The sidecar uses these to read PAT secrets from Key Vault. **Do not commit this secret.**

```bash
oc create secret generic azure-sp-credentials \
  --from-literal=AZURE_TENANT_ID=<tenant-id> \
  --from-literal=AZURE_CLIENT_ID=<client-id> \
  --from-literal=AZURE_CLIENT_SECRET=<client-secret> \
  -n mcp-gateway
```

## Step 3 — Grant the `anyuid` SCC for the GitHub server

The upstream `github-mcp-server` binary runs as **root**, which OpenShift's default
`restricted-v2` SCC rejects. Grant `anyuid` to the `default` ServiceAccount (the SA the
operator-created server pods run under). This is *additive* to the `nonroot-v2` grant from
Milestone 1's Step 3:

```bash
oc adm policy add-scc-to-user anyuid -z default -n mcp-gateway
```

> The subcommand is `add-scc-to-user`; it handles service accounts via `-z`. (There is no
> `add-scc-to-serviceaccount` subcommand.) The Entra sidecar itself runs non-root (uid 10001)
> and is already covered by the `nonroot-v2` grant from Milestone 1 — no extra grant needed for it.
> If you rebuild the GitHub image to run non-root, you can skip this `anyuid` grant.

## Step 4 — Edit the sidecar manifest and deploy it

In `manifests/sidecar-deployment.yaml`, replace every `PLACEHOLDER_*` value (image ref, Entra
tenant/client IDs, resource URI, gateway URL, Key Vault URL — see the table in
[`azure-setup.md`](azure-setup.md)). Then deploy the sidecar (a standalone Deployment + Service):

```bash
oc apply -f manifests/sidecar-deployment.yaml
oc rollout status deploy/mcp-entra-sidecar -n mcp-gateway
```

Confirm it started and serves its health + discovery endpoints:

```bash
oc logs deploy/mcp-entra-sidecar -n mcp-gateway | grep -i "starting entra-sidecar"
oc exec deploy/mcp-entra-sidecar -n mcp-gateway -- \
  wget -qO- http://localhost:8080/healthz            # -> ok
```

The sidecar is reached in-cluster at `http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp`.

## Step 5 — Deploy the GitHub MCP server

Set your registry in `mcpserver-github.yaml` (replace `REPLACE_WITH_YOUR_REGISTRY`) and apply.
The operator reconciles it into a Deployment + Service named `mcp-github` (same pattern as the
DuckDuckGo server from Milestone 1):

```bash
oc apply -f mcpserver-github.yaml
oc get mcpserver github -n mcp-gateway         # PHASE -> Running
oc rollout status deploy/mcp-github -n mcp-gateway
```

## Step 6 — Add GitHub to the catalog and gateway

Edit the Milestone 1 catalog (`catalog-and-gateway.yaml`) to add a `github` registry entry
**with `auth_delegation: gateway`** (this is what makes the gateway call the sidecar's
`get_connection_headers` to inject the caller's PAT), and add `github` to the `MCPGateway`
`serverNames`. DuckDuckGo stays as-is (no delegation — it's public):

```yaml
    # ... under registry: (alongside the existing duckduckgo entry)
      github:
        name: github
        title: GitHub
        description: GitHub repository access — issues, PRs, code search
        type: remote
        auth_delegation: gateway          # gateway injects the per-user PAT from the sidecar
        remote:
          url: http://mcp-github.mcp-gateway.svc.cluster.local:8080/mcp
          transport_type: streamable-http
        allowHosts:
          - api.github.com:443
```
```yaml
# ... in the MCPGateway spec:
  serverNames:
    - duckduckgo
    - github
```

Re-apply:

```bash
oc apply -f catalog-and-gateway.yaml
oc get mcpgw pov-gateway -n mcp-gateway -w      # wait for Active
```

## Step 7 — Re-wire the gateway from bearer auth to the Entra sidecar

This is the one change to the gateway itself. In Milestone 1 the data plane validated a static
bearer token with the built-in in-memory plugin. Point it at the sidecar instead so it validates
**Entra JWTs** and delegates **per-user credentials**.

Edit the `dataPlane` block of the `GatewayServiceConfig` template (`gatewayserviceconfig.yaml`),
replacing the Milestone 1 `pluginConfig` (and the now-unused `MCP_GATEWAY_AUTH_TOKEN` `extraEnv`)
with the sidecar wiring. Keep `sidecar.enabled: false` — the sidecar runs as its own Deployment
(Step 4), so the operator must **not** inject an in-pod sidecar:

```yaml
      sidecar:
        enabled: false
      dataPlane:
        replicas: 1
        # Validate Entra JWTs and delegate per-user creds via the standalone Entra sidecar.
        pluginConfig: |
          plugins:
            gateway_auth:
              provider: mcp
              server: "http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp"
          auth_delegators:
            - name: entra-sidecar
              strategy: remote
              provider: mcp
              server: "http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp"
          auth_delegation:
            defaults:
              remote: entra-sidecar
        # (Remove the MCP_GATEWAY_AUTH_TOKEN extraEnv from Milestone 1 — no longer used.)
        resources:
          requests: { cpu: 200m, memory: 512Mi }
          limits:   { cpu: 4000m, memory: 2Gi }
```

Re-apply the CR (same `oc process` command as Milestone 1 Step 8) and wait for the DP to roll:

```bash
oc process -f gatewayserviceconfig.yaml \
  -p VERSION="$VERSION" \
  -p CLUSTER_DOMAIN="$CLUSTER_DOMAIN" \
  | oc apply -n mcp-gateway -f -

oc rollout status deploy/mcp-gw-dp -n mcp-gateway
```

> After this, the static bearer token from Milestone 1 **no longer works** — every request
> (including to DuckDuckGo) now requires a valid Entra JWT. That's expected.

## Step 8 — Verify end-to-end

Get an Entra JWT for a user who has the gateway app role assigned (and, for the GitHub test, a
`github-pat-<their-oid>` secret in Key Vault):

```bash
TOKEN=$(az account get-access-token --resource api://mcp-gateway --query accessToken -o tsv)
GATEWAY_URL=$(oc get mcpgw pov-gateway -n mcp-gateway -o jsonpath='{.status.endpoints.sk}')
```

The gateway speaks MCP streamable-http, so do the handshake (`initialize` → keep the
`Mcp-Session-Id` → `notifications/initialized` → call tools), send the SSE `Accept` header, and
pull the JSON out of the `data:` frame. **Tool names are server-prefixed** (`duckduckgo__search`,
`github__*`):

```bash
# 1. initialize — capture the session id
SID=$(curl -sS -k -D - -o /dev/null -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')

# 2. complete the handshake
curl -sS -k -o /dev/null -X POST "$GATEWAY_URL" -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3. list tools — expect duckduckgo__search and github__* tools
curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq '.result.tools[].name'

# 4. call a GitHub tool — the gateway injects THIS user's PAT (from Key Vault) automatically
curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"github__search_repositories","arguments":{"query":"user:@me"}}}' \
  | sed -n 's/^data: //p' | jq .
```

GitHub results scoped to *that* user — with no token in the request beyond their Entra JWT —
means the full chain works: Entra auth → gateway → sidecar credential delegation → Key Vault →
GitHub. Confirm the sidecar did the work:

```bash
oc logs deploy/mcp-entra-sidecar -n mcp-gateway --tail=30 | grep -E 'authenticate|delegated'
# Expect: delegated PAT credential: server=github principal=<oid>
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Sidecar `CrashLoopBackOff` | Missing required env (`ENTRA_TENANT_ID`, `ENTRA_AUDIENCE`, `ENTRA_CLIENT_ID`, `GATEWAY_RESOURCE`, `AZURE_KEYVAULT_URL`) or wrong `azure-sp-credentials` key | `oc logs deploy/mcp-entra-sidecar -n mcp-gateway --previous`; verify env + secret keys |
| `mcp-github` pod `unable to validate against any security context constraint … runAsUser` | Root server without the SCC grant | Apply Step 3 (`add-scc-to-user anyuid -z default`) |
| `mcp-github` `ImagePullBackOff … no image found … architecture "amd64"` | Single-arch (arm64-only) image built on Apple Silicon | Rebuild `--platform linux/amd64,linux/arm64 --push` (Step 1) |
| All requests now 401 after Step 7 | Expected — bearer auth is gone; present an Entra JWT | Use `az account get-access-token …` |
| JWT rejected `wrong audience` | `ENTRA_AUDIENCE` must be the app **client ID** (GUID), not the `api://` URI (v2.0 tokens put client_id in `aud`) | Fix the env var, roll the sidecar |
| GitHub call 401 / `get_connection_headers` empty | No `github-pat-<oid>` secret in Key Vault for that user | `az keyvault secret set --vault-name <kv> --name github-pat-<oid> --value <pat>` |
| Key Vault access denied | SP lacks `Key Vault Secrets User`, or KV is in access-policy (not RBAC) mode | Grant the role on the vault; switch KV to RBAC |
