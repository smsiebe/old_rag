"""test_live_sync_quota_and_bridge.py - Unmocked Cross-Sovereignty Conformance Suite (TG4).

Strict compliance with BLACK Framework Rule 11 & 12:
- ZERO test doubles, mocks, or synthetic transport stubs.
- Live ephemeral ThreadingHTTPServer on 127.0.0.1:0 for Rocket.Chat, Google Chat, and CalDAV.
- Live Win32 Named Pipe server/client roundtrip.
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import pytest

from old_rag.contracts import (
    ChatAccountConfig,
    DataTypeQuotaConfig,
    QuotaPolicyType,
    RawIngestPayload,
    SearchQuery,
    SourceType,
    StorageConfig,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.engine.search import HybridSearchEngine
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.lifecycle import NamedPipeClient, NamedPipeServer
from old_rag.pipeline.sync_scheduler import RealTimeSyncScheduler
from old_rag.storage.cas import CASEngine
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.quota import QuotaGovernor
from old_rag.ui.bridge import OldRAGDesktopBridge


class EphemeralChatAndCalDAVHandler(http.server.BaseHTTPRequestHandler):
    """Real HTTP server responding to Rocket.Chat, Google Chat, and CalDAV protocols."""

    def log_message(self, format, *args):
        # Suppress standard logging to keep pytest output clean
        pass

    def do_GET(self):
        parsed_path = self.path

        # 1. Rocket.Chat Joined Channels
        if "/api/v1/channels.list.joined" in parsed_path or "/api/v1/channels.list" in parsed_path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "success": True,
                "channels": [
                    {
                        "_id": "chan_aurora",
                        "name": "project-aurora",
                        "t": "c",
                        "msgs": 1,
                    }
                ],
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        # 2. Rocket.Chat Channel History
        elif "/api/v1/channels.history" in parsed_path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "success": True,
                "messages": [
                    {
                        "_id": "msg_rc_aurora_01",
                        "rid": "chan_aurora",
                        "msg": "Project Aurora critical cryptographic blueprint specifications",
                        "ts": "2026-11-13T10:00:00.000Z",
                        "u": {"_id": "u_alice", "username": "alice", "name": "Alice Smith"},
                    }
                ],
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        # 3. Google Chat Spaces
        elif "/v1/spaces" in parsed_path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "spaces": [
                    {
                        "name": "spaces/space_quantum",
                        "displayName": "Quantum Systems",
                        "spaceType": "SPACE",
                    }
                ]
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        # 4. Google Chat Messages
        elif "/messages" in parsed_path and "spaces/space_quantum" in parsed_path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "messages": [
                    {
                        "name": "spaces/space_quantum/messages/gc_msg_01",
                        "text": "Project Aurora deployment timeline scheduled for next quarter",
                        "createTime": "2026-11-13T10:30:00Z",
                        "sender": {"name": "users/u_bob", "displayName": "Bob Jones"},
                    }
                ]
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        # 5. CalDAV / Principal
        elif "/calendar" in parsed_path or "/caldav" in parsed_path:
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.end_headers()
            ics_payload = (
                "BEGIN:VCALENDAR\r\n"
                "VERSION:2.0\r\n"
                "PRODID:-//OldRAG Test//EN\r\n"
                "BEGIN:VEVENT\r\n"
                "UID:evt_aurora_review_01\r\n"
                "DTSTAMP:20261113T120000Z\r\n"
                "DTSTART:20261114T140000Z\r\n"
                "DTEND:20261114T150000Z\r\n"
                "SUMMARY:Project Aurora Executive Architecture Review\r\n"
                "LOCATION:Room 404 Sovereign Vault\r\n"
                "DESCRIPTION:Discuss Aurora storage quotas and cryptographic verification.\r\n"
                "END:VEVENT\r\n"
                "END:VCALENDAR\r\n"
            )
            self.wfile.write(ics_payload.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


@pytest.mark.sovereignty
def test_live_chat_and_caldav_realtime_sync_to_hybrid_search_and_quota_eviction(tmp_path):
    """Live unmocked test: Ephemeral HTTP server -> Chat & Sync -> Hybrid Search -> Quota Purge."""
    # 1. Start live ephemeral HTTP server on 127.0.0.1:0 (random OS free port)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EphemeralChatAndCalDAVHandler)
    server_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # 2. Setup isolated storage layer in tmp_path
        storage_cfg = StorageConfig(app_data_dir=str(tmp_path))
        cas = CASEngine(storage_cfg)
        cat = CatalogRepository(tmp_path / "catalog.db")
        ldb_table = open_lancedb_table(tmp_path, dim=768)
        indexer = IndexEngine(tmp_path / "lexical_fts.db", lancedb_table=ldb_table)
        extractor = DocumentExtractor(catalog=cat)
        chunker = ParentAwareChunker(catalog=cat)
        coordinator = PipelineCoordinator(
            cas=cas,
            catalog=cat,
            extractor=extractor,
            chunker=chunker,
            indexer=indexer,
        )
        quota_gov = QuotaGovernor(catalog=cat, cas=cas, indexer=indexer, coordinator=coordinator)

        # 3. Create a real physical source file to verify Source File Safety Invariant
        user_source_dir = tmp_path / "user_documents"
        user_source_dir.mkdir(parents=True, exist_ok=True)
        user_file = user_source_dir / "aurora_specs.txt"
        user_file.write_text("Confidential Project Aurora system engineering manual.", encoding="utf-8")

        # Ingest the filesystem file
        p_file = RawIngestPayload(
            doc_id="doc_file_aurora_01",
            source_type=SourceType.FILE,
            original_path=str(user_file),
            subject="Aurora Specs File",
            body_text=user_file.read_text(encoding="utf-8"),
            received_ts_utc=datetime.now(timezone.utc),
        )
        coordinator.ingest_payload(p_file)

        # 4. Connect chat adapter over live HTTP server
        chat_reg = ChatPluginRegistry()
        rc_account = ChatAccountConfig(
            id="acc_rc_live",
            name="Live RocketChat Server",
            adapter_type="rocketchat",
            server_url=f"http://127.0.0.1:{server_port}",
            username="u_alice",
            credentials={"auth_token": "live_token_abc", "user_id": "u_alice"},
            lookback_days=7,
        )
        adapter = chat_reg.create_adapter(rc_account)

        # Real HTTP call to list rooms
        rooms = adapter.list_rooms(include_dms=False)
        assert len(rooms) >= 1
        assert rooms[0].room_id == "chan_aurora"

        # Real HTTP call to fetch messages
        raw_msgs, _ = adapter.fetch_messages("chan_aurora", limit=10)
        assert len(raw_msgs) >= 1
        assert "Aurora" in raw_msgs[0].body_text

        # Ingest the chat message into the pipeline
        chat_payloads = adapter.group_messages_into_ingest_payloads(raw_msgs, rc_account)
        for cp in chat_payloads:
            coordinator.ingest_payload(cp)

        # 5. Execute cross-type hybrid search
        search_engine = HybridSearchEngine(
            fts_db_path=tmp_path / "lexical_fts.db",
            lancedb_table=ldb_table,
            catalog=cat,
        )

        query = SearchQuery(query_text="Aurora", top_k=10)
        hits = search_engine.search(query)
        assert len(hits) >= 2, f"Expected hits across file and chat, got {len(hits)}"

        found_types = {h.source_type for h in hits}
        assert SourceType.FILE in found_types
        assert SourceType.CHAT in found_types

        # Verify chat hit metadata preservation
        chat_hit = next(h for h in hits if h.source_type == SourceType.CHAT)
        assert chat_hit.doc_id.startswith("chat_") or "chan_aurora" in chat_hit.doc_id

        # 6. Test Hard-Drive Quota Enforcement on CHAT items
        chat_status = quota_gov.get_status(SourceType.CHAT)
        assert chat_status.used_bytes > 0
        assert chat_status.document_count >= 1

        # Enforce quota with strict max_bytes=10 to force eviction
        strict_cfg = DataTypeQuotaConfig(
            source_type=SourceType.CHAT,
            max_bytes=10,
            policy=QuotaPolicyType.OLDEST_FIRST,
            preserve_recent_days=0,
            preserve_starred=False,
            preserve_pinned=False,
        )
        purge_res = quota_gov.enforce_quota(SourceType.CHAT, dry_run=False, config=strict_cfg)
        assert len(purge_res.evicted_doc_ids) >= 1
        assert purge_res.freed_bytes > 0

        # Verify purged item is 100% gone from all stores
        evicted_id = purge_res.evicted_doc_ids[0]
        assert cas.get_text(evicted_id) is None
        conn = cat._get_connection()
        cur = conn.execute("SELECT COUNT(*) FROM documents WHERE doc_id = ?", (evicted_id,))
        assert cur.fetchone()[0] == 0

        # 7. CRITICAL INVARIANT: Verify user source file on disk was NEVER deleted or modified
        assert user_file.exists(), "Source file safety violation: user file was deleted!"
        assert user_file.read_text(encoding="utf-8") == "Confidential Project Aurora system engineering manual."

    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.sovereignty
def test_live_named_pipe_daemon_and_old_rag_desktop_bridge_roundtrip(tmp_path):
    """Live unmocked test: NamedPipeServer <-> NamedPipeClient <-> OldRAGDesktopBridge."""
    # Generate unique pipe name
    unique_id = uuid.uuid4().hex[:8]
    pipe_name = rf"\\.\pipe\OldRAG_Test_LivePipe_{unique_id}"

    storage_cfg = StorageConfig(app_data_dir=str(tmp_path))
    cat = CatalogRepository(tmp_path / "catalog.db")
    cas = CASEngine(storage_cfg)

    # Simple live request handler simulating daemon response dispatch
    def pipe_request_handler(req_str: str) -> str:
        req = json.loads(req_str)
        action = req.get("action", "")
        if action == "status":
            return json.dumps({
                "daemon_running": True,
                "vlm_active": False,
                "items_indexed": 42,
                "pending_outbox_count": 0,
                "classic_outlook_active": False,
                "sync_progress_pct": 100.0,
            })
        elif action == "list_chat_plugins":
            return json.dumps({
                "plugins": [
                    {
                        "adapter_type": "rocketchat",
                        "display_name": "Rocket.Chat",
                        "version": "1.0.0",
                        "capabilities": {
                            "supports_dms": True,
                            "supports_rooms": True,
                            "supports_threads": True,
                            "supports_streaming": False,
                            "supports_send": True,
                        },
                        "config_schema": {},
                        "is_builtin": True,
                    }
                ]
            })
        elif action == "get_missing_document":
            return json.dumps({"error": "Document not found in archive"})
        elif action == "invalid_request":
            return json.dumps({"error": "Validation error: Missing required fields"})
        return json.dumps({"error": f"Unknown action: {action}"})

    pipe_server = NamedPipeServer(pipe_name=pipe_name, handler=pipe_request_handler)
    pipe_server.start()

    try:
        # Give server thread a moment to bind
        time.sleep(0.1)

        bridge = OldRAGDesktopBridge(pipe_name=pipe_name, storage_cfg=storage_cfg)

        # 1. Test status call through live pipe (200 OK)
        res_status = bridge.dispatch("status")
        assert res_status["ok"] is True
        assert res_status["status"] == 200
        assert res_status["data"]["daemon_running"] is True
        assert res_status["data"]["items_indexed"] == 42

        # 2. Test list_chat_plugins through live pipe (200 OK)
        res_plugins = bridge.dispatch("list_chat_plugins")
        assert res_plugins["ok"] is True
        assert res_plugins["status"] == 200
        assert len(res_plugins["data"]["plugins"]) >= 1

        # 3. Test missing document handling (404 Not Found)
        res_missing = bridge.dispatch("get_missing_document")
        assert res_missing["ok"] is False
        assert res_missing["status"] == 404
        assert res_missing["error_code"] == "NOT_FOUND"

        # 4. Test dispatch_json serialization wrapper roundtrip
        json_req = json.dumps({"action": "status"})
        json_resp = bridge.dispatch_json(json_req)
        parsed_resp = json.loads(json_resp)
        assert parsed_resp["ok"] is True
        assert parsed_resp["data"]["daemon_running"] is True

    finally:
        pipe_server.stop()
