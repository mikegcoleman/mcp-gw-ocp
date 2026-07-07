# CLAUDE.md — MCP Gateway Enterprise on OpenShift (PoV)

Guidance for an agent working in or answering questions about this repository.

## What this repo is

A **standalone, customer-facing install guide** for deploying **Docker MCP Gateway Enterprise**
onto **OpenShift** (validated on Azure Red Hat OpenShift / ARO), using only the **published
release artifacts** — it assumes **no access to source**. `README.md` is the deliverable (it
renders on GitHub); the YAML/Template files are applied as-is.

This is *not* the gateway source. The product lives elsewhere:
- **Gateway source (internal):** `github.com/docker/mcp-gateway-enterprise`
- **Customer release artifacts:** `github.com/docker/mcp-gateway-enterprise-releases` — OCI Helm
  charts + `-releases`-pathed images + the `docs-user-guide`. Access is gated by a Design-Partner
  Subscription Service Agreement (a GitHub token with `read:packages`, **not** Docker org membership).

## Scope: Milestone 1 (what the guide currently delivers)

- Gateway up on OpenShift from OCI artifacts.
- **Client→gateway auth = a single static bearer token, no IdP, no sidecar.**
- **One no-auth MCP server (DuckDuckGo)** reachable through the gateway, behind a verification gate.

**Milestone 2 (built — see [`docs/sidecar-entra.md`](docs/sidecar-entra.md)):** per-user identity
(Entra ID JWTs) + per-user upstream credentials (per-user GitHub PATs from Azure Key Vault),
delivered with the **Entra sidecar** (source under `sidecar/`) — a FastMCP server that speaks MCP
over HTTP, runs as its own Deployment, and is wired into the data plane via `dataPlane.pluginConfig`.
See the dedicated Milestone 2 section below.

## Deployment architecture (two-phase, operator-driven)

```
helm install (OCI appliance chart) → mcp-operator + bundled PostgreSQL + Redis + umbrella Secret
        ↓ (operator watches)
GatewayServiceConfig CR (oc process) → operator creates <cr>-cp (8080) + <cr>-dp (8081)
                                       Deployments/Services + RBAC
you create → OpenShift Routes (edge TLS) for cp and dp
MCPServer CR → operator creates Deployment+Service `mcp-<name>`
catalog ConfigMap + MCPEnvironment + MCPGateway → a live endpoint at
        https://<dp-route>/gateways/sk/<gateway-name>/mcp
```

**Crucial nuance:** `helm install` installs the **operator (+ Postgres + Redis)**, *not* the
gateway. The CP/DP gateway is rendered by the **operator** from the `GatewayServiceConfig` CR.
The appliance umbrella chart has exactly three subcharts — `mcp-operator`, `postgres`, `redis` —
and **no `gateway-service` subchart**.

## Files

| File | What it is |
|------|-----------|
| `README.md` | The install guide — Milestone 1 (source of truth; ~12 steps + appendix + troubleshooting). |
| `CLAUDE.md` | This file — agent reference. |
| `gatewayserviceconfig.yaml` | OpenShift **Template** for the GatewayServiceConfig CR. Apply with `oc process -f … -p VERSION=… -p CLUSTER_DOMAIN=… \| oc apply -n mcp-gateway -f -`. Contains the no-sidecar bearer-auth config (M1). |
| `mcpserver-duckduckgo.yaml` | M1 MCPServer CR — DuckDuckGo no-auth server (`docker.io/mikegcoleman/demo-mcp-duckduckgo:latest`, multi-arch). |
| `catalog-and-gateway.yaml` | M1 catalog ConfigMap (`mcp-catalog`) + MCPEnvironment (`pov-env`) + MCPGateway (`pov-gateway`). |
| `docs/sidecar-entra.md` | Milestone 2 guide (Entra auth + per-user Key Vault creds), continues from the README. |
| `docs/azure-setup.md` | M2 Azure prerequisites (Portal steps + a scripted `az` CLI appendix). |
| `mcpserver-github.yaml` | M2 MCPServer CR — GitHub server (per-user PAT injection; `auth_delegation: gateway`). |
| `manifests/sidecar-deployment.yaml` | M2 Entra sidecar Deployment + Service (`mcp-entra-sidecar`) — the one standalone component. |
| `sidecar/` | M2 Entra sidecar source (Python/FastMCP) + Dockerfile. |
| `servers/github/` | M2 GitHub MCP server Dockerfile (builds `github/github-mcp-server` from upstream). |

There is no build/test/lint. The README renders on GitHub; manifests are validated against live
CRDs with `oc apply --dry-run=server` and the Template with `oc process --local`. Both milestones
were run end-to-end on a fresh ARO cluster.

