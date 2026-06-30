# Maintainer notes

Internal notes for this package — **not** part of the customer-facing guide (`README.md`).

## Contents

| File | What it is |
|------|-----------|
| `README.md` | The install guide (source of truth). |
| `README.pdf` | Rendered PDF of the guide. |
| `gatewayserviceconfig.yaml` | OpenShift **Template** for the GatewayServiceConfig CR (`oc process`). Includes the no-sidecar bearer-auth config. |
| `mcpserver-duckduckgo.yaml` | MCPServer CR — DuckDuckGo (no-auth tier). |
| `catalog-and-gateway.yaml` | Catalog ConfigMap + MCPEnvironment + MCPGateway. |
| `md2pdf.py` | Dependency-free Markdown→PDF renderer (this environment has no pandoc/Chromium). |

Regenerate the PDF after editing the guide:
```bash
python3 md2pdf.py README.md README.pdf
```

## Status

- **Milestone 1 — done (in the guide):** gateway up on OpenShift from the OCI release artifacts,
  with **basic bearer-token client auth (no IdP, no sidecar)**, and a **no-auth MCP server
  (DuckDuckGo)** working end-to-end behind a verification gate (401→200 + real tool results).
- **Milestone 2 — not yet (noted as "what's next" in the guide):** per-user upstream credentials
  (e.g. per-user GitHub PATs) via the **preset plugin sidecar** + an IdP (Entra on Azure).
  Upstream-OAuth MCP servers are a separate later step (the preset's OAuth lane isn't implemented).

All manifests/templates were **validated against the live CRDs** on the test cluster
(`oc apply --dry-run=server`) and the template via `oc process --local`. Schema is good; full
runtime (the DuckDuckGo call on a fresh deploy) has not been executed yet — see caveats.

## Open items / caveats

- **Runtime not yet proven on a fresh deploy.** Re-apply the bearer-auth CR template + the two
  manifests, then run the Step 11 gates, to confirm end-to-end.
- **DuckDuckGo image non-root on OpenShift** — unverified; the MCPServer CR has a
  `runAsNonRoot: false` + `anyuid` fallback note in case the image needs root.
- **Temporary workarounds in the guide** (until fixed upstream + released): the
  `gatewayserviceconfigs/finalizers` ClusterRole grant (Step 5b) and the manual `nonroot-v2` SCC
  grants (Step 3). The leases RBAC is already handled by the operator.

## Test cluster (Azure Red Hat OpenShift)

Built for validation:
- Cluster `aro-mcp`, resource group `aro-mcp-rg`, region `eastus`, OpenShift 4.18.
- **Bills ~\$2/hr until torn down:**
  ```bash
  az aro delete -g aro-mcp-rg -n aro-mcp --yes
  az group delete -n aro-mcp-rg --yes
  ```
