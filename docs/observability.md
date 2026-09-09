# Observability — Metrics + Logs (Prometheus, Loki, Grafana)

This is purely additive on top of whatever milestone you're on (works from Milestone 1
onward — it only touches the `GatewayServiceConfig` CR and adds new Deployments/Services,
nothing in the auth/catalog/policy path changes). It wires the gateway's built-in
OpenTelemetry Collector to:

- **Metrics** — scraped by OpenShift's own Prometheus (user-workload monitoring).
- **Logs** — shipped to Loki.
- **Grafana** — one pane of glass over both.

## Why this needs a small "aggregator" collector, not just CR fields

The product ships an OTel Collector sidecar (on both CP and DP pods), configured entirely
through `GatewayServiceConfig.spec.observability.collector` — see the release repo's
`docs-user-guide/operator-reference.md` (Observability section) and
`docs-user-guide/customer-log-export.md` for the full field reference. Two fields matter here:

- `customerExporter.type: prometheus` — exposes a plain `/metrics` scrape endpoint for the
  **metrics/customer** pipeline. Simple, self-service, no external target to configure.
- `tenantRouting` — fans **both** `metrics/customer` and `logs/customer` out to an OTLP
  endpoint you provide, keyed on `mcp.principal.tenant_id`.

These are **mutually exclusive** for the customer pipelines: enabling `tenantRouting`
supersedes `customerExporter`/`customerLogsExporter` entirely (per the CRD schema — checked
against `mcp-operator` chart 0.0.89's `mcp.docker.com_gatewayserviceconfigs.yaml`). There is
no CR field to point `customerLogsExporter` at an arbitrary self-service OTLP target outside
of `tenantRouting` — the field is just a name string that "must be defined in the collector
config," and nothing exposes that definition except `tenantRouting`. So you can't get
plain-Prometheus metrics *and* a self-configured log destination at the same time straight
from the gateway's own collector.

The fix: point `tenantRouting` at a small OTel Collector **you own** (`otel-aggregator`
below). It receives both signals over OTLP, then fans out however you like — in this case,
a Prometheus-scrape exporter for metrics and an OTLP-to-Loki exporter for logs. This is also
genuinely how you'd do this against a real customer's own observability stack: point
`tenantRouting` at their collector, they own the fan-out from there.

```
Gateway CP/DP pods
  └─ otel-collector sidecar (built-in, per-pod)
         │  tenantRouting: tenantID "default" → OTLP
         ▼
  otel-aggregator (Deployment, this repo's addition)
         ├─ metrics ──▶ :8889/metrics ──▶ ServiceMonitor ──▶ OpenShift user-workload Prometheus ──▶ Grafana
         └─ logs ─────▶ Loki (native OTLP ingest, /otlp/v1/logs) ───────────────────────────────▶ Grafana
```

`tenantID: default` matches the Entra sidecar's `authenticate()` principal
(`GATEWAY_TENANT_ID`, default `"default"` — see `sidecar/server.py`), so every authenticated
request through this gateway routes here. If you run multiple tenants with different
`GATEWAY_TENANT_ID` values, add one `tenants[]` entry per tenant.

---

## Prerequisites

- Milestone 1 complete (gateway up and reachable).
- `helm` (3.7+, already required by the main guide).
- Cluster-admin access, for the user-workload-monitoring config change (Step 1) — this is a
  cluster-scoped setting, not namespace-scoped; check with whoever owns the cluster if it's
  shared.

---

## Step 1 — Enable OpenShift user-workload monitoring

```bash
oc apply -f manifests/cluster-monitoring-config.yaml
```

> If `cluster-monitoring-config` already exists in your cluster with other settings, merge
> `enableUserWorkload: true` into its existing `config.yaml` instead of applying this file
> as-is — it would otherwise overwrite whatever's already there.

Wait for the stack to come up:

```bash
oc get pods -n openshift-user-workload-monitoring -w
# Expect: prometheus-operator, prometheus-user-workload-{0,1}, thanos-ruler-user-workload-{0,1}
```

## Step 2 — Deploy Loki

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm install loki grafana/loki --version 7.3.0 -n mcp-gateway -f manifests/loki-values.yaml

# The main loki pod's fixed UID (10001) needs nonroot-v2, same pattern as the main guide's
# Step 3. The chart's memcached caches are disabled in loki-values.yaml instead of granted
# an SCC — their fixed UID (11211) falls outside the range even nonroot-v2 allows.
oc adm policy add-scc-to-user nonroot-v2 -z loki -n mcp-gateway

oc rollout status statefulset/loki -n mcp-gateway --timeout=180s
```

> If the pod stays stuck with no `FailedCreate` retry after granting the SCC, force an
> immediate retry: `oc scale statefulset loki -n mcp-gateway --replicas=0 && oc scale
> statefulset loki -n mcp-gateway --replicas=1` (the StatefulSet controller's retry backoff
> can otherwise take several minutes).

## Step 3 — Deploy the aggregator OTel Collector

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm install otel-aggregator open-telemetry/opentelemetry-collector \
  --version 0.172.1 -n mcp-gateway -f manifests/otel-aggregator-values.yaml

oc rollout status deploy/otel-aggregator-opentelemetry-collector -n mcp-gateway --timeout=120s
```

## Step 4 — Wire the gateway to the aggregator

Add the `observability` block to `gatewayserviceconfig.yaml`'s `spec` (see the file in this
repo for the exact block — it's already there, alongside the `dataPlane`/`controlPlane`
config from whichever milestone you're on). Re-apply and force a fresh sidecar injection:

```bash
oc process -f gatewayserviceconfig.yaml \
  -p VERSION="$VERSION" \
  -p CLUSTER_DOMAIN="$CLUSTER_DOMAIN" \
  | oc apply -n mcp-gateway -f -

oc delete pod -l app.kubernetes.io/component=control-plane -n mcp-gateway
oc delete pod -l app.kubernetes.io/component=data-plane -n mcp-gateway
oc rollout status deploy/mcp-gw-cp -n mcp-gateway --timeout=150s
oc rollout status deploy/mcp-gw-dp -n mcp-gateway --timeout=150s
```

Confirm the `otel-collector` sidecar container landed on both pods:

```bash
oc get pod -n mcp-gateway -l app.kubernetes.io/component=data-plane \
  -o jsonpath='{.items[0].spec.containers[*].name}'
# Expect: data-plane otel-collector
```

## Step 5 — Let OpenShift's Prometheus scrape it

```bash
oc apply -f manifests/otel-aggregator-servicemonitor.yaml
```

## Step 6 — Deploy Grafana

```bash
oc apply -f manifests/grafana-prom-rbac.yaml
oc adm policy add-scc-to-user nonroot-v2 -z grafana -n mcp-gateway

PROM_TOKEN=$(oc create token grafana-prom-reader -n mcp-gateway --duration=8760h)
python3 -c "
import os
c = open('manifests/grafana-values.yaml').read().replace('\${PROM_TOKEN}', os.environ['PROM_TOKEN'])
open('/tmp/grafana-values-rendered.yaml', 'w').write(c)
"

helm repo add grafana https://grafana.github.io/helm-charts   # if not already added
helm install grafana grafana/grafana --version 10.5.15 -n mcp-gateway -f /tmp/grafana-values-rendered.yaml
rm /tmp/grafana-values-rendered.yaml   # contains the real token — don't leave it lying around

oc create route edge grafana --service=grafana --port=service \
  --hostname="grafana.$CLUSTER_DOMAIN" -n mcp-gateway
```

> **The `--port` flag must be the Service's port *name* (`service`), not its number (`80`).**
> This chart's Service declares `port: 80` with a *named* `targetPort` (`grafana`, → container
> port 3000). `oc create route --port=80` matches the Service by number and admits the Route
> fine, but the router never populates a `server` line for it — you get a persistent `503
> Application is not available` with valid Endpoints and a healthy pod, and even a full router
> pod restart doesn't fix it. Matching by name works. (The `mcp-gw-cp`/`mcp-gw-dp` Services in
> the main guide dodge this because their `targetPort` is numeric and identical to `port`.)

