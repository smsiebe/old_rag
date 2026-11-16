"""test_desktop_bridge_and_ui_v3.py - TG3 Unit Tests for Desktop Bridge, Wire-Sync, and UI Assets."""

from __future__ import annotations

import json
import re
from pathlib import Path
import pytest

from old_rag.contracts import SourceType
from old_rag.ui.bridge import OldRAGClient, OldRAGDesktopBridge


def test_ui_wire_sync_api_js_matches_bridge_schema_and_zero_cdn():
    """Verify zero CDNs, complete asset inlining, </script> escaping, and action wire-sync parity."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    static_dir = repo_root / "src" / "old_rag" / "ui" / "static"
    schema_path = repo_root / "src" / "old_rag" / "bridge_schema.json"

    assert static_dir.exists(), f"Static dir missing at {static_dir}"
    assert schema_path.exists(), f"Bridge schema missing at {schema_path}"

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    valid_actions = set(schema.get("actions", {}).keys())
    assert len(valid_actions) >= 40, f"Expected at least 40 actions in schema, got {len(valid_actions)}"

    # 1. Zero External CDNs in all static files
    cdn_pattern = re.compile(r'(?:src|href|url)\s*=\s*["\']https?://', re.IGNORECASE)
    import_cdn_pattern = re.compile(r'@import\s+url\(["\']?https?://', re.IGNORECASE)

    for asset_file in static_dir.glob("*.*"):
        if asset_file.suffix not in (".html", ".js", ".css"):
            continue
        text = asset_file.read_text(encoding="utf-8")
        assert not cdn_pattern.search(text), f"External CDN reference found in {asset_file.name}"
        assert not import_cdn_pattern.search(text), f"External @import CDN found in {asset_file.name}"

    # 2. Verify render_desktop_html inlines all assets and escapes </script>
    bridge = OldRAGDesktopBridge()
    inlined_html = bridge.render_desktop_html(inline_assets=True)

    assert "window.__OLDRAG_DESKTOP_MODE__ = true;" in inlined_html
    assert "<link rel=\"stylesheet\"" not in inlined_html
    assert "<script src=" not in inlined_html
    assert "<style>" in inlined_html
    assert "<script>" in inlined_html

    # Ensure no raw unescaped </script> inside <script> blocks (XSS breakout defense)
    # Every script block ending should strictly be the actual closing </script> tag
    script_blocks = re.findall(r"<script>(.*?)</script>", inlined_html, re.DOTALL)
    for block in script_blocks:
        assert "</script" not in block.lower(), "Raw </script tag found unescaped inside script block"

    # 3. Action wire-sync parity: Every action invoked in api.js must exist in bridge_schema.json
    api_js_path = static_dir / "api.js"
    assert api_js_path.exists()
    api_js_text = api_js_path.read_text(encoding="utf-8")

    # Match invokeBridge("action_name", ...)
    invoked_actions = set(re.findall(r'invokeBridge\s*\(\s*["\']([a-zA-Z0-9_]+)["\']', api_js_text))

    assert len(invoked_actions) >= 25, f"Non-vacuous floor violated: matched only {len(invoked_actions)} actions"
    for action in invoked_actions:
        assert action in valid_actions, f"Action '{action}' invoked in api.js does not exist in bridge_schema.json"


def test_old_rag_desktop_bridge_dispatch_and_genai_tool_schemas(tmp_path):
    """Exercise OldRAGDesktopBridge and OldRAGClient across required actions and verify tool schemas."""
    # Test that OldRAGClient is an alias of OldRAGDesktopBridge
    assert OldRAGClient is OldRAGDesktopBridge

    bridge = OldRAGDesktopBridge(pipe_name=r"\\.\pipe\NonExistentTestPipeDesktopBridge")

    # 1. Search action
    res_search = bridge.dispatch("search", {"query_text": "project update"})
    assert res_search["ok"] is True
    assert "results" in res_search["data"]

    # 2. Chat plugin actions
    res_plugins = bridge.dispatch("list_chat_plugins", {})
    assert res_plugins["ok"] is True
    assert "plugins" in res_plugins["data"]
    plugin_types = [p["adapter_type"] for p in res_plugins["data"]["plugins"]]
    assert "rocketchat" in plugin_types
    assert "google_chat" in plugin_types

    # Save chat account
    res_save_acc = bridge.dispatch("save_chat_account", {
        "account": {
            "id": "chat_acc_test_v3",
            "adapter_type": "rocketchat",
            "display_name": "Test RocketChat Account",
            "server_url": "https://chat.example.com",
            "username": "tester",
            "auth_token": "secret_token_123",
            "lookback_days": 14,
        }
    })
    assert res_save_acc["ok"] is True

    # List chat rooms
    res_rooms = bridge.dispatch("list_chat_rooms", {})
    assert res_rooms["ok"] is True
    assert "rooms" in res_rooms["data"]

    # 3. Calendar events
    res_cal = bridge.dispatch("list_calendar_events", {})
    assert res_cal["ok"] is True
    assert "events" in res_cal["data"]

    # 4. Quota governance actions
    res_quota_status = bridge.dispatch("get_quota_status", {"source_type": "EMAIL"})
    assert res_quota_status["ok"] is True
    assert "used_bytes" in res_quota_status["data"]

    res_preview = bridge.dispatch("preview_quota_eviction", {"source_type": "EMAIL"})
    assert res_preview["ok"] is True
    assert "candidates" in res_preview["data"]

    res_purge = bridge.dispatch("run_quota_purge", {"source_type": "EMAIL", "dry_run": True})
    assert res_purge["ok"] is True
    assert res_purge["data"]["dry_run"] is True

    # 5. Sync status
    res_sync = bridge.dispatch("get_sync_status", {})
    assert res_sync["ok"] is True
    assert "statuses" in res_sync["data"]

    # 6. Action schema reflection and GenAI tool schemas
    actions_meta = bridge.describe_actions()
    assert "actions" in actions_meta
    assert len(actions_meta["actions"]) >= 40

    genai_tools = bridge.get_genai_tool_definitions()
    assert len(genai_tools) >= 40
    tool_names = {t["function"]["name"] for t in genai_tools}
    assert "search" in tool_names
    assert "list_chat_plugins" in tool_names
    assert "get_quota_status" in tool_names
    assert "preview_quota_eviction" in tool_names
    assert "run_quota_purge" in tool_names

    # Check function tool schema structure matches standard OpenAI/Anthropic format
    search_tool = next(t for t in genai_tools if t["function"]["name"] == "search")
    assert search_tool["type"] == "function"
    assert "parameters" in search_tool["function"]
    assert "query_text" in search_tool["function"]["parameters"]["properties"]
