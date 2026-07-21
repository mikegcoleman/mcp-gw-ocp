"""
ConfigMap-backed policy plugin — implements the evaluate_policy MCP tool
consumed by SidecarPolicyPlugin in pkg/plugins/providers/mcp/policy_plugin.go.

Rules are loaded from YAML files in MCP_GATEWAY_POLICY_DIR (default
/etc/mcp-policy). K8s volume-mounted ConfigMaps update in-place (~60s
kubelet sync); files are re-read on every call so changes take effect
without a sidecar restart.

Rule file format:
  version: "1"
  rules:
    - serverName: team-a-granola  # required; omit to match any server
      toolName: list_meetings      # optional; omit to match any tool
      effect: deny                 # required
      reason: "..."               # optional; surfaced to the caller
"""
import glob
import logging
import os

import yaml

log = logging.getLogger("policy")


def _load_rules() -> list[dict]:
    """Load and merge all policy YAML files from MCP_GATEWAY_POLICY_DIR."""
    policy_dir = os.environ.get("MCP_GATEWAY_POLICY_DIR", "/etc/mcp-policy")
    rules = []
    for path in sorted(glob.glob(os.path.join(policy_dir, "*.yaml"))):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            rules.extend(data.get("rules", []))
        except Exception as e:
            log.warning("policy: failed to load %s: %s", path, e)
    return rules


def _match(rule: dict, server_name: str, tool_name: str) -> bool:
    """Return True if this rule matches the given server and tool."""
    if rule.get("serverName") and rule["serverName"] != server_name:
        return False
    if rule.get("toolName") and rule["toolName"] != tool_name:
        return False
    return True


def _evaluate(request: dict, _rules: list[dict] | None = None) -> dict:
    """Evaluate a policy request. Returns an allow/deny decision dict.

    _rules is injected by unit tests; production callers omit it and the
    function reads from MCP_GATEWAY_POLICY_DIR at call time.
    """
    resource = request.get("resource") or {}
    server_name = resource.get("server_name", "")
    tool_name = resource.get("tool_name", "")

    for rule in (_rules if _rules is not None else _load_rules()):
        if rule.get("effect") == "deny" and _match(rule, server_name, tool_name):
            reason = rule.get("reason") or f"denied by policy rule ({server_name}/{tool_name})"
            log.info("policy: DENY server=%s tool=%s reason=%s", server_name, tool_name, reason)
            return {
                "allowed": False,
                "outcome": "policy_rule",
                "reason": reason,
                "policy_source": "sidecar-configmap-policy",
            }

    return {
        "allowed": True,
        "outcome": "policy_rule",
        "reason": "no deny rule matched",
        "policy_source": "sidecar-configmap-policy",
    }


def register_tools(mcp) -> None:
    """Register evaluate_policy on the given FastMCP instance."""

    @mcp.tool(name="evaluate_policy")
    def evaluate_policy(request: dict) -> dict:
        """Evaluate a single policy request against ConfigMap-backed rules."""
        decision = _evaluate(request)
        log.info(
            "policy: evaluate principal=%s action=%s server=%s tool=%s allowed=%s",
            (request or {}).get("principal", {}).get("id", ""),
            (request or {}).get("action", ""),
            (request or {}).get("resource", {}).get("server_name", ""),
            (request or {}).get("resource", {}).get("tool_name", ""),
            decision["allowed"],
        )
        return {"result": decision}

    log.info("policy: registered evaluate_policy tool")
