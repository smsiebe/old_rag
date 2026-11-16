"""test_phase0_contracts_v3.py - Phase 0 contract and schema v3 validation tests."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pytest

from old_rag.contracts import (
    BridgeErrorCode,
    BridgeResponseEnvelope,
    CalendarEvent,
    CalendarIngestMeta,
    ChatAccountConfig,
    ChatAdapterCapability,
    ChatIngestMeta,
    ChatMessageItem,
    ChatPluginDescriptor,
    ChatRoomSummary,
    DataTypeQuotaConfig,
    DataTypeQuotaStatus,
    EvictionCandidateSummary,
    OutgoingChatMessage,
    QuotaPolicyType,
    QuotaPurgeResult,
    RelevanceTier,
    SearchResult,
    SourceType,
    SyncCursorRecord,
    SyncPolicyConfig,
    UIEventDTO,
    safe_json_dumps,
)
from old_rag.schema import (
    CATALOG_SCHEMA_DDL,
    CURRENT_SCHEMA_VERSION,
    FTS_SCHEMA_DDL,
)


def test_phase0_source_type_v3_and_dto_roundtrip():
    """Verify SourceType enum values and JSON round-tripping for all v3 DTOs."""
    assert SourceType.EMAIL == "EMAIL"
    assert SourceType.FILE == "FILE"
    assert SourceType.CALENDAR == "CALENDAR"
    assert SourceType.CHAT == "CHAT"

    # CalendarEvent defaults to SourceType.CALENDAR
    cal_event = CalendarEvent(
        event_id="evt_001",
        start_ts_utc=datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc),
        end_ts_utc=datetime(2026, 11, 14, 11, 0, tzinfo=timezone.utc),
        subject="Sprint Planning",
    )
    assert cal_event.source_type == SourceType.CALENDAR
    assert cal_event.doc_id is None

    # ChatAccountConfig roundtrip
    account = ChatAccountConfig(
        id="acc_teams_1",
        name="Engineering Teams",
        adapter_type="teams",
        server_url="https://graph.microsoft.com/v1.0",
        credentials={"tenant_id": "test-tenant", "client_id": "test-client"},
        lookback_days=45,
        rooms=["general", "dev"],
    )
    raw_json = safe_json_dumps(account)
    reloaded_account = ChatAccountConfig.model_validate_json(raw_json)
    assert reloaded_account.id == account.id
    assert reloaded_account.lookback_days == 45
    assert reloaded_account.rooms == ["general", "dev"]

    # ChatMessageItem roundtrip
    msg = ChatMessageItem(
        message_id="msg_101",
        doc_id="doc_chat_101",
        account_id="acc_teams_1",
        adapter_type="teams",
        room_id="room_dev",
        room_name="Development",
        is_dm=False,
        sender_id="usr_alice",
        sender_name="Alice Smith",
        sent_ts_utc=datetime.now(timezone.utc),
        body_text="Hello world from v3 chat ingest",
        reactions={"👍": ["usr_bob"]},
    )
    msg_json = safe_json_dumps(msg)
    reloaded_msg = ChatMessageItem.model_validate_json(msg_json)
    assert reloaded_msg.message_id == "msg_101"
    assert reloaded_msg.reactions["👍"] == ["usr_bob"]

    # DataTypeQuotaConfig and QuotaPurgeResult roundtrip
    quota_cfg = DataTypeQuotaConfig(
        source_type=SourceType.CHAT,
        max_bytes=100 * 1024 * 1024,
        policy=QuotaPolicyType.COMPOSITE_SCORE,
        preserve_starred=True,
        preserve_pinned=True,
        preserve_recent_days=7,
    )
    assert quota_cfg.source_type == SourceType.CHAT

    purge_res = QuotaPurgeResult(
        source_type=SourceType.CHAT,
        dry_run=True,
        evicted_doc_ids=["doc_1", "doc_2"],
        freed_bytes=2048,
        used_bytes_before=10000,
        used_bytes_after=7952,
        candidates=[
            EvictionCandidateSummary(
                doc_id="doc_1",
                source_type=SourceType.CHAT,
                byte_size=1024,
                estimated_freed_bytes=1024,
                retention_score=0.15,
                reason="LRU score below threshold",
            )
        ],
    )
    purge_json = safe_json_dumps(purge_res)
    reloaded_purge = QuotaPurgeResult.model_validate_json(purge_json)
    assert len(reloaded_purge.candidates) == 1
    assert reloaded_purge.freed_bytes == 2048

    # BridgeResponseEnvelope roundtrip
    env = BridgeResponseEnvelope(
        ok=True,
        status=200,
        data={"results": [msg.model_dump()]},
        error=None,
        error_code=BridgeErrorCode.OK,
    )
    env_json = safe_json_dumps(env)
    reloaded_env = BridgeResponseEnvelope.model_validate_json(env_json)
    assert reloaded_env.ok is True
    assert reloaded_env.error_code == BridgeErrorCode.OK


def test_phase0_bridge_schema_v3_backward_and_forward_compat():
    """Verify bridge_schema.json parses, retains backward compat, and declares v3 actions."""
    schema_path = Path(__file__).parent.parent.parent / "src" / "old_rag" / "bridge_schema.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Required field verification for SearchResultItem (Rule 11 & 12 backward compat)
    item_reqs = schema["definitions"]["SearchResultItem"]["required"]
    for expected_field in [
        "chunk_id",
        "doc_id",
        "content",
        "source_type",
        "rrf_score",
        "normalized_rrf",
        "relevance_tier",
    ]:
        assert expected_field in item_reqs

    # Enum checks
    st_enum = schema["definitions"]["SearchResultItem"]["properties"]["source_type"]["enum"]
    assert "CALENDAR" in st_enum
    assert "CHAT" in st_enum
    assert "EMAIL" in st_enum
    assert "FILE" in st_enum

    # v3 Definitions check
    for def_name in [
        "BridgeResponseEnvelope",
        "ChatPluginDescriptor",
        "ChatAccountItem",
        "ChatRoomSummary",
        "ChatMessageItem",
        "SendChatMessageRequest",
        "SyncStatusResponse",
        "TriggerSyncRequest",
        "DataTypeQuotaConfig",
        "DataTypeQuotaStatus",
        "QuotaPurgeRequest",
        "QuotaPurgeResult",
        "PollEventsResponse",
    ]:
        assert def_name in schema["definitions"], f"Missing definition {def_name} in bridge_schema.json"

    # Actions check
    actions = schema.get("actions", {})
    assert len(actions) >= 25, f"Expected at least 25 actions in bridge_schema.json, got {len(actions)}"
    assert "search" in actions
    assert "list_chat_rooms" in actions
    assert "list_chat_messages" in actions
    assert "send_chat_message" in actions
    assert "get_quota_status" in actions
    assert "run_quota_purge" in actions
    assert "get_sync_status" in actions
    assert "trigger_sync" in actions
    assert "poll_events" in actions


def test_phase0_schema_v3_ddl_executes_cleanly():
    """Execute CATALOG_SCHEMA_DDL and FTS_SCHEMA_DDL on clean in-memory SQLite with foreign keys ON."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(CATALOG_SCHEMA_DDL)
    conn.executescript(FTS_SCHEMA_DDL)

    assert CURRENT_SCHEMA_VERSION == 3

    # Check documents table columns
    doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "account_id" in doc_cols
    assert "byte_size" in doc_cols
    assert "last_accessed_utc" in doc_cols
    assert "access_count" in doc_cols
    assert "is_pinned" in doc_cols
    assert "is_starred" in doc_cols
    assert "is_read" in doc_cols
    assert "chunk_ids_json" in doc_cols

    # Check newly added tables exist
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "chat_rooms" in tables
    assert "chat_messages" in tables
    assert "sync_cursors" in tables
    assert "quota_policies" in tables
    assert "calendar_events" in tables

    # Verify ON DELETE CASCADE foreign key relationship between documents and chat_messages
    conn.execute(
        """
        INSERT INTO documents (doc_id, source_type, original_path, ingested_at_utc)
        VALUES ('doc_test_1', 'CHAT', 'virtual/teams', '2026-11-14T00:00:00Z');
        """
    )
    conn.execute(
        """
        INSERT INTO chat_messages (message_id, doc_id, account_id, adapter_type, room_id, sent_ts_utc, body_text)
        VALUES ('msg_test_1', 'doc_test_1', 'acc_1', 'teams', 'room_1', '2026-11-14T00:00:00Z', 'Test message');
        """
    )
    conn.commit()

    # Deleting document should cascade delete chat_messages
    conn.execute("DELETE FROM documents WHERE doc_id = 'doc_test_1';")
    conn.commit()

    remaining_msgs = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE doc_id = 'doc_test_1'").fetchone()[0]
    assert remaining_msgs == 0

    conn.close()
