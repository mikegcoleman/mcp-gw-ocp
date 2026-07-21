"""Unit tests for sidecar/policy/policy.py.

Run from the sidecar/ directory:
    pip install pyyaml pytest
    pytest tests/test_policy.py -v
"""
import os
import tempfile

import pytest
import yaml

from policy.policy import _evaluate, _load_rules, _match

# ── _match ────────────────────────────────────────────────────────────────────

def test_match_exact_server_and_tool():
    rule = {"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny"}
    assert _match(rule, "team-a-granola", "list_meetings")

def test_match_server_mismatch():
    rule = {"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny"}
    assert not _match(rule, "team-b-notion", "list_meetings")

def test_match_tool_mismatch():
    rule = {"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny"}
    assert not _match(rule, "team-a-granola", "get_meeting")

def test_match_no_tool_matches_any_tool():
    rule = {"serverName": "team-a-granola", "effect": "deny"}
    assert _match(rule, "team-a-granola", "list_meetings")
    assert _match(rule, "team-a-granola", "get_meeting")
    assert _match(rule, "team-a-granola", "anything")

def test_match_no_server_matches_any_server():
    rule = {"toolName": "list_meetings", "effect": "deny"}
    assert _match(rule, "team-a-granola", "list_meetings")
    assert _match(rule, "team-b-notion", "list_meetings")

# ── _evaluate ─────────────────────────────────────────────────────────────────

REQ = {
    "principal": {"id": "test-oid"},
    "action": "invokeTool",
    "resource": {"server_name": "team-a-granola", "tool_name": "list_meetings"},
}

def test_allow_empty_rules():
    result = _evaluate(REQ, _rules=[])
    assert result["allowed"] is True
    assert result["policy_source"] == "sidecar-configmap-policy"

def test_deny_exact_match():
    rules = [{"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny", "reason": "blocked for demo"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is False
    assert result["reason"] == "blocked for demo"
    assert result["outcome"] == "policy_rule"

def test_deny_server_only_blocks_all_tools():
    rules = [{"serverName": "team-a-granola", "effect": "deny", "reason": "server blocked"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is False

def test_allow_wrong_server():
    rules = [{"serverName": "team-b-notion", "toolName": "list_meetings", "effect": "deny"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is True

def test_allow_wrong_tool():
    rules = [{"serverName": "team-a-granola", "toolName": "get_meeting", "effect": "deny"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is True

def test_first_deny_wins():
    rules = [
        {"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny", "reason": "first"},
        {"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny", "reason": "second"},
    ]
    result = _evaluate(REQ, _rules=rules)
    assert result["reason"] == "first"

def test_non_deny_effect_is_ignored():
    rules = [{"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "allow"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is True

def test_missing_reason_uses_fallback():
    rules = [{"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny"}]
    result = _evaluate(REQ, _rules=rules)
    assert result["allowed"] is False
    assert "team-a-granola" in result["reason"]

def test_empty_request_allows():
    result = _evaluate({}, _rules=[])
    assert result["allowed"] is True

# ── _load_rules ───────────────────────────────────────────────────────────────

def test_load_rules_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_POLICY_DIR", str(tmp_path / "nonexistent"))
    assert _load_rules() == []

def test_load_rules_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_POLICY_DIR", str(tmp_path))
    assert _load_rules() == []

def test_load_rules_single_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_POLICY_DIR", str(tmp_path))
    (tmp_path / "team-a.yaml").write_text(yaml.dump({
        "version": "1",
        "rules": [{"serverName": "team-a-granola", "toolName": "list_meetings", "effect": "deny"}],
    }))
    rules = _load_rules()
    assert len(rules) == 1
    assert rules[0]["serverName"] == "team-a-granola"

def test_load_rules_merges_multiple_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_POLICY_DIR", str(tmp_path))
    (tmp_path / "team-a.yaml").write_text(yaml.dump({
        "version": "1",
        "rules": [{"serverName": "team-a-granola", "effect": "deny"}],
    }))
    (tmp_path / "team-b.yaml").write_text(yaml.dump({
        "version": "1",
        "rules": [{"serverName": "team-b-notion", "effect": "deny"}],
    }))
    rules = _load_rules()
    assert len(rules) == 2

def test_load_rules_skips_invalid_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_POLICY_DIR", str(tmp_path))
    (tmp_path / "bad.yaml").write_text("{{invalid yaml{{")
    (tmp_path / "good.yaml").write_text(yaml.dump({
        "version": "1",
        "rules": [{"serverName": "team-a-granola", "effect": "deny"}],
    }))
    rules = _load_rules()
    assert len(rules) == 1
