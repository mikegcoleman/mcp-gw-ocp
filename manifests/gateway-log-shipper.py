#!/usr/bin/env python3
"""Tails CP/DP/sidecar pod logs via the Kubernetes API and pushes them into Loki
unmodified, bypassing the gateway's own OTel "customer" pipeline — which
unconditionally strips mcp.principal.id (and other identity fields) via its
generated transform/strip_internal processor before anything reaches
tenantRouting (see docs/observability.md "Gateway logs lack identity (who did
what)"). This is the only current way to get per-user audit detail (who called
which tool on which server, policy allow/deny, PAT delegation) into Loki/Grafana.

Uses only the stdlib (no pip installs) and the pod's own ServiceAccount token —
needs just `pods`/`pods/log` read RBAC (see gateway-log-shipper.yaml), not
hostPath or a privileged SCC like a node-level log-scraping DaemonSet would.
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NS = "mcp-gateway"
API = "https://kubernetes.default.svc"
LOKI_PUSH = "http://loki.mcp-gateway.svc.cluster.local:3100/loki/api/v1/push"
TOKEN = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read().strip()
CTX = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

TARGETS = [
    {"selector": "app.kubernetes.io/component=control-plane", "container": "control-plane", "component": "cp"},
    {"selector": "app.kubernetes.io/component=data-plane", "container": "data-plane", "component": "dp"},
    {"selector": "app=mcp-entra-sidecar", "container": "sidecar", "component": "sidecar"},
]


def api_get(path):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.load(r)


def resolve_pod(selector):
    data = api_get(f"/api/v1/namespaces/{NS}/pods?labelSelector={urllib.parse.quote(selector)}")
    items = [p for p in data.get("items", []) if p["status"].get("phase") == "Running"]
    return items[0]["metadata"]["name"] if items else None


def push_batch(component, pod, lines):
    if not lines:
        return
    values = [[str(int(time.time() * 1e9) + i), ln] for i, ln in enumerate(lines)]
    payload = {"streams": [{"stream": {"job": "gateway-raw", "component": component, "pod": pod}, "values": values}]}
    req = urllib.request.Request(
        LOKI_PUSH, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[{component}] push error: {e}", flush=True)


def tail(target):
    while True:
        pod = resolve_pod(target["selector"])
        if not pod:
            print(f"[{target['component']}] no running pod matching {target['selector']}, retrying...", flush=True)
            time.sleep(10)
            continue
        print(f"[{target['component']}] tailing pod={pod} container={target['container']}", flush=True)
        path = f"/api/v1/namespaces/{NS}/pods/{pod}/log?container={target['container']}&follow=true&tailLines=20"
        req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=None) as resp:
                buf = []
                last_flush = time.time()
                for raw in resp:
                    line = raw.decode(errors="replace").rstrip("\n")
                    if line:
                        buf.append(line)
                    if len(buf) >= 20 or time.time() - last_flush > 2:
                        push_batch(target["component"], pod, buf)
                        buf = []
                        last_flush = time.time()
                push_batch(target["component"], pod, buf)
        except Exception as e:
            print(f"[{target['component']}] stream error: {e}, re-resolving pod...", flush=True)
            time.sleep(5)


threads = [threading.Thread(target=tail, args=(t,), daemon=True) for t in TARGETS]
for th in threads:
    th.start()
for th in threads:
    th.join()
