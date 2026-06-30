# OpenShift Deployment Guide: MCP Gateway Enterprise (Appliance Helm Chart)

Proof-of-Value deployment guide for the MCP Gateway on OpenShift, using the published
`mcp-gateway-appliance` umbrella chart (with bundled PostgreSQL and Redis).

> This guide deploys entirely from the **published release artifacts** in
> `github.com/docker/mcp-gateway-enterprise-releases`: **OCI Helm charts** and `-releases`-pathed
> container images. It assumes **no access to source** — there is no `deploy/` directory and no
> chart to build locally; everything is pulled from the OCI registry.
>
> Two steps are flagged **TEMPORARY** (Step 3 SCC grants and Step 5b ClusterRole finalizers) —
> they work around operator/chart gaps not yet in a published release. Remove them once a release
> ships the fixes; see **Pending upstream fixes** at the end.

---

## Architecture Overview

Deployment is two-phased. Phase 1 installs the operator, PostgreSQL, and Redis via Helm
(from the OCI registry). Phase 2 applies a `GatewayServiceConfig` CR that the operator
reconciles into the actual CP/DP Deployments and Services. You then expose those Services via
OpenShift **Routes** — the chart does not create Routes or Ingress.

```
helm install (OCI)  →  mcp-operator pod + postgres StatefulSet + redis Deployment
        ↓ (watches for)
GatewayServiceConfig CR  →  operator creates:
        <cr-name>-cp Service (ClusterIP, port 8080)
        <cr-name>-dp Service (ClusterIP, port 8081)
        <cr-name>-cp Deployment
        <cr-name>-dp Deployment

You create:
        OpenShift Route → <cr-name>-cp   (for console/API)
        OpenShift Route → <cr-name>-dp   (for MCP client traffic)
```

## Prerequisites

