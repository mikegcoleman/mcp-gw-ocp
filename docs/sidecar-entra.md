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

Two build options. Use **1A** if you have a local Docker with buildx; use **1B** to build on the
cluster (no local Docker needed — recommended, and it guarantees the correct node architecture).

Either way the images must be **`linux/amd64`** (OpenShift/ARO nodes are amd64). A single-arch
arm64 image (e.g. built on Apple Silicon without `--platform`) fails the pull on the cluster with
`no image found in image index for architecture "amd64"`.

### Option 1A — local Docker (multi-arch)

```bash
export REGISTRY=<your-registry>   # a registry your cluster can pull from

docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REGISTRY/mcp-entra-sidecar:latest --push sidecar/

# GitHub MCP server (builds from upstream github/github-mcp-server — takes a few minutes)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REGISTRY/mcp-github:latest --push servers/github/
```

### Option 1B — build on the cluster with OpenShift BuildConfig (no local Docker)

Builds run on amd64 nodes and push to the cluster's internal registry. The deployment manifests
then reference `image-registry.openshift-image-registry.svc:5000/mcp-gateway/<name>:latest`.

```bash
# Sidecar
oc new-build --binary --strategy=docker --name=mcp-entra-sidecar -n mcp-gateway
oc start-build mcp-entra-sidecar --from-dir=sidecar -n mcp-gateway --follow

# GitHub MCP server (clones upstream at build time)
oc new-build --binary --strategy=docker --name=mcp-github -n mcp-gateway
oc start-build mcp-github --from-dir=servers/github -n mcp-gateway --follow
```

With Option 1B, set the image fields in later steps to
`image-registry.openshift-image-registry.svc:5000/mcp-gateway/mcp-entra-sidecar:latest` and
`.../mcp-github:latest`.

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

## Step 4 — Deploy the sidecar

### Step 4a — Create the sidecar-config Secret

All deployment-specific values (Entra IDs, gateway URL, Key Vault URL, OAuth callback URLs) live
in a `sidecar-config` Secret. The manifest never contains these values, so re-applying it can
never accidentally overwrite them.

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')

oc create secret generic sidecar-config \
  --from-literal=ENTRA_TENANT_ID=<TENANT_ID> \
  --from-literal=ENTRA_AUDIENCE=<CLIENT_ID> \
  --from-literal=ENTRA_CLIENT_ID=<CLIENT_ID> \
  --from-literal=ENTRA_RESOURCE_URI=api://<CLIENT_ID> \
  --from-literal=GATEWAY_RESOURCE=https://mcp-gw-dp.$CLUSTER_DOMAIN \
  --from-literal=AZURE_KEYVAULT_URL=https://<KV_NAME>.vault.azure.net/ \
  --from-literal=DCR_PROXY_URL=https://mcp-gw-dp.$CLUSTER_DOMAIN/dcr \
  --from-literal=MCP_GATEWAY_OAUTH_CALLBACK_BASE_URL=https://mcp-sidecar-oauth.$CLUSTER_DOMAIN \
  -n mcp-gateway
```

See [`azure-setup.md`](azure-setup.md) for where each value comes from. Leave `DCR_PROXY_URL`
and `MCP_GATEWAY_OAUTH_CALLBACK_BASE_URL` as empty strings (`--from-literal=DCR_PROXY_URL=`)
for Milestone 2 — they are required for Milestone 3.

### Step 4b — Deploy the sidecar

`manifests/sidecar-deployment.yaml` is an OpenShift Template — it requires an `IMAGE` parameter
(the sidecar image you built in Step 1B) and reads all config from the Secrets above.

```bash
IMAGE=$(oc get istag mcp-entra-sidecar:latest -n mcp-gateway \
  -o jsonpath='{.image.dockerImageReference}')

oc process -f manifests/sidecar-deployment.yaml -p IMAGE="$IMAGE" \
  | oc apply -n mcp-gateway -f -

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

Re-apply, then **restart the control plane** — it reads the catalog file at startup, so a changed
catalog ConfigMap is not picked up until the CP rolls:

```bash
oc apply -f catalog-and-gateway.yaml
oc rollout restart deploy/mcp-gw-cp -n mcp-gateway    # required: CP re-reads the catalog
oc rollout status deploy/mcp-gw-cp -n mcp-gateway
oc get mcpgw pov-gateway -n mcp-gateway -w            # wait for Active
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
`github-pat-<their-oid>` secret in Key Vault). Request the token against the **delegated scope**
(`access`), using the app's client-id URI:

```bash
APPID=<your-app-client-id>   # the Application (client) ID
TOKEN=$(az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv)
GATEWAY_URL=$(oc get mcpgw pov-gateway -n mcp-gateway -o jsonpath='{.status.endpoints.sk}')
```

> If this returns `AADSTS65001` (consent), the Azure CLI client isn't authorized for your API yet
> — see the "Testing from the CLI" note in [`azure-setup.md`](azure-setup.md) §1e (or just run the
> CLI-alternative appendix there, which grants it). This only affects the CLI test; real MCP
> clients (VS Code Copilot) use their own pre-authorized client.

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

## Step 9 — Connecting MCP clients

The gateway requires a valid **Entra JWT** on every request; how each client obtains one differs.
Use `<GATEWAY_URL>` = the gateway's `status.endpoints.sk` (Step 8) and `<APP_CLIENT_ID>` = the Entra
app's Application (client) ID.

**Pick the path for your client:** if your users are on **Claude Code**, use the token helper in
**9b** — that's the path for this deployment. Section **9a (VS Code)** is included as a *reference*,
not a recommendation: it proves the server-side Entra configuration is correct, because a client
that implements Entra natively signs in seamlessly with zero extra config. That's what isolates the
Claude Code limitation as a client-side gap (see the callout at the end), not a gateway/config problem.

### 9a. VS Code Copilot — reference: proof the Entra config is correct

Not a recommendation (many customers don't use Copilot) — this is the control case. A client with a
native Entra auth provider signs in with **zero app-specific config beyond the URL**, which confirms
the gateway + app registration are set up correctly and leaves any per-client friction (e.g. Claude
Code, 9b) squarely on the client. `.vscode/mcp.json`:
```json
{ "servers": { "pov-gateway": { "type": "http", "url": "<GATEWAY_URL>" } } }
```
Open the workspace → Copilot **Agent mode** → **Sign in with Microsoft** → tools load. Requires
azure-setup §1e (VS Code pre-authorized) **and** §1e-2 (public client + `http://localhost` redirect).
**Verified working** end-to-end (interactive SSO → gateway → per-user credential injection).

### 9b. Claude Code — DCR proxy (full OAuth, no helper script) — Milestone 3