## Resource names used by the guide

| Thing | Name |
|---|---|
| Namespace / OpenShift project | `mcp-gateway` |
| GatewayServiceConfig CR (and CP/DP service prefix) | `mcp-gw` → `mcp-gw-cp` (8080), `mcp-gw-dp` (8081) |
| gateway-service ServiceAccount | `mcp-gw` (operator does **not** create it — you must) |
| Operator (from Helm) | Deployment/SA/ClusterRole `mcp-gateway-mcp-operator` |
| Pull secret | `ghcr-pull-secret` |
| Bearer-token Secret | `mcp-gateway-auth` (key `auth-token`) → DP env `MCP_GATEWAY_AUTH_TOKEN` |
| Umbrella Secret (chart-rendered) | `mcp-gateway-gateway-service` (`<release>-gateway-service`) |
| MCPServer / catalog / env / gateway | `duckduckgo` / `mcp-catalog` / `pov-env` / `pov-gateway` |
| M2: Entra sidecar (svc) / GitHub MCPServer (svc) / SP-creds Secret | `mcp-entra-sidecar` / `github`→`mcp-github` / `azure-sp-credentials` |

## Artifact model (how things are pulled)

- **Charts (OCI, Helm 3.7+):** `oci://ghcr.io/docker/mcp-gateway-enterprise-releases/charts/mcp-gateway-appliance` (and `/mcp-operator`).
- **Images:** `ghcr.io/docker/mcp-gateway-enterprise-releases/{gateway-service,mcp-operator}`. The
  operator image default is rewritten to the `-releases` path at publish time; `gateway-service`
  is set in the CR's `spec.image.repository`.
- **Versioning:** chart and images share a release tag (e.g. `0.0.59`); keep `--version`, the CR
  `spec.version`, and mirror tags in lockstep. Always check the Releases page for the latest.
- **Postgres/Redis** come from public `docker.io` (not rewritten) — subject to Docker Hub rate limits.

## Client auth (Milestone 1) — how it works

`spec.sidecar.enabled: false` + `spec.dataPlane.pluginConfig` selecting the gateway core's
built-in provider:
```yaml
plugins:
  gateway_auth:
    provider: in-memory
    implementation: anonymous-desktop-bearer-token
    config: { tenant_id: default }
```
The DP validates `Authorization: Bearer <token>` (timing-safe) against `MCP_GATEWAY_AUTH_TOKEN`,
injected from the `mcp-gateway-auth` Secret via `spec.dataPlane.extraEnv`. This is a **single
shared identity** — per-user identity requires an IdP, which Milestone 2 adds.

## Critical gotchas (the battle-tested list — read before answering deployment questions)

These were all hit and fixed during a real ARO bring-up. Most are operator/chart gaps not yet in
a published release.

1. **Two missing finalizers (Step 5b workaround).** The operator ClusterRole ships
   `finalizers` for `mcpgateways`/`mcpenvironments` but **not** `gatewayserviceconfigs` or
   `mcpservers`. Without them the operator can't set `blockOwnerDeletion`, so the gwsvc stays
   `Provisioning` and any MCPServer stays `Failed` with no pod
   (`cannot set blockOwnerDeletion … can't set finalizers on`). Fix: `oc edit clusterrole
   mcp-gateway-mcp-operator` and add both `…/finalizers` rules (`update`,`patch`).
2. **SCC (Step 3).** Chart containers run as fixed UIDs (operator 65532, Postgres 70, Redis 999)
   that `restricted-v2` rejects. Grant `nonroot-v2` to SAs `mcp-gateway-mcp-operator`, `default`,
   `mcp-gw` via **`oc adm policy add-scc-to-user`** (note: `add-scc-to-serviceaccount` is **not**
   a valid oc subcommand — common mistake).
3. **Leader-election lease RBAC.** `spec.controlPlane.leaderElection.{enabled: true, backend:
   k8s-lease}` is **required even at `replicas: 1`** — the CP always runs a provisioner that
   acquires a lease, and the operator only creates the lease Role/RoleBinding for the gateway SA
   when leader election is enabled. Without it, **every MCPGateway hangs in `Creating`**
   (`leases.coordination.k8s.io "<cr>-provisioner" is forbidden`).
4. **Pull secret on macOS/Windows.** Don't build it from `~/.docker/config.json` — Docker Desktop
   stores the real token in the OS keychain (`credsStore`), leaving `auths` empty → pods get
   `unauthorized`. Use `oc create secret docker-registry … --docker-username/--docker-password`
   (sourced from `gh`).