Get the admin password:

```bash
oc get secret grafana -n mcp-gateway -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

## Step 7 — Verify end-to-end

Generate some traffic (any authenticated tool call through the gateway works — see README
Step 11c or `docs/sidecar-entra.md` Step 8 for the handshake), then:

```bash
# Metrics reached the aggregator and are in Prometheus-scrape format:
oc run curltest --image=curlimages/curl --rm -i --restart=Never -n mcp-gateway -- \
  curl -sS http://otel-aggregator-opentelemetry-collector.mcp-gateway.svc.cluster.local:8889/metrics \
  | grep mcp_http_request_duration

# Logs reached Loki:
oc run curltest --image=curlimages/curl --rm -i --restart=Never -n mcp-gateway -- \
  curl -sS -G "http://loki.mcp-gateway.svc.cluster.local:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={service_name=~".+"}' --data-urlencode 'limit=5'

# OpenShift's Prometheus picked up the ServiceMonitor target (via Grafana's datasource):
curl -sS -u "admin:<password>" \
  "https://grafana.$CLUSTER_DOMAIN/api/datasources/uid/<prometheus-ds-uid>/resources/api/v1/query?query=mcp_http_request_duration_milliseconds_count" \
  -k | jq '.data.result | length'
```

Open `https://grafana.$CLUSTER_DOMAIN`, log in, and both the **Prometheus (UWM)** and
**Loki** datasources should already be provisioned (Connections → Data sources) with green
health checks.

## Step 8 — Import the "MCP Gateway — Overview" dashboard