With the Entra DCR proxy deployed (see [group-based-access.md §5](group-based-access.md#5-deploy-the-entra-dcr-proxy-enables-full-oauth-in-mcp-clients)),
Claude Code completes a full browser OAuth flow automatically — no `headersHelper` or `az` session
required:

```bash
claude mcp add --transport http pov-gateway <GATEWAY_URL> --scope user
```

On first connect, Claude Code opens a browser window. The user signs in with their Entra account
and grants consent once. Subsequent connections are silent (token is refreshed automatically).

> If the DCR proxy is not yet deployed, use the token helper fallback in **9b-fallback** below.

### 9b-fallback. Claude Code — Entra token helper (Milestone 2, no DCR proxy)

```bash
cat > ~/entra-mcp-token.sh <<'EOF'
#!/bin/bash
tok=$(az account get-access-token --scope "api://<APP_CLIENT_ID>/access" --query accessToken -o tsv)
printf '{"Authorization":"Bearer %s"}' "$tok"
EOF
chmod +x ~/entra-mcp-token.sh

claude mcp add-json pov-gateway \
  '{"type":"http","url":"<GATEWAY_URL>","headersHelper":"/absolute/path/to/entra-mcp-token.sh"}'
```
`/mcp` connects (no browser). The helper re-runs on reconnect / 401, so the token stays fresh as
long as the user's `az` session is alive (`az login` once per user; the Azure CLI is pre-authorized
in §1e).

### 9c. Any other HTTP MCP client — static bearer fallback

Same pattern: `Authorization: Bearer <entra-jwt>` from `az account get-access-token`, refreshed
hourly (or via an equivalent helper).

### 9d. Fleet distribution

- **VS Code Copilot:** nothing to distribute — the client-id is built in; each user just signs in.
- **Claude Code:** push the config to every machine with **`managed-mcp.json`** via MDM
  (Jamf / Intune / GPO):
  - macOS `/Library/Application Support/ClaudeCode/managed-mcp.json`
  - Linux/WSL `/etc/claude-code/managed-mcp.json`
  - Windows `C:\Program Files\ClaudeCode\managed-mcp.json`
  Same JSON as 9b (`url` + `headersHelper`, and ship the helper script too). It auto-loads — no
  per-user `claude mcp add`, no trust prompt. Optionally restrict to only managed servers with
  `allowedMcpServers` + `allowManagedMcpServersOnly: true` in managed settings.

> **Why Claude Code can't do interactive Entra sign-in — and why it's not a gateway problem.**
> Two upstream incompatibilities between Claude Code's MCP OAuth and Microsoft Entra:
> 1. Claude Code expects the authorization server to support **RFC 7591 Dynamic Client
>    Registration**; Entra does not (its DCR is gated). Symptom: *"does not support dynamic client
>    registration."*
> 2. Even with a pre-registered `--client-id`, Claude Code sends an **RFC 8707 `resource`
>    parameter** — the gateway URL, taken from the gateway's RFC 9728 metadata — which Entra
>    rejects against the app-scoped scope. Symptom: **`AADSTS9010010`**. This is not fixable at the
>    sidecar or gateway (the data plane sets `resource` to the gateway URL per RFC 9728 §3.3) nor in
>    Entra (the gateway URL can't be registered as an app identifier).
>
> VS Code Copilot avoids both by using its **native Microsoft auth provider** instead of the
> generic MCP OAuth flow. Until Anthropic ships an Entra-compatible client, Claude Code uses the
> token helper (9b). This is a client ↔ identity-provider gap, **not** a limitation of the gateway.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Sidecar `CrashLoopBackOff` | Missing required env (`ENTRA_TENANT_ID`, `ENTRA_AUDIENCE`, `ENTRA_CLIENT_ID`, `GATEWAY_RESOURCE`, `AZURE_KEYVAULT_URL`) or wrong `azure-sp-credentials` key | `oc logs deploy/mcp-entra-sidecar -n mcp-gateway --previous`; verify env + secret keys |
| `mcp-github` pod `unable to validate against any security context constraint … runAsUser` | Root server without the SCC grant | Apply Step 3 (`add-scc-to-user anyuid -z default`) |
| `mcp-github` `ImagePullBackOff … no image found … architecture "amd64"` | Single-arch (arm64-only) image built on Apple Silicon | Rebuild `--platform linux/amd64,linux/arm64 --push` (Step 1) |
| All requests now 401 after Step 7 | Expected — bearer auth is gone; present an Entra JWT | Use `az account get-access-token …` |
| JWT rejected `wrong audience` | `ENTRA_AUDIENCE` must be the app **client ID** (GUID), not the `api://` URI (v2.0 tokens put client_id in `aud`) | Fix the env var, roll the sidecar |
| GitHub call 401 / `get_connection_headers` empty | No `github-pat-<oid>` secret in Key Vault for that user | `az keyvault secret set --vault-name <kv> --name github-pat-<oid> --value <pat>` |
| GitHub call `401 Bad credentials` (injection worked, GitHub rejected) | The `github-pat-<oid>` secret is expired/invalid or lacks scope | Store a valid PAT with the needed scopes in Key Vault |
| Client OAuth: `AADSTS65001` (no consent) | The client's app-id isn't pre-authorized for the `access` scope | Pre-authorize the client id (azure-setup §1e); for CLI, the Azure CLI client |
| Client OAuth: *"does not support dynamic client registration"* (Claude Code) | Entra has no DCR; the client tried to self-register | Use the token helper (Step 9b) — or VS Code Copilot for interactive |
| Client OAuth: `AADSTS9010010` (resource ≠ scope) | Claude Code's RFC 8707 `resource` param (gateway URL) can't reconcile with the Entra scope | Not fixable on Entra/gateway — use the token helper (9b) or VS Code (9a) |
| Key Vault access denied | SP lacks `Key Vault Secrets User`, or KV is in access-policy (not RBAC) mode | Grant the role on the vault; switch KV to RBAC |