- OpenShift 4.x cluster with **cluster-admin** access (required for SCC grants and the
  operator's ClusterRole/ClusterRoleBinding).
- `oc` CLI and **Helm 3.7+** installed locally (3.7+ is required for OCI registry support).
  Variable substitution into the CR (Step 8) uses `oc process`, which is built into `oc` — no
  extra tooling required on any platform.
- A GitHub account that has been **granted access to the Docker MCP Gateway Enterprise
  releases** under your Subscription Service Agreement, with a token carrying the
  `read:packages` scope. This is **not** Docker org membership — it is the access your
  Design-Partner agreement provides to `ghcr.io/docker/mcp-gateway-enterprise-releases`.
- Pull access to `docker.io` for `postgres:17-alpine` and `redis:8-alpine` (public, but
  rate-limited — see the Appendix for the CRC mirroring approach).

> **Pick a release version.** Charts and images are published at the same version tag (e.g.
> `0.0.59`). Find the latest on the
> [Releases page](https://github.com/docker/mcp-gateway-enterprise-releases/releases) and use
> it consistently for `--version`, the CR `spec.version`, and any mirrored image tags. This
> guide uses `0.0.59` as the example — the latest at the time of writing — but releases ship
> frequently, so **always check the Releases page and substitute the current version**
> throughout (tip: `export VERSION=<latest>` and use `--version "$VERSION"`).

## Naming: namespace vs. CR name

Two independent names run through this guide and must not be confused:

- **Namespace** (`mcp-gateway`) — the OpenShift project created in Step 1. All `-n mcp-gateway`
  flags refer to this.
- **CR name** (`mcp-gw` in the examples) — the `metadata.name` of the `GatewayServiceConfig`
  resource applied in Step 8. This determines the Service names the operator creates and must
  match the `cpEndpoint` hostname configured in Step 5.

If you name your CR `mcp-gw`:

- CP Service: `mcp-gw-cp` (port 8080)
- DP Service: `mcp-gw-dp` (port 8081)
- CP endpoint (in-cluster): `http://mcp-gw-cp.mcp-gateway.svc.cluster.local:8080`

You can use any value for either name; just keep each consistent within its own role.

---

## Step 1 — Create the OpenShift project

```bash
oc new-project mcp-gateway
```

## Step 2 — Authenticate to the releases registry and create a pull secret

The operator and gateway-service images live in the **private** releases registry, so the
cluster needs a pull secret. Authenticate Helm and Docker to GHCR using your GitHub token:

```bash
# Add read:packages if your token lacks it:
gh auth refresh -h github.com -s read:packages

# Log in for both helm (charts) and docker (images)
gh auth token | helm registry login ghcr.io -u "$(gh api /user --jq .login)" --password-stdin
gh auth token | docker login ghcr.io -u "$(gh api /user --jq .login)" --password-stdin
```

Create the pull secret with **explicit credentials** (do not build it from
`~/.docker/config.json` — on macOS/Windows, Docker Desktop stores the real token in the OS
keychain via `credsStore`, leaving `config.json` with empty `auths`, which produces a pull
secret that fails with `unauthorized`). Sourcing the values from `gh` avoids pasting the token:

```bash
oc create secret docker-registry ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username="$(gh api user --jq .login)" \
  --docker-password="$(gh auth token)" \
  -n mcp-gateway

oc secrets link default ghcr-pull-secret --for=pull -n mcp-gateway
```

> `gh auth token` must carry `read:packages` (Step's first command ensures it). To use a classic
> PAT instead, pass `--docker-password='ghp_xxxxx'`. If you ever recreate this secret after the
> operator is already running, force a re-pull with
> `oc rollout restart deploy/mcp-gateway-mcp-operator -n mcp-gateway`.

The operator and CP/DP pods reference this secret explicitly (Step 5 `--set` flag and the CR
`imagePullSecrets`), so they do not rely on the `default` SA link.

## Step 3 — Grant OpenShift SCC permissions

The chart's containers run as fixed non-root UIDs (operator `65532`, Postgres `70`, Redis
`999`, gateway-service CP/DP non-root) that fall **outside** the UID range OpenShift's default
`restricted-v2` SCC assigns per-namespace. Without a grant, pods fail to schedule with:

```
unable to validate against any security context constraint: [provider restricted-v2:
.containers[0].runAsUser: Invalid value: 65532: must be in the ranges: [1000670000, 1000679999]]
```

Grant the `nonroot-v2` SCC to the three ServiceAccounts the workload uses. SCC bindings may be
created before the ServiceAccounts exist, so do this now:

```bash
# Operator SA (created by Helm in Step 5)
oc adm policy add-scc-to-user nonroot-v2 \
  -z mcp-gateway-mcp-operator -n mcp-gateway

# default SA — used by bundled Postgres and Redis
oc adm policy add-scc-to-user nonroot-v2 \
  -z default -n mcp-gateway

# gateway-service SA — created in Step 7, referenced by the CR
oc adm policy add-scc-to-user nonroot-v2 \
  -z mcp-gw -n mcp-gateway
```

> `nonroot-v2` is preferred over `anyuid`: the containers already run as non-root, so they only
> need to be allowed to keep their fixed UID, not to run as root.

## Step 4 — Set variables and generate secrets

Export these once in the shell you run the rest of the guide from. `CLUSTER_DOMAIN` is
**auto-derived from the cluster**, so you never hand-type the (long) ingress hostname:

```bash
export VERSION=0.0.59                                                     
export CLUSTER_DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')
export CP_TOKEN=$(openssl rand -hex 32)
export DP_AUTH_TOKEN=$(openssl rand -hex 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
export AUTH_TOKEN=$(openssl rand -hex 32)        

echo "VERSION=$VERSION"
echo "CLUSTER_DOMAIN=$CLUSTER_DOMAIN"         
echo "CP_TOKEN=$CP_TOKEN"
echo "DP_AUTH_TOKEN=$DP_AUTH_TOKEN"
echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
echo "AUTH_TOKEN=$AUTH_TOKEN"
```

Create the K8s Secret that holds the client-facing bearer token (the CR's data plane reads it
via `MCP_GATEWAY_AUTH_TOKEN` — see Step 8). The `mcp-gateway` project already exists from Step 1:

```bash
oc create secret generic mcp-gateway-auth \
  --from-literal=auth-token="$AUTH_TOKEN" \
  -n mcp-gateway
```

These variables are interpolated into the install command (Step 5), the CR (Step 8, via
`oc process`), and the Route hostnames (Step 9). Save the three secrets — the CP token
authenticates operator→CP API calls, the DP auth token secures the CP→DP channel, and the
Postgres password must be re-supplied on every `helm upgrade` (see Upgrading) to stay stable.

> If you open a new terminal later, re-run these `export`s (at minimum `VERSION` and
> `CLUSTER_DOMAIN`) before any step that references them.

## Step 5 — Install the operator and bundled databases (appliance chart)

This installs the **`mcp-gateway-appliance` umbrella chart**, which deploys the **`mcp-operator`**
plus bundled **PostgreSQL** and **Redis** and the umbrella **Secret** — it does **not** deploy the
gateway itself. The running gateway (control plane + data plane) is created later by the operator
when you apply the `GatewayServiceConfig` CR in Step 8.

```bash
helm install mcp-gateway \
  oci://ghcr.io/docker/mcp-gateway-enterprise-releases/charts/mcp-gateway-appliance \
  --version "$VERSION" \
  --namespace mcp-gateway \
  --set mcp-operator.cpEndpoint="http://mcp-gw-cp.mcp-gateway.svc.cluster.local:8080" \
  --set mcp-operator.insecure=true \
  --set 'mcp-operator.imagePullSecrets[0].name=ghcr-pull-secret' \
  --set postgres.enabled=true \
  --set postgres.auth.password="$POSTGRES_PASSWORD" \
  --set redis.enabled=true \
  --set secrets.create=true \
  --set secrets.cpToken="$CP_TOKEN" \
  --set secrets.dpAuthToken="$DP_AUTH_TOKEN"
```

Notes:

- **`mcp-operator.cpEndpoint`** must match the CP Service the operator will create for your CR
  (derived in "Naming" above). **`insecure=true`** lets the operator reach the CP over plain
  HTTP in-cluster; TLS is terminated at the Route layer (Step 9).
- **The operator image needs no override** — the published chart already defaults it to
  `ghcr.io/docker/mcp-gateway-enterprise-releases/mcp-operator`.
- **`redis.enabled=true`** is required (the chart default is `false`). Redis provides shared
  rate limiting and cross-pod OAuth token-refresh broadcasting; it is required when gateways
  have rate limits configured or when running multiple DP replicas.
- With `postgres.enabled=true` and `secrets.create=true`, the chart auto-derives
  `cp-postgres-dsn`, `dp-postgres-dsn`, and `dp-redis-url` into the umbrella Secret
  (`mcp-gateway-gateway-service`, i.e. `<release>-gateway-service`) — you don't supply those
  manually.
- Set **`postgres.auth.password` explicitly**. The chart auto-generates and stabilizes a
  password across upgrades, but if the release is ever deleted and reinstalled a new password
  is generated and the existing PVC data becomes inaccessible. An explicit password avoids that.

### Step 5b — Apply the operator ClusterRole finalizers workaround  ⚠️ TEMPORARY

> Remove this step once you are on a release whose `mcp-operator` ClusterRole includes
> `gatewayserviceconfigs/finalizers` **and** `mcpservers/finalizers` (see "Pending upstream fixes").

The operator's ClusterRole is missing the `finalizers` sub-resource for two of its CRDs:
`gatewayserviceconfigs` and `mcpservers`. Without them, the operator can't set
`blockOwnerDeletion` on resources it owns, so reconciles fail with
`... is forbidden: cannot set blockOwnerDeletion if an ownerReference refers to a resource you
can't set finalizers on`. The result: the `GatewayServiceConfig` never leaves `Provisioning`
(missing `gatewayserviceconfigs/finalizers`), **and** any `MCPServer` you deploy in Step 11 stays
`Failed` with no pod (missing `mcpservers/finalizers`). Grant both now.

Open the ClusterRole in an editor:

```bash
oc edit clusterrole mcp-gateway-mcp-operator
```

Find the `rules:` section and add the block below as new list entries (match the indentation of
the other `- apiGroups:` entries). It uses **no quotes**, so copy/paste from a PDF can't corrupt
it the way a JSON one-liner can:

```yaml
- apiGroups:
  - mcp.docker.com
  resources:
  - gatewayserviceconfigs/finalizers
  verbs:
  - update
  - patch
- apiGroups:
  - mcp.docker.com
  resources:
  - mcpservers/finalizers
  verbs:
  - update
  - patch
```

Save and quit (`:wq` in the default `vi` editor) — `oc` applies the change on save. To use a
different editor, set it first, e.g. `export KUBE_EDITOR="nano"`.

Verify the grant landed:

```bash
oc get clusterrole mcp-gateway-mcp-operator -o yaml | grep finalizers
```

## Step 6 — Verify the operator, Postgres, and Redis are running

```bash
oc get pods -n mcp-gateway
```

The operator pod starts but will show `0/1` under `READY` until a `GatewayServiceConfig` CR is applied. Postgres and
Redis should also reach Running/Ready.

## Step 7 — Create the gateway-service ServiceAccount

The operator does **not** create the ServiceAccount for the CP/DP pods — create it with the
name you will reference in the CR. Create it **before** applying the CR (Step 8) so the pods
schedule on the first try:

```bash
oc create serviceaccount mcp-gw -n mcp-gateway
```

> **Note:** If you applied the CR before the SA existed, the CP/DP ReplicaSets enter
> `FailedCreate` and do **not** retry automatically. Recover with:
> ```bash
> oc create serviceaccount mcp-gw -n mcp-gateway
> oc delete rs -n mcp-gateway -l app.kubernetes.io/component=control-plane
> oc delete rs -n mcp-gateway -l app.kubernetes.io/component=data-plane
> ```

## Step 8 — Apply the GatewayServiceConfig CR

The CR ships as an **OpenShift Template** (`gatewayserviceconfig.yaml`) with `VERSION` and
`CLUSTER_DOMAIN` parameters, so nothing cluster-specific is hard-coded. `dpExternalBasePath` is
derived as `https://mcp-gw-dp.${CLUSTER_DOMAIN}`, which exactly matches the DP Route created in
Step 9 (no patch-after-the-fact needed).

```yaml
# gatewayserviceconfig.yaml
apiVersion: template.openshift.io/v1
kind: Template
metadata:
  name: mcp-gateway-gwsvc
parameters:
  - name: VERSION
    description: gateway-service image tag, matching your chart release (e.g. 0.0.59)
    required: true
  - name: CLUSTER_DOMAIN
    description: "oc get ingresses.config cluster -o jsonpath='{.spec.domain}'"
    required: true
objects:
  - apiVersion: mcp.docker.com/v1alpha1
    kind: GatewayServiceConfig
    metadata:
      name: mcp-gw                 # must match the CR name from "Naming" above
      namespace: mcp-gateway
    spec:
      version: "${VERSION}"
      deploymentMode: customer-cloud-k8s
      image:
        repository: ghcr.io/docker/mcp-gateway-enterprise-releases/gateway-service
        pullPolicy: IfNotPresent
      imagePullSecrets:
        - name: ghcr-pull-secret
      serviceAccountName: mcp-gw
      secretsRef:
        name: mcp-gateway-gateway-service    # <release>-gateway-service (from Step 5)
      # No plugin sidecar — client auth is the gateway's built-in static bearer token.
      sidecar:
        enabled: false
      controlPlane:
        replicas: 1
        bootstrapUser: admin
        # REQUIRED even at replicas:1: the CP provisioner acquires a leader-election lease,
        # and the operator only creates the lease RBAC for the gateway SA when this is set.
        # Without it, every MCPGateway hangs in `Creating` (leases ... forbidden).
        leaderElection:
          enabled: true
          backend: k8s-lease
        dpExternalBasePath: "https://mcp-gw-dp.${CLUSTER_DOMAIN}"
        resources:
          requests: { cpu: 100m, memory: 512Mi }
          limits:   { cpu: 2000m, memory: 2Gi }
      dataPlane:
        replicas: 1
        # Client→gateway auth: built-in static bearer (validates Authorization: Bearer
        # against MCP_GATEWAY_AUTH_TOKEN). No IdP, no sidecar.
        pluginConfig: |
          plugins:
            gateway_auth:
              provider: in-memory
              implementation: anonymous-desktop-bearer-token
              config:
                tenant_id: default
        extraEnv:
          - name: MCP_GATEWAY_AUTH_TOKEN
            valueFrom:
              secretKeyRef:
                name: mcp-gateway-auth      # created in Step 4
                key: auth-token
        resources:
          requests: { cpu: 200m, memory: 512Mi }
          limits:   { cpu: 4000m, memory: 2Gi }
```

Render it with `oc process` (built into `oc` — works identically on macOS, Linux, and Windows,
no extra tooling) and pipe to `oc apply`:

```bash
oc process -f gatewayserviceconfig.yaml \
  -p VERSION="$VERSION" \
  -p CLUSTER_DOMAIN="$CLUSTER_DOMAIN" \
  | oc apply -n mcp-gateway -f -
```

> The `-n mcp-gateway` on `oc apply` is intentional: `oc process` drops the `namespace` field
> from rendered objects, so set it explicitly to be sure the CR lands in the right project.

> `oc process` substitutes the `${VERSION}` / `${CLUSTER_DOMAIN}` parameters and emits the final
> CR on stdout; `oc apply -f -` reads it from there. Values pass as `-p KEY=VALUE`, so there's
> nothing to quote-escape or smart-quote.

Watch the operator provision CP and DP move from `Provisioning` to `Healthy` state:

```bash
oc get gwsvc mcp-gw -n mcp-gateway -w     
```

If it stays in `Provisioning`, check the operator logs:

```bash
oc logs -l app.kubernetes.io/name=mcp-operator -n mcp-gateway -f
```

Verify the CP and DP pods started:

```bash
oc get pods -l app.kubernetes.io/component=control-plane -n mcp-gateway
oc get pods -l app.kubernetes.io/component=data-plane -n mcp-gateway
```

## Step 9 — Create OpenShift Routes

The operator creates ClusterIP Services only. Expose them via TLS edge-terminated Routes with
explicit, clean hostnames under the cluster's `*.${CLUSTER_DOMAIN}` wildcard cert. The DP host
matches `dpExternalBasePath` from Step 8, so **no patch is needed**:

```bash
oc create route edge mcp-gw-cp --service=mcp-gw-cp --port=8080 \
  --hostname="mcp-gw-cp.$CLUSTER_DOMAIN" -n mcp-gateway
oc create route edge mcp-gw-dp --service=mcp-gw-dp --port=8081 \
  --hostname="mcp-gw-dp.$CLUSTER_DOMAIN" -n mcp-gateway

oc get routes -n mcp-gateway
```

> Without `--hostname`, OpenShift generates a long default host
> (`mcp-gw-dp-<namespace>.${CLUSTER_DOMAIN}`) that won't match `dpExternalBasePath`. Setting it
> explicitly keeps the URL clean and the CR consistent.

## Step 10 — Verify end-to-end health

```bash
CP_HOST="mcp-gw-cp.$CLUSTER_DOMAIN"   

curl -k https://$CP_HOST/health
# Expected: ok

curl -k -I https://$CP_HOST/api/v1/gateways
# Expected: 401 Unauthorized (CP API is protected by cpToken)
```

## Step 11 — Deploy and test your first MCP server (DuckDuckGo)

With the gateway up and **bearer auth on** (Step 8), validate the full path with a **no-auth**
MCP server. DuckDuckGo needs no upstream credentials, so this isolates the one thing we're
proving here: a client authenticates to the gateway with a bearer token and reaches a tool.

This is **Milestone 1** — basic, no IdP, single shared bearer token. (Per-user credentials and
IdP-backed auth are Milestone 2; see the note at the end. Nothing here is wasted — the servers,
catalog, gateway, and tests all carry forward.)

### Step 11a — Deploy the DuckDuckGo MCP server

`mcpserver-duckduckgo.yaml` deploys the public DuckDuckGo MCP server (image
`docker.io/mikegcoleman/demo-mcp-duckduckgo`). The operator turns the `MCPServer` CR into an
in-cluster Deployment + Service named `mcp-duckduckgo`:

```bash
oc apply -f mcpserver-duckduckgo.yaml

# The operator reconciles the MCPServer into a Deployment + Service named `mcp-duckduckgo`.
oc get mcpserver duckduckgo -n mcp-gateway      # PHASE should be Running (not Failed)
oc rollout status deploy/mcp-duckduckgo -n mcp-gateway --timeout=120s
```

> If the pod won't schedule with an SCC/`runAsNonRoot` error, the image needs root — set
> `runAsNonRoot: false` in the CR and `oc adm policy add-scc-to-user anyuid -z default -n mcp-gateway`.

### Step 11b — Register it in the catalog and activate it

`catalog-and-gateway.yaml` declares the server in a catalog ConfigMap, binds it via an
`MCPEnvironment`, and activates it on an `MCPGateway`:

```bash
oc apply -f catalog-and-gateway.yaml
oc get mcpgw pov-gateway -n mcp-gateway -w        # wait for status Active
```

Capture the gateway URL — read it from status, don't hand-build it. The MCP endpoint clients use
is `.status.endpoints.sk` (the server-key endpoint; the status also exposes `id` (by gateway UUID)
and `registry`):

```bash
GATEWAY_URL=$(oc get mcpgw pov-gateway -n mcp-gateway -o jsonpath='{.status.endpoints.sk}')
echo "$GATEWAY_URL"
# e.g. https://mcp-gw-dp.apps.<cluster>/gateways/sk/pov-gateway/mcp
```

### ✅ Step 11c — Verification gate (do not proceed if either fails)

**Gate 1 — bearer auth is enforced:**

```bash
# No token → rejected
curl -sS -o /dev/null -w "no-token  -> HTTP %{http_code}\n" -k -X POST "$GATEWAY_URL" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
# Expect: 401

# With the bearer token from Step 4 → accepted
curl -sS -o /dev/null -w "with-token -> HTTP %{http_code}\n" -k -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
# Expect: 200
```

**Gate 2 — the tool actually works through the gateway.**

> Two things to know about talking to the gateway over raw HTTP:
> - It speaks **MCP streamable-http**: a real MCP client must do the `initialize` →
>   `notifications/initialized` → `tools/*` handshake, carrying the `Mcp-Session-Id` returned by
>   `initialize`. A single-shot `tools/list` is rejected (`invalid during session initialization`).
> - Responses come back as a **Server-Sent-Events** stream (`event:` / `data:` lines), so send
>   `Accept: application/json, text/event-stream` and extract the JSON with `sed -n 's/^data: //p'`.
> - Tool names are **server-prefixed**: DuckDuckGo's `search` is exposed as `duckduckgo__search`.

The realistic test is Step 12 — point an MCP client at the gateway and let it drive the handshake.
For a pure-CLI gate, this script does the handshake by hand:

```bash
# 1. initialize — capture the session id from the response headers
SID=$(curl -sS -k -D - -o /dev/null -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $AUTH_TOKEN" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
echo "session=$SID"

# 2. complete the handshake
curl -sS -k -o /dev/null -X POST "$GATEWAY_URL" -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3. list tools (expect duckduckgo__search among them)
curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq '.result.tools[].name'

# 4. call it and get real results back
curl -sS -k -X POST "$GATEWAY_URL" -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Mcp-Session-Id: $SID" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"duckduckgo__search","arguments":{"query":"python"}}}' \
  | sed -n 's/^data: //p' | jq -r '.result.content[].text'
```

Tool results coming back means **every layer works** — TLS/Route, bearer auth, gateway routing,
the MCP handshake, and MCP server invocation. Milestone 1 is done.

## Step 12 — Connect an MCP client

Any HTTP-capable MCP client works with a static bearer header. For Claude Code `.mcp.json` (or
Cursor MCP settings), use the `GATEWAY_URL` from Step 11b and the `AUTH_TOKEN` from Step 4:

```json
{
  "mcpServers": {
    "pov-gateway": {
      "type": "http",
      "url": "<GATEWAY_URL>",
      "headers": { "Authorization": "Bearer <AUTH_TOKEN>" }
    }
  }
}
```

> The bearer token is shared (single identity) — fine for a basic PoV. Treat it as a secret; it's
> full access to the gateway. Rotate by updating the `mcp-gateway-auth` Secret and re-applying
> the CR (the operator rolls the DP to pick up the new value).

## What's next — Milestone 2 (per-user credentials + IdP)

Milestone 1 gives you one shared identity. The richer demo — multiple users sharing one gateway
URL, each getting **their own** upstream credentials (e.g. per-user GitHub PATs), with **no token
in the client config** — is **Milestone 2**. It uses the **preset plugin sidecar** (the supported
plugin architecture, not a custom one):

- `gateway_auth` lane backed by an **IdP** (the preset supports Okta, Auth0, **Entra**, Keycloak)
  — this is what supplies a real per-user `principal_id`. On Azure, Entra is the natural choice.
- `credentials` lane (file backend) mapping `(principal_id, server)` → per-user `Authorization`
  headers, with the secret values mounted from K8s Secrets.

Everything from Milestone 1 (servers, catalog, gateway, Routes, tests) carries over unchanged —
Milestone 2 swaps the `dataPlane.pluginConfig`/`sidecar` block and adds the IdP. Upstream-OAuth
MCP servers (where the *end user* must OAuth to the backend) are a separate, later step — the
preset's OAuth lane isn't implemented yet, so that path needs a custom delegator for now.

---

## Upgrading

### Chart values or operator version

Re-supply `postgres.auth.password` on every upgrade — it is a generated secret, not a static
Helm value, so `--reuse-values` does not persist it:

```bash
helm upgrade mcp-gateway \
  oci://ghcr.io/docker/mcp-gateway-enterprise-releases/charts/mcp-gateway-appliance \
  --version 0.0.60 \
  --namespace mcp-gateway \
  --reuse-values \
  --set postgres.auth.password="$POSTGRES_PASSWORD"
```

### CRDs

Helm never auto-upgrades CRDs on `helm upgrade`. Because you have no local chart, pull the
`mcp-operator` chart from OCI and apply its CRDs whenever the schema changes:

```bash
helm pull oci://ghcr.io/docker/mcp-gateway-enterprise-releases/charts/mcp-operator \
  --version 0.0.60 --untar
oc apply -f mcp-operator/crds/
```

### Gateway-service version

Patch the CR; the operator rolls the Deployment:

```bash
oc patch gwsvc mcp-gw -n mcp-gateway --type=merge \
  -p '{"spec":{"version":"0.0.60"}}'
```

## Uninstalling

```bash
# Remove gateway resources first so the operator can clean up
oc delete mcpgateway --all -n mcp-gateway
oc delete mcpenvironment --all -n mcp-gateway
oc delete gwsvc --all -n mcp-gateway

# Remove the Helm release (operator + databases)
helm uninstall mcp-gateway -n mcp-gateway

# PVCs are intentionally retained on uninstall — delete manually to reclaim storage
oc delete pvc -l app.kubernetes.io/component=postgres -n mcp-gateway
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Operator/Postgres/Redis pod won't schedule (`runAsUser … must be in the ranges …`) | SCC blocking the fixed UID | Grant `nonroot-v2` to the affected SA (Step 3). |
| `gwsvc` stuck in `Provisioning`; operator logs show `cannot set blockOwnerDeletion` on a ConfigMap | ClusterRole missing `gatewayserviceconfigs/finalizers` | Apply Step 5b. |
| `MCPServer` PHASE `Failed` / `Degraded`, no `mcp-<name>` pod; operator logs show `cannot set blockOwnerDeletion` on a Deployment | ClusterRole missing `mcpservers/finalizers` | Apply Step 5b (it grants both finalizers). |
| All `MCPGateway` CRs stuck in `Creating`; CP logs show `leases.coordination.k8s.io "…-provisioner" is forbidden` | `leaderElection` not enabled on the gwsvc, so the operator never created the gateway SA's lease RBAC | Set `spec.controlPlane.leaderElection.enabled: true` + `backend: k8s-lease` (Step 8) and re-apply the CR. |
| `MCPServer` pod `ImagePullBackOff` with `no image found in image index for architecture "amd64"` | The server image is single-arch (e.g. arm64-only, built on Apple Silicon); ARO nodes are amd64 | Rebuild/push the image multi-arch: `docker buildx build --platform linux/amd64,linux/arm64 -t <img> --push .`, then `oc rollout restart deploy/mcp-<name>`. |
| CP/DP pods `ImagePullBackOff` | Pull secret missing/not referenced | Confirm `ghcr-pull-secret` exists and the CR lists it under `imagePullSecrets`; re-auth with `read:packages`. |
| Operator pod `ImagePullBackOff` | Operator SA can't pull from `-releases` | Confirm `--set 'mcp-operator.imagePullSecrets[0].name=ghcr-pull-secret'` was passed in Step 5. |
| `zsh: no matches found: …imagePullSecrets[0]…` | zsh globs the `[0]` in a `--set` flag | Single-quote any `--set` value containing `[ ]`, e.g. `--set 'mcp-operator.imagePullSecrets[0].name=ghcr-pull-secret'`. |
| Postgres pod `Pending` | No default StorageClass | `oc get storageclass`, then reinstall with `--set postgres.storageClass=<name>`. |
| `field not declared in schema` on CR apply | CRD schema outdated | Apply the CRDs from the pulled chart (see Upgrading → CRDs). |
| Operator logs `connection refused` to `cpEndpoint` | CP not up yet or wrong `cpEndpoint` | Wait for the CP pod to be Ready; verify `cpEndpoint` matches `<cr-name>-cp.<namespace>.svc.cluster.local:8080`. |
| OAuth tools missing after authorization on a multi-replica DP | Redis unreachable | `oc logs -l app.kubernetes.io/name=redis -n mcp-gateway`; verify Redis is Running and DP pods reach it on 6379. |

---

## Appendix — Image pull notes

There are two registries with different requirements.

### `ghcr.io/docker/mcp-gateway-enterprise-releases` (private)

Operator and gateway-service images. Requires a GitHub token with `read:packages` from an
account granted releases access under your SSA (Step 2). For production, **mirror** these images
into your own registry so pod restarts/scale-outs don't depend on GHCR:

```bash
crane copy \
  ghcr.io/docker/mcp-gateway-enterprise-releases/gateway-service:0.0.59 \
  your-registry.example.com/mcp-gateway/gateway-service:0.0.59
crane copy \
  ghcr.io/docker/mcp-gateway-enterprise-releases/mcp-operator:0.0.59 \
  your-registry.example.com/mcp-gateway/mcp-operator:0.0.59
```

Then set `spec.image.repository` (CR) and `--set mcp-operator.image.repository=…` (Helm) to your
mirror.

### `docker.io` (`postgres:17-alpine`, `redis:8-alpine`)

Public, but Docker Hub rate-limits unauthenticated pulls (100 / 6h / IP). These subcharts are
**not** rewritten to the releases registry, so they pull from Docker Hub by default.

**Recommended for CRC — mirror into the internal registry:**

```bash
REGISTRY=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')
docker login $REGISTRY -u kubeadmin -p $(oc whoami -t)

docker pull postgres:17-alpine && docker pull redis:8-alpine
docker tag postgres:17-alpine $REGISTRY/mcp-gateway/postgres:17-alpine
docker tag redis:8-alpine     $REGISTRY/mcp-gateway/redis:8-alpine
docker push $REGISTRY/mcp-gateway/postgres:17-alpine
docker push $REGISTRY/mcp-gateway/redis:8-alpine
```

Then add to the `helm install` in Step 5:

```bash
  --set postgres.image=image-registry.openshift-image-registry.svc:5000/mcp-gateway/postgres:17-alpine \
  --set redis.image=image-registry.openshift-image-registry.svc:5000/mcp-gateway/redis:8-alpine
```

**Alternative — authenticate to Docker Hub** (free tier: 200 / 6h): `docker login docker.io`,
create a `dockerhub-pull-secret`, and add
`--set 'postgres.imagePullSecrets[0].name=dockerhub-pull-secret'` (and the same for `redis`).

---

## Pending upstream fixes (remove the workarounds once released)

These two manual steps exist only because the fixes are not yet in a published release. They are
cut from `main`, so a release built from a `main` that contains the fixes will let you drop them:

1. **`gatewayserviceconfigs/finalizers` and `mcpservers/finalizers` ClusterRole grants (Step 5b).**
   The `mcp-operator` ClusterRole ships `finalizers` for `mcpgateways` and `mcpenvironments` but
   not for `gatewayserviceconfigs` or `mcpservers`. Fix: add both rules to the ClusterRole
   template. Once released, delete Step 5b.
2. **OpenShift SCC grants (Step 3).** A chart-side `openshift.scc.enabled` template (granting
   `nonroot-v2` to the operator and `default` SAs) would reduce Step 3 to a single `--set
   openshift.scc.enabled=true` plus one grant for the CR-named gateway SA. Once that template is
   released, simplify Step 3 accordingly.

> The `leases.coordination.k8s.io` RBAC for the gateway-service SA is created by the operator
> (the leader-election Role) **only when `spec.controlPlane.leaderElection.enabled: true` with
> `backend: k8s-lease` is set on the gwsvc** (Step 8 now sets this). It is required even at
> `replicas: 1` because the CP always runs a provisioner that acquires a lease; without it every
> MCPGateway hangs in `Creating`. This is correct configuration, not a workaround — no manual
> lease grant is needed.
