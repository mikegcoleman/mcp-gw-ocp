# Troubleshooting

Collected troubleshooting across all three milestones. If you are working through the guides in
order, start with the Milestone 1 section and work forward.

---

## Milestone 1 — Core install

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

## Milestone 2 — Entra auth & per-user credentials

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
| Client OAuth: *"does not support dynamic client registration"* (Claude Code) | DCR proxy not reachable or `DCR_PROXY_URL` not set in `sidecar-config` | Verify `DCR_PROXY_URL=https://mcp-gw-dp.<domain>/dcr` in the sidecar env; confirm the proxy Route is up |
| Client OAuth: `AADSTS9010010` (resource ≠ scope) | DCR proxy not routing correctly — Claude Code is hitting Entra directly | Same as above — check `DCR_PROXY_URL` and the proxy deployment |
| Key Vault access denied | SP lacks `Key Vault Secrets User`, or KV is in access-policy (not RBAC) mode | Grant the role on the vault; switch KV to RBAC |

**Bypassing the DCR proxy for diagnostics** — if you're seeing auth errors and aren't sure
whether the issue is the proxy or the gateway/Entra config, bypass the proxy entirely with a
static token to confirm the core stack is sound:

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

If tools load with the helper but not with the DCR proxy, the proxy or its `DCR_PROXY_URL` env
var is the issue. If tools don't load with either, the problem is in the gateway config or Entra
app registration.

---

## Milestone 3 — Group-based access & GitOps

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
