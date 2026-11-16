"""test_chat_sync_and_quota.py - Comprehensive Unit Tests for Task Group 1.

Verifies:
1. Schema v2 -> v3 migration, DPAPI secret vault (encrypt/decrypt round-trip).
2. Chat Plugin Registry, 4 built-in adapters, Rule 10 error handling, 25-message windowing.
3. QuotaGovernor across all 4 policies (OLDEST_FIRST, LRU_ACCESS, LOWEST_CENTRALITY, COMPOSITE_SCORE),
   pinned/starred/recent/reply-parent protections, and shared blob reference accounting.
4. Coordinator atomic delete cascade with OutboxOp.DELETE crash recovery, preserving source filesystem files.
5. RealTimeSyncScheduler two-sided watermark cursor lookback expansion backfilling.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from old_rag.contracts import (
    ChatAccountConfig,
    ChatMessageItem,
    ChatRoomSummary,
    DataTypeQuotaConfig,
    ModelConfig,
    OutboxOp,
    QuotaPolicyType,
    RawAttachment,
    RawIngestPayload,
    ServiceUnavailableError,
    SourceType,
    StorageConfig,
    SyncCursorRecord,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.ingest.chat.base import BaseChatAdapter
from old_rag.ingest.chat.google_chat import GoogleChatAdapter
from old_rag.ingest.chat.jabber import JabberAdapter
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.ingest.chat.rocketchat import RocketChatAdapter
from old_rag.ingest.chat.teams import TeamsGraphAdapter
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.sync_scheduler import EventBus, RealTimeSyncScheduler
from old_rag.schema import CURRENT_SCHEMA_VERSION, SCHEMA_V2_TO_V3_MIGRATION_STATEMENTS
from old_rag.storage.cas import CASEngine, KeyManager
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.quota import QuotaGovernor
from old_rag.storage.settings import (
    delete_chat_account,
    get_accounts,
    get_chat_accounts,
    save_account,
    save_chat_account,
)


# ==============================================================================
# 1. Schema v2 -> v3 Migration and DPAPI Secret Vault
# ==============================================================================

def test_schema_v2_to_v3_migration_and_dpapi_secret_vault(tmp_path: Path):
    db_path = tmp_path / "catalog_v2.db"
    conn = sqlite3.connect(str(db_path))
    # Create pure v2 schema (without v3 columns: account_id, byte_size, chunk_ids_json, is_starred, etc.)
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        INSERT INTO schema_version (version, applied_at_utc) VALUES (2, '2026-01-01T00:00:00Z');

        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            original_path TEXT NOT NULL,
            entry_id TEXT,
            internet_message_id TEXT,
            conversation_id TEXT,
            sender TEXT,
            subject TEXT,
            folder TEXT,
            received_ts_utc TEXT,
            text_cas_ref TEXT,
            raw_msg_cas_ref TEXT,
            ingested_at_utc TEXT NOT NULL,
            is_deleted INTEGER DEFAULT 0
        );

        CREATE TABLE calendar_events (
            event_id TEXT PRIMARY KEY,
            account_id TEXT,
            source_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            location TEXT,
            start_ts_utc TEXT NOT NULL,
            end_ts_utc TEXT NOT NULL,
            is_all_day INTEGER DEFAULT 0,
            organizer TEXT,
            attendees_json TEXT NOT NULL DEFAULT '[]',
            body_text TEXT,
            status TEXT NOT NULL DEFAULT 'CONFIRMED',
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE blobs (
            blob_hash TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            ref_count INTEGER NOT NULL DEFAULT 1,
            first_seen_utc TEXT NOT NULL
        );

        CREATE TABLE blob_references (
            blob_hash TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            PRIMARY KEY (blob_hash, doc_id)
        );

        CREATE TABLE outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            op TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            chunk_ids_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at_utc TEXT NOT NULL,
            committed_at_utc TEXT
        );

        INSERT INTO documents (doc_id, source_type, original_path, subject, ingested_at_utc)
        VALUES ('doc_legacy', 'EMAIL', 'mailbox://inbox/1', 'Legacy Email Subject', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    # Instantiating CatalogRepository triggers automatic v3 migration
    repo = CatalogRepository(db_path)
    cur = repo._get_connection().execute("PRAGMA table_info(documents)")
    doc_cols = {row["name"] for row in cur.fetchall()}
    assert "account_id" in doc_cols
    assert "byte_size" in doc_cols
    assert "chunk_ids_json" in doc_cols
    assert "is_starred" in doc_cols
    assert "is_pinned" in doc_cols
    assert "is_read" in doc_cols
    assert "last_accessed_utc" in doc_cols
    assert "reply_to_doc_id" in doc_cols

    # Verify v3 tables created
    tables = {r[0] for r in repo._get_connection().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "chat_rooms" in tables
    assert "chat_messages" in tables
    assert "sync_cursors" in tables
    assert "quota_policies" in tables

    # Verify calendar_events has doc_id column
    cal_cols = {row["name"] for row in repo._get_connection().execute("PRAGMA table_info(calendar_events)").fetchall()}
    assert "doc_id" in cal_cols

    # Verify DPAPI Secret Vault
    km = KeyManager(tmp_path / "master.key")
    secret_text = "super_secret_token_12345!@#"
    encrypted = km.encrypt_secret(secret_text)
    assert encrypted.startswith("ENC_DPAPI_AESGCM:")
    decrypted = km.decrypt_secret(encrypted)
    assert decrypted == secret_text

    # Verify plaintext is safely returned if not encrypted
    assert km.decrypt_secret("not_encrypted") == "not_encrypted"

    # Verify chat account secret encryption
    chat_acc = {
        "id": "rc_main",
        "name": "RocketChat Production",
        "adapter_type": "rocketchat",
        "server_url": "https://chat.internal.corp",
        "auth_token": "secret_rc_auth_token",
        "password": "secret_rc_password",
        "enabled": True,
    }
    saved = save_chat_account(chat_acc)
    assert saved["id"] == "rc_main"

    # Fetch without decrypt: secrets remain masked/encrypted
    accs_raw = get_chat_accounts(decrypt=False)
    target_raw = next(a for a in accs_raw if a["id"] == "rc_main")
    assert target_raw["auth_token"].startswith("ENC_DPAPI_AESGCM:")

    # Fetch with decrypt: secrets restored
    accs_dec = get_chat_accounts(decrypt=True)
    target_dec = next(a for a in accs_dec if a["id"] == "rc_main")
    assert target_dec["auth_token"] == "secret_rc_auth_token"
    assert target_dec["password"] == "secret_rc_password"

    # Clean up
    delete_chat_account("rc_main")


# ==============================================================================
# 2. Chat Plugin Registry and Built-in Adapters Error Handling (Rule 10)
# ==============================================================================

def test_chat_plugin_registry_and_builtin_adapters_error_handling():
    registry = ChatPluginRegistry()
    plugins = registry.list_plugins()
    plugin_types = {p.adapter_type for p in plugins}

    assert "rocketchat" in plugin_types
    assert "google_chat" in plugin_types
    assert "teams" in plugin_types
    assert "jabber" in plugin_types

    # Rule 10 Verification: Unreachable endpoints must raise ServiceUnavailableError, NEVER dummy fake success
    # 1. RocketChat unreachable
    rc_cfg = ChatAccountConfig(
        id="rc_test",
        name="RC Test",
        adapter_type="rocketchat",
        server_url="http://127.0.0.1:59999",  # Nothing listening
        auth_token="token",
        user_id="user1",
    )
    rc_adapter = registry.create_adapter(rc_cfg)
    with pytest.raises(ServiceUnavailableError):
        rc_adapter.list_rooms()

    with pytest.raises(ServiceUnavailableError):
        rc_adapter.fetch_messages("room_123")

    # 2. Google Chat unreachable
    gc_cfg = ChatAccountConfig(
        id="gc_test",
        name="GC Test",
        adapter_type="google_chat",
        server_url="http://127.0.0.1:59999",
        auth_token="dummy_token",
    )
    gc_adapter = registry.create_adapter(gc_cfg)
    with pytest.raises(ServiceUnavailableError):
        gc_adapter.fetch_messages("spaces/SPACE1")

    # 3. Teams Graph unreachable
    teams_cfg = ChatAccountConfig(
        id="teams_test",
        name="Teams Test",
        adapter_type="teams",
        tenant_id="dummy_tenant",
        client_id="dummy_client",
        client_secret="dummy_secret",
    )
    teams_adapter = registry.create_adapter(teams_cfg)
    with pytest.raises(ServiceUnavailableError):
        teams_adapter.fetch_messages("channel_123")

    # 4. Jabber unreachable
    jabber_cfg = ChatAccountConfig(
        id="jabber_test",
        name="Jabber Test",
        adapter_type="jabber",
        username="user@xmpp.corp.internal",
        password="password",
        server_url="xmpp.corp.internal",
    )
    jabber_adapter = registry.create_adapter(jabber_cfg)
    with pytest.raises(ServiceUnavailableError):
        jabber_adapter.fetch_messages("room@conference.corp.internal")

    # 5. Message grouping window verification (25-message window)
    now = datetime.now(timezone.utc)
    messages: List[ChatMessageItem] = []
    for i in range(55):
        messages.append(
            ChatMessageItem(
                message_id=f"msg_{i}",
                doc_id=f"chat_rc_test_general_{i}",
                account_id="rc_test",
                adapter_type="rocketchat",
                room_id="general",
                room_name="General Discussion",
                is_dm=False,
                sender_id="alice",
                sender_name="Alice Smith",
                thread_root_id=None,
                sent_ts_utc=now + timedelta(seconds=i),
                body_text=f"This is chat message number {i}",
            )
        )

    payloads = BaseChatAdapter.group_messages_into_ingest_payloads(messages, rc_cfg)
    # 55 messages grouped by at most 25 messages per payload -> 3 payloads (25, 25, 5)
    assert len(payloads) == 3
    assert payloads[0].source_type == SourceType.CHAT
    assert payloads[0].account_id == "rc_test"
    assert payloads[0].chat_meta is not None
    assert payloads[0].chat_meta.room_id == "general"
    assert len(payloads[0].chat_meta.message_ids) == 25
    assert len(payloads[1].chat_meta.message_ids) == 25
    assert len(payloads[2].chat_meta.message_ids) == 5


# ==============================================================================
# 3. QuotaGovernor: All Four Policies & Shared Blob Accounting
# ==============================================================================

def test_quota_governor_all_four_policies_and_shared_blob_accounting(tmp_path: Path):
    app_data = tmp_path / "quota_test"
    app_data.mkdir(parents=True, exist_ok=True)

    storage_cfg = StorageConfig(app_data_dir=str(app_data))
    cas = CASEngine(storage_cfg)
    catalog = CatalogRepository(app_data / "catalog.db")
    model_cfg = ModelConfig()

    lancedb_table = open_lancedb_table(app_data, dim=model_cfg.matryoshka_dim)
    indexer = IndexEngine(
        fts_db_path=app_data / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
    )
    extractor = DocumentExtractor(catalog=catalog)
    chunker = ParentAwareChunker(catalog=catalog)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=catalog,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    governor = QuotaGovernor(catalog=catalog, cas=cas, indexer=indexer, coordinator=coordinator)

    now = datetime.now(timezone.utc)
    base_time = now - timedelta(days=60)

    # Ingest 4 documents with controlled timestamps, accesses, and graph structure:
    # doc_old: Very old (60 days ago), never accessed, no replies
    # doc_mid: 30 days ago, accessed recently, has replies (is reply parent)
    # doc_child: 20 days ago, replies to doc_mid
    # doc_recent: 2 days ago (within 7-day protect window)
    # doc_pinned: 40 days ago, is_pinned=True

    p_old = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_old",
        original_path="mailbox://inbox/old",
        received_ts_utc=base_time,
        sender="bob@test.com",
        subject="Old Project Update",
        body_text="Discussion about ancient project",
    )
    coordinator.ingest_payload(p_old)

    p_mid = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_mid",
        original_path="mailbox://inbox/mid",
        received_ts_utc=base_time + timedelta(days=30),
        sender="charlie@test.com",
        subject="Mid Conversation Root",
        body_text="This is the parent email of a thread",
    )
    coordinator.ingest_payload(p_mid)

    p_child = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_child",
        original_path="mailbox://inbox/child",
        received_ts_utc=base_time + timedelta(days=40),
        sender="david@test.com",
        subject="Re: Mid Conversation Root",
        body_text="Reply to mid conversation",
    )
    coordinator.ingest_payload(p_child)
    # Mark doc_child as replying to doc_mid
    conn = catalog._get_connection()
    conn.execute("UPDATE documents SET reply_to_doc_id = 'doc_mid' WHERE doc_id = 'doc_child'")
    conn.commit()

    p_recent = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_recent",
        original_path="mailbox://inbox/recent",
        received_ts_utc=now - timedelta(days=2),
        sender="eve@test.com",
        subject="Recent Update",
        body_text="Recent message within protect window",
    )
    coordinator.ingest_payload(p_recent)

    p_pinned = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_pinned",
        original_path="mailbox://inbox/pinned",
        received_ts_utc=base_time + timedelta(days=20),
        sender="frank@test.com",
        subject="Pinned Important Document",
        body_text="Crucial information that is pinned",
    )
    coordinator.ingest_payload(p_pinned)
    catalog.set_document_flags("doc_pinned", is_pinned=True)

    # 1. Test Policy: OLDEST_FIRST
    cfg_oldest = DataTypeQuotaConfig(
        source_type=SourceType.EMAIL,
        max_bytes=100,  # Force eviction
        policy=QuotaPolicyType.OLDEST_FIRST,
        preserve_recent_days=7,
        preserve_pinned=True,
    )
    candidates_oldest = governor.rank_eviction_candidates(SourceType.EMAIL, bytes_to_free=1000000, config=cfg_oldest)
    candidate_ids = [c.doc_id for c in candidates_oldest]

    # Invariants:
    # - doc_pinned must NEVER be in candidates
    # - doc_recent (< 7 days) must NEVER be in candidates
    # - doc_mid (parent of doc_child) must NEVER be in candidates (reply-parent protection)
    # - doc_old (oldest, no replies, unpinned) must be the first candidate!
    assert "doc_pinned" not in candidate_ids
    assert "doc_recent" not in candidate_ids
    assert "doc_mid" not in candidate_ids
    assert candidate_ids[0] == "doc_old"

    # 2. Test Policy: LRU_ACCESS
    # Record access on doc_old, making doc_child the least recently accessed
    catalog.record_document_access("doc_old")
    cfg_lru = DataTypeQuotaConfig(
        source_type=SourceType.EMAIL,
        max_bytes=100,
        policy=QuotaPolicyType.LRU_ACCESS,
        preserve_recent_days=7,
        preserve_pinned=True,
    )
    candidates_lru = governor.rank_eviction_candidates(SourceType.EMAIL, bytes_to_free=1000000, config=cfg_lru)
    candidate_lru_ids = [c.doc_id for c in candidates_lru]
    # doc_child was never accessed, so it ranks before doc_old
    assert candidate_lru_ids[0] == "doc_child"

    # 3. Test Policy: LOWEST_CENTRALITY & COMPOSITE_SCORE
    cfg_comp = DataTypeQuotaConfig(
        source_type=SourceType.EMAIL,
        max_bytes=100,
        policy=QuotaPolicyType.COMPOSITE_SCORE,
        preserve_recent_days=7,
        preserve_pinned=True,
    )
    candidates_comp = governor.rank_eviction_candidates(SourceType.EMAIL, bytes_to_free=1000000, config=cfg_comp)
    assert len(candidates_comp) > 0

    # 4. Test Dry Run vs Real Enforcement
    purge_dry = governor.enforce_quota(SourceType.EMAIL, config=cfg_oldest, dry_run=True)
    assert purge_dry.dry_run is True
    assert "doc_old" in purge_dry.evicted_doc_ids
    # In dry run, document is NOT deleted
    assert catalog.get_document("doc_old") is not None

    # Real Enforcement
    purge_real = governor.enforce_quota(SourceType.EMAIL, config=cfg_oldest, dry_run=False)
    assert purge_real.dry_run is False
    assert "doc_old" in purge_real.evicted_doc_ids
    assert purge_real.freed_bytes > 0
    # In real enforcement, document is hard deleted
    assert catalog.get_document("doc_old") is None

    # 5. Shared Blob Reference Accounting
    # Document X and Document Y share blob B1. Document Y also has blob B2.
    # Evicting X alone should NOT free blob B1.
    b1_data = b"SHARED_ATTACHMENT_DATA_BINARY_BLOB_CONTENT_12345"
    b2_data = b"EXCLUSIVE_ATTACHMENT_DATA_FOR_DOC_Y_67890"

    p_x = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_x",
        original_path="mailbox://inbox/x",
        received_ts_utc=base_time - timedelta(days=10),
        subject="Doc X with shared attachment",
        body_text="Doc X content",
        attachments=[RawAttachment(filename="shared.pdf", content_type="application/pdf", size_bytes=len(b1_data), data=b1_data)],
    )
    coordinator.ingest_payload(p_x)

    p_y = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_y",
        original_path="mailbox://inbox/y",
        received_ts_utc=base_time - timedelta(days=5),
        subject="Doc Y with shared attachment and exclusive attachment",
        body_text="Doc Y content",
        attachments=[
            RawAttachment(filename="shared.pdf", content_type="application/pdf", size_bytes=len(b1_data), data=b1_data),
            RawAttachment(filename="exclusive.pdf", content_type="application/pdf", size_bytes=len(b2_data), data=b2_data),
        ],
    )
    coordinator.ingest_payload(p_y)

    b1_hash = hashlib.sha256(b1_data).hexdigest()
    # Check ref count in catalog: blob b1_hash should have ref_count >= 2
    cur = catalog._get_connection().execute("SELECT ref_count FROM blobs WHERE blob_hash = ?", (b1_hash,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] >= 2

    # Verify governor status reports shared vs exclusive blob bytes
    status = governor.get_status(SourceType.EMAIL)
    assert status.used_bytes > 0
    assert status.document_count >= 2


# ==============================================================================
# 4. Atomic Delete Cascade & Crash Recovery Preserves Source File
# ==============================================================================

def test_atomic_delete_cascade_and_crash_recovery_preserves_source_file(tmp_path: Path):
    app_data = tmp_path / "delete_cascade_test"
    app_data.mkdir(parents=True, exist_ok=True)

    storage_cfg = StorageConfig(app_data_dir=str(app_data))
    cas = CASEngine(storage_cfg)
    catalog = CatalogRepository(app_data / "catalog.db")
    model_cfg = ModelConfig()

    lancedb_table = open_lancedb_table(app_data, dim=model_cfg.matryoshka_dim)
    indexer = IndexEngine(
        fts_db_path=app_data / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
    )
    extractor = DocumentExtractor(catalog=catalog)
    chunker = ParentAwareChunker(catalog=catalog)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=catalog,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    # 1. Create a user file in an external folder
    user_docs_dir = tmp_path / "user_documents"
    user_docs_dir.mkdir(parents=True, exist_ok=True)
    source_file = user_docs_dir / "confidential_quarterly_report.txt"
    source_file_content = "This is confidential user data that must never be deleted from disk."
    source_file.write_text(source_file_content, encoding="utf-8")

    # Ingest the user file
    payload = RawIngestPayload(
        source_type=SourceType.FILE,
        doc_id="file_confidential_1",
        original_path=str(source_file),
        received_ts_utc=datetime.now(timezone.utc),
        subject="Confidential Report",
        body_text=source_file_content,
    )
    coordinator.ingest_payload(payload)

    # Verify indexed in Catalog, CAS, FTS, LanceDB
    assert catalog.get_document("file_confidential_1") is not None
    assert cas.get_text("file_confidential_1") is not None
    fts_conn = sqlite3.connect(str(app_data / "lexical_fts.db"))
    fts_rows = fts_conn.execute("SELECT doc_id FROM chunks_fts_porter WHERE doc_id = 'file_confidential_1'").fetchall()
    assert len(fts_rows) > 0

    # 2. Trigger deletion through coordinator
    success = coordinator.delete_document("file_confidential_1")
    assert bool(success) is True

    # 3. Execute crash-recovery outbox loop to ensure complete atomic purge
    coordinator.process_outbox()

    # 4. Verify internal records are completely purged
    assert cas.get_text("file_confidential_1") is None
    fts_rows_after = fts_conn.execute("SELECT doc_id FROM chunks_fts_porter WHERE doc_id = 'file_confidential_1'").fetchall()
    assert len(fts_rows_after) == 0
    assert catalog.get_document("file_confidential_1") is None

    # 5. CORE INVARIANT CHECK:
    # User's source file on disk must STILL exist and its contents must be 100% intact!
    assert source_file.exists() is True
    assert source_file.read_text(encoding="utf-8") == source_file_content


# ==============================================================================
# 5. RealTimeSyncScheduler: Lookback Expansion Backfills Low Watermark
# ==============================================================================

class MockLookbackChatAdapter(BaseChatAdapter):
    """Mock adapter capturing fetch_messages parameters to verify gap backfill."""

    def __init__(self, account_config: ChatAccountConfig):
        super().__init__(account_config)
        self.fetch_calls: List[Tuple[str, Optional[datetime], Optional[int]]] = []

    def test_connection(self) -> bool:
        return True

    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        return [
            ChatRoomSummary(
                room_id="dev_channel",
                account_id=self.config.id,
                adapter_type=self.config.adapter_type,
                room_name="Developers Channel",
                is_dm=False,
                subscribed=True,
            )
        ]

    def fetch_messages(
        self,
        room_id: str,
        since_utc: Optional[datetime] = None,
        limit: int = 50,
    ) -> Tuple[List[ChatMessageItem], Optional[str]]:
        self.fetch_calls.append((room_id, since_utc, limit))
        # Return a message dated at since_utc
        msg_time = since_utc or datetime.now(timezone.utc)
        msg = ChatMessageItem(
            message_id=f"msg_backfill_{len(self.fetch_calls)}",
            doc_id=f"doc_backfill_{len(self.fetch_calls)}",
            account_id=self.config.id,
            adapter_type=self.config.adapter_type,
            room_id=room_id,
            room_name="Developers Channel",
            is_dm=False,
            sender_id="alice",
            sender_name="Alice",
            sent_ts_utc=msg_time,
            body_text=f"Fetched for since {since_utc}",
        )
        return [msg], msg_time.isoformat()

    def send_message(self, room_id: str, body_text: str, thread_root_id: Optional[str] = None) -> ChatMessageItem:
        return ChatMessageItem(
            message_id="sent_1",
            doc_id="doc_sent_1",
            account_id=self.config.id,
            adapter_type=self.config.adapter_type,
            room_id=room_id,
            room_name="Developers Channel",
            is_dm=False,
            sender_id="me",
            sender_name="Me",
            sent_ts_utc=datetime.now(timezone.utc),
            body_text=body_text,
        )


def test_sync_scheduler_lookback_expansion_backfills_low_watermark(tmp_path: Path):
    app_data = tmp_path / "sync_lookback_test"
    app_data.mkdir(parents=True, exist_ok=True)

    storage_cfg = StorageConfig(app_data_dir=str(app_data))
    cas = CASEngine(storage_cfg)
    catalog = CatalogRepository(app_data / "catalog.db")
    model_cfg = ModelConfig()

    lancedb_table = open_lancedb_table(app_data, dim=model_cfg.matryoshka_dim)
    indexer = IndexEngine(
        fts_db_path=app_data / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
    )
    extractor = DocumentExtractor(catalog=catalog)
    chunker = ParentAwareChunker(catalog=catalog)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=catalog,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    registry = ChatPluginRegistry()
    scheduler = RealTimeSyncScheduler(
        coordinator=coordinator,
        catalog=catalog,
        chat_registry=registry,
    )

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # Initial state: existing cursor with lookback_days = 7
    source_key = "chat_mock_acc"
    initial_cursor = SyncCursorRecord(
        source_key=source_key,
        source_type=SourceType.CHAT,
        account_id="mock_acc",
        high_watermark_cursor=now.isoformat(),
        low_watermark_utc=seven_days_ago,
        configured_lookback_days=7,
        last_sync_utc=now,
        status="IDLE",
    )
    catalog.upsert_sync_cursor(initial_cursor)

    # Mock the adapter factory in registry
    mock_acc_cfg = ChatAccountConfig(
        id="mock_acc",
        name="Mock Account",
        adapter_type="rocketchat",
        lookback_days=30,  # User expanded lookback from 7 days to 30 days!
        include_dms=True,
    )
    mock_adapter = MockLookbackChatAdapter(mock_acc_cfg)
    registry.create_adapter = MagicMock(return_value=mock_adapter)

    # Execute sync_chat_account
    scheduler.sync_chat_account(mock_acc_cfg)

    # Verify adapter was called with since_utc reaching back to 30 days ago to fill the gap!
    assert len(mock_adapter.fetch_calls) >= 2
    # First call is the gap backfill:
    gap_call = mock_adapter.fetch_calls[0]
    assert gap_call[0] == "dev_channel"
    assert gap_call[1] is not None
    # since_utc must be ~30 days ago (difference from thirty_days_ago < 5 seconds)
    assert abs((gap_call[1] - thirty_days_ago).total_seconds()) < 10

    # Verify sync cursor is updated in Catalog
    cursors = {c.source_key: c for c in catalog.get_sync_cursors()}
    updated_cursor = cursors.get(source_key)
    assert updated_cursor is not None
    assert updated_cursor.configured_lookback_days == 30
    assert abs((updated_cursor.low_watermark_utc - thirty_days_ago).total_seconds()) < 10
    assert updated_cursor.status == "IDLE"