5. **Image architecture.** OpenShift/ARO nodes are **amd64**. Single-arch (arm64-only) images
   built on Apple Silicon fail with `no image found in image index for architecture "amd64"`.
   Publish server/sidecar images multi-arch: `docker buildx build --platform
   linux/amd64,linux/arm64 … --push`.
6. **`helm install` installs the operator, not the gateway** (see Architecture).
7. **CRDs are install-only.** `helm upgrade` never updates them; pull the `mcp-operator` chart and
   `oc apply -f mcp-operator/crds/` when schemas change.
8. **`oc process` strips `namespace`** from rendered objects — apply the CR with `-n mcp-gateway`.

## MCP protocol reality (for testing the gateway over HTTP)

- The gateway is a **stateful MCP streamable-http server**. A client must handshake:
  `initialize` → `notifications/initialized` → `tools/*`, carrying the **`Mcp-Session-Id`**
  returned by `initialize`. A single-shot `tools/list` is rejected
  (`invalid during session initialization`).
- Responses are **Server-Sent-Events** (`event:` / `data:` lines) — request
  `Accept: application/json, text/event-stream` and extract the JSON from the `data:` frame.
- **Tool names are server-prefixed:** DuckDuckGo's `search` is exposed as `duckduckgo__search`.
- **The MCPGateway URL** is at `.status.endpoints.sk` (server-key endpoint), **not** `.status.url`.
  (`endpoints` also has `id` (by UUID) and `registry`.)
- **MCPServer healthy phase is `Running`** (not `Ready`).
- The realistic test is to point a real MCP client (Claude Code / Cursor) at the gateway with a
  static `Authorization: Bearer` header — the client drives the handshake. README Step 11c also
  has a pure-curl handshake script.

## Pending upstream fixes (delete the workarounds once released)

A release built from a `main` that contains these lets you drop the manual steps:
- Add `gatewayserviceconfigs/finalizers` **and** `mcpservers/finalizers` to the operator
  ClusterRole template → removes Step 5b.