`manifests/grafana-dashboard-mcp-gateway.json` covers request rate/latency/status by CP vs
DP, tool call rate and p95 latency by server, a top-tools table, and a log volume + raw log
view from Loki. It uses the metric and label names the gateway actually emits (`mcp_component`,
`mcp_server_name`, `mcp_tool_name`, `http_status_class`, etc.) — confirmed against a live
gateway rather than guessed from the docs.

Import it via the API (this persists into Grafana's own database, backed by the PVC from
`grafana-values.yaml`, so it survives pod restarts without re-importing):

```bash
python3 -c "
import json
dash = json.load(open('manifests/grafana-dashboard-mcp-gateway.json'))
print(json.dumps({'dashboard': dash, 'overwrite': True}))
" > /tmp/dashboard-payload.json

curl -sS -u "admin:<password>" -X POST "https://grafana.$CLUSTER_DOMAIN/api/dashboards/db" \
  -H "Content-Type: application/json" -k --data-binary @/tmp/dashboard-payload.json
rm /tmp/dashboard-payload.json
```

Or import it by hand: Grafana → Dashboards → New → Import → paste the file's contents.

The dashboard JSON hard-codes the datasource UIDs (`mcp-gw-prometheus-uwm`,
`mcp-gw-loki`) — these are pinned explicitly in `grafana-values.yaml`'s `datasources:` block
rather than left for Grafana to auto-assign, specifically so the dashboard JSON can reference
them reliably. If you ever change a datasource's `uid:` in `grafana-values.yaml`, update the
dashboard JSON (or the running dashboard's panel JSON) to match, and add a `deleteDatasources`
entry for the old name so file-provisioning replaces the existing entry instead of erroring
with `Datasource provisioning error: data source not found` (provisioning doesn't re-key an
existing datasource to a new UID on its own).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Loki/Grafana pod `FailedCreate`, SCC error mentioning a fixed UID (11211, 472, etc.) | Chart-fixed non-root UID falls outside restricted-v2's allowed range | For Loki's caches: disable them (already done in `loki-values.yaml`). For Grafana's chown init container: disable it and clear `securityContext` (already done in `grafana-values.yaml`) so OpenShift assigns its own UID/fsGroup. For a plain fixed-UID container (no root/CHOWN requirement): grant `nonroot-v2` to its ServiceAccount. |
| Loki StatefulSet stuck at `0/1` with no new events after granting the SCC | StatefulSet controller's retry backoff | `oc scale statefulset loki -n mcp-gateway --replicas=0` then back to `1` to force an immediate retry. |
| Grafana Route returns `503 Application is not available` despite a Running pod and populated Endpoints | Route's `--port` given as a number when the Service's `targetPort` is a name | Recreate the Route with `--port=<service-port-name>` instead of the number (see Step 6). |
| `oc process` output for the GatewayServiceConfig looks truncated/wrong, or `oc apply` fails with a YAML parse error | A hand-edited Template's `objects:` field isn't a proper YAML list (e.g. `---`-separated documents under it) | `objects:` must be a real list — each resource as a `- apiVersion: ...` item, not a separate `---` document. |
| Aggregator collector `CrashLoopBackOff`: `unknown type: "otlp_http"` | Collector image too old for the chart's exporter-name auto-rewrite (chart renamed `otlphttp` → `otlp_http` for newer collector versions) | Pin the image tag to match what the chart expects (`otel-aggregator-values.yaml` already pins `0.159.0`) — don't reuse the gateway's own collector sidecar's older pinned version (`0.127.0`) here. |
| Aggregator's `/metrics` has no `mcp_*` series after generating traffic | `tenantRouting` misconfigured, or `tenantID` doesn't match the principal's actual tenant | Confirm `gatewayserviceconfig.yaml`'s `tenantRouting.tenants[].tenantID` matches `GATEWAY_TENANT_ID` on the sidecar (default `"default"`); check `oc get configmap mcp-gw-otel-collector -n mcp-gateway -o jsonpath='{.data.collector\.yaml}'` for the generated `routing/customer_metrics` connector's `condition`. |
| Grafana's Prometheus datasource health check fails with 401/403 | `grafana-prom-reader` token expired, wrong, or RBAC not applied | Re-mint the token (`oc create token grafana-prom-reader -n mcp-gateway --duration=8760h`) and update the datasource's `secureJsonData.httpHeaderValue1`; confirm `manifests/grafana-prom-rbac.yaml` is applied. |
| Prometheus/Grafana show no gateway metrics at all | ServiceMonitor not scraped — user-workload monitoring not enabled, or Prometheus Operator hasn't reconciled yet | Confirm `oc get pods -n openshift-user-workload-monitoring` shows a running stack (Step 1); check `oc get servicemonitor otel-aggregator -n mcp-gateway` exists and its `spec.selector` matches the aggregator Service's labels. |