- A chart-side `openshift.scc.enabled` template granting `nonroot-v2` → simplifies Step 3.
- (Leader-election lease RBAC is *not* a bug — it's correct config once `leaderElection` is set.)

## Milestone 2 — Entra ID auth + per-user credentials (custom sidecar)

Full guide: [`docs/sidecar-entra.md`](docs/sidecar-entra.md) (Azure prereqs: [`docs/azure-setup.md`](docs/azure-setup.md)).
Purely additive on top of Milestone 1 — does not re-create the namespace/operator/gateway/DuckDuckGo.

**Components added:** the Entra sidecar (`sidecar/`, a FastMCP server serving MCP over **HTTP** on
`:8080/mcp`, deployed standalone via `manifests/sidecar-deployment.yaml` → Service
`mcp-entra-sidecar`), and a GitHub `MCPServer` (`mcpserver-github.yaml` → Service `mcp-github`,
same operator-managed pattern as M1's DuckDuckGo).

**Sidecar tools:** `authenticate` (validate Entra JWT → principal, `id` = the token's `oid`),
`get_connection_headers` (look up `<server>-pat-<oid>` in Key Vault → `Authorization` header),
`record-*` (telemetry stubs), `oauth-*` (broker — future phase, present in source, out of scope).

**Key Vault secret naming:** `{server_name}-pat-{oid}`, e.g. `github-pat-<oid>`. RBAC-mode vault;
the SP needs `Key Vault Secrets User`.

**Required sidecar env** (source: `sidecar/server.py:40-45`; crashes if absent): `ENTRA_TENANT_ID`,
`ENTRA_AUDIENCE` (= app **client ID**, not the `api://` URI — v2 tokens put client_id in `aud`),
`ENTRA_CLIENT_ID`, `GATEWAY_RESOURCE`, `AZURE_KEYVAULT_URL`; plus `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/
`AZURE_CLIENT_SECRET` (consumed by the Azure SDK, from the `azure-sp-credentials` Secret).
Optional: `ENTRA_RESOURCE_URI`, `MCP_GATEWAY_STORE_TYPE` (`kv`/`local`/`auto`), `MCP_GATEWAY_OAUTH_*`.

**The wiring (the crux):** with `spec.dataPlane.sidecar.enabled: false` (sidecar runs standalone,
not operator-injected), set `spec.dataPlane.pluginConfig` to point `gateway_auth` + the credential
delegator at the sidecar URL:
```yaml
plugins:
  gateway_auth: { provider: mcp, server: "http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp" }
auth_delegators:
  - { name: entra-sidecar, strategy: remote, provider: mcp, server: "http://mcp-entra-sidecar.mcp-gateway.svc.cluster.local:8080/mcp" }
auth_delegation:
  defaults: { remote: entra-sidecar }
```
Re-wiring the gwsvc from M1's bearer plugin to this **replaces** client auth: after it, requests
need an Entra JWT (the M1 static bearer no longer works). Catalog entries with
`auth_delegation: gateway` (e.g. `github`) trigger the per-user PAT injection; DuckDuckGo stays
plain (public).

**M2 deploy gotchas:**
- GitHub upstream runs as **root** → `mcpserver-github.yaml` sets `runAsNonRoot: false` and needs
  `anyuid` on the `default` SA (additive to M1's `nonroot-v2`).
- The sidecar has `readOnlyRootFilesystem: true`, so it mounts writable `/tmp` + `/.cache` emptyDirs.
- Build the sidecar + GitHub images **amd64** for OCP nodes (locally multi-arch, or on-cluster via
  `oc new-build --binary --strategy=docker` — see the guide's Step 1B; there's no usable local
  Docker in some environments).
- **Catalog changes need a CP restart.** Adding `github` to the catalog ConfigMap does nothing
  until `oc rollout restart deploy/mcp-gw-cp` (the CP reads the catalog file at startup).

**Why the sidecar source looks the way it does (run-2 fixes — do NOT "simplify" these away):**
- `servers/github/Dockerfile` pins `golang:1.25-alpine` — upstream `github-mcp-server` needs Go ≥1.25;
  older tags fail the build.
- `sidecar/requirements.txt` pins `fastmcp>=3.4,<4` **and** `server.py` calls
  `mcp.http_app(path="/mcp", host_origin_protection=False)` — current FastMCP's DNS-rebinding
  protection otherwise rejects the gateway's in-cluster Service-DNS Host with **HTTP 421** and the
  DP CrashLoopBackOffs.
- `authenticate` sets a **top-level `tenant_id`** (default `default`, override `GATEWAY_TENANT_ID`) —
  the DP's tenant guard rejects a principal whose tenant doesn't match the gateway's (`404
  gateway_not_found`).
- `get_connection_headers` accepts extra `headers`/`method`/`path` kwargs the gateway sends —
  FastMCP strict-schema validation rejects unknown keys, which makes credential injection fail
  **silently** (upstream then 401s and its tools never appear). This is the AGENTS.md
  "strict-schema delegator" hazard; any custom delegator must tolerate extra kwargs.

**Connecting clients (M2 — see `docs/sidecar-entra.md` Step 9):** the gateway needs an Entra JWT
per request; how clients get one differs, and this is where the real friction lives.
- **VS Code Copilot** — interactive Microsoft sign-in via its built-in Entra provider. **Verified.**
  Treat it as the *reference/control case*, not a recommendation (many customers don't use Copilot):
  a client with native Entra support signs in with zero app-specific config, which proves the
  server-side Entra setup is correct and isolates the Claude Code gap below as client-side. Needs the
  app to pre-authorize VS Code's client-id (`aebc6443…`) + be a public client with an
  `http://localhost` redirect (azure-setup §1e/§1e-2).
- **Claude Code** — **interactive Entra OAuth does not work** (two upstream gaps: Entra has no
  RFC 7591 DCR → *"does not support dynamic client registration"*; and even with `--client-id`,
  Claude Code's RFC 8707 `resource` param = the gateway URL, which Entra rejects against the scope →
  `AADSTS9010010`). The gateway data plane sets the PRM `resource` per RFC 9728, so it can't be
  fixed sidecar-side. **Verified working path: a `headersHelper` token script** (`az account
  get-access-token --scope api://<app-id>/access`), fleet-distributed via `managed-mcp.json` (MDM).
  Not a gateway limitation — a Claude↔Entra gap.
- Static bearer works for any HTTP client as a fallback.

## Working in this repo

- Edit `README.md` (the guide) directly; it is the source of truth and renders on GitHub.
- Keep the inline CR YAML in README Step 8 in sync with `gatewayserviceconfig.yaml`.
- Validate manifests against a live cluster: `oc apply --dry-run=server -f <file>` and
  `oc process --local -f gatewayserviceconfig.yaml -p VERSION=x -p CLUSTER_DOMAIN=y`.
- **Confidentiality:** never put specific customer/partner names in committed content; use generic
  terms ("customer", "Design Partner", "reference deployment").

## References

- Gateway artifacts + operator reference: the `docs-user-guide/` in the release repo
  (`mcp-gateway-enterprise-releases`) the gateway is installed from.
- Provisioning a compatible cluster: ARO (`az aro create`, amd64 nodes, ≥44 Dsv3 vCPUs, default
  `managed-csi` StorageClass, built-in Routes) satisfies every prerequisite the guide assumes.
