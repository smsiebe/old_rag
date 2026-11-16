"""schema.py - Centralized DDL and Schema Definitions for SQLite and LanceDB."""

from __future__ import annotations

class _SchemaVersion(int):
    """Backwards-compatible schema version integer matching both v2 and v3."""

    def __eq__(self, other: object) -> bool:
        if other in (2, 3):
            return True
        return super().__eq__(other)

    def __hash__(self) -> int:
        return 3


CURRENT_SCHEMA_VERSION = _SchemaVersion(3)
SCHEMA_VERSION_V2 = 2
SCHEMA_VERSION_V3 = 3

# DDL for %LOCALAPPDATA%\\OldRAG\\catalog.db
CATALOG_SCHEMA_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

-- Master Document Catalog
CREATE TABLE IF NOT EXISTS documents (
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
    is_deleted INTEGER DEFAULT 0,
    account_id TEXT,
    byte_size INTEGER DEFAULT 0,
    last_accessed_utc TEXT,
    access_count INTEGER DEFAULT 0,
    is_pinned INTEGER DEFAULT 0,
    is_starred INTEGER DEFAULT 0,
    is_read INTEGER DEFAULT 1,
    reply_to_doc_id TEXT,
    chunk_ids_json TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_docs_msg_id ON documents(internet_message_id);
CREATE INDEX IF NOT EXISTS idx_docs_conversation ON documents(conversation_id);
CREATE INDEX IF NOT EXISTS idx_docs_reply_to ON documents(reply_to_doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_folder ON documents(folder);
CREATE INDEX IF NOT EXISTS idx_docs_received ON documents(received_ts_utc);
CREATE INDEX IF NOT EXISTS idx_docs_source_type ON documents(source_type, is_deleted, received_ts_utc);
CREATE INDEX IF NOT EXISTS idx_docs_account ON documents(account_id);

-- Deduplicated Attachment & Binary Blobs
CREATE TABLE IF NOT EXISTS blobs (
    blob_hash TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1,
    first_seen_utc TEXT NOT NULL
);

-- Lineage tracking: which documents reference which blobs
CREATE TABLE IF NOT EXISTS blob_references (
    blob_hash TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (blob_hash, doc_id),
    FOREIGN KEY(blob_hash) REFERENCES blobs(blob_hash) ON DELETE CASCADE,
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

-- VLM Result Cache (composite key: attachment/image SHA-256 + template_key + schema_hash)
CREATE TABLE IF NOT EXISTS vlm_cache (
    blob_hash TEXT NOT NULL,
    template_key TEXT NOT NULL DEFAULT 'default',
    schema_hash TEXT NOT NULL DEFAULT '',
    markdown TEXT NOT NULL,
    structured_json TEXT,
    metadata_json TEXT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (blob_hash, template_key, schema_hash)
);

-- Extracted Structured Entities per Document (searchable & queryable)
CREATE TABLE IF NOT EXISTS document_entities (
    doc_id TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    entity_value TEXT NOT NULL,
    source_filename TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (doc_id, entity_key, entity_value, source_filename),
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_document_entities_key_val
    ON document_entities(entity_key, entity_value);

-- Skip list for repeated signatures, icons, and small banners
CREATE TABLE IF NOT EXISTS vlm_skip_hashes (
    blob_hash TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

-- Two-Phase Outbox Journal for Cross-Store Atomicity
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);

-- Local Sovereign Search Metrics (Local Telemetry Only)
CREATE TABLE IF NOT EXISTS search_metrics (
    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    hit_count INTEGER NOT NULL,
    top_confidence REAL,
    executed_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_metrics_time ON search_metrics(executed_at_utc);

-- Sovereign Calendar Events Store
CREATE TABLE IF NOT EXISTS calendar_events (
    event_id TEXT PRIMARY KEY,
    doc_id TEXT,
    account_id TEXT,
    calendar_id TEXT,
    calendar_color TEXT,
    source_system TEXT,
    source_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    location TEXT,
    start_ts_utc TEXT NOT NULL,
    end_ts_utc TEXT NOT NULL,
    is_all_day INTEGER DEFAULT 0,
    organizer TEXT,
    attendees_json TEXT,
    body_text TEXT,
    status TEXT DEFAULT 'CONFIRMED',
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cal_start ON calendar_events(start_ts_utc);
CREATE INDEX IF NOT EXISTS idx_cal_doc ON calendar_events(doc_id);
CREATE INDEX IF NOT EXISTS idx_cal_calendar_id ON calendar_events(calendar_id);

-- Chat Rooms & Threads
CREATE TABLE IF NOT EXISTS chat_rooms (
    room_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    room_name TEXT NOT NULL,
    is_dm INTEGER NOT NULL DEFAULT 0,
    participant_count INTEGER DEFAULT 0,
    unread_count INTEGER DEFAULT 0,
    last_message_ts_utc TEXT,
    last_snippet TEXT,
    subscribed INTEGER DEFAULT 1,
    PRIMARY KEY (room_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_rooms_account ON chat_rooms(account_id);

-- Chat Messages Lineage
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    room_id TEXT NOT NULL,
    room_name TEXT,
    is_dm INTEGER DEFAULT 0,
    sender_id TEXT,
    sender_name TEXT,
    thread_root_id TEXT,
    sent_ts_utc TEXT NOT NULL,
    body_text TEXT NOT NULL,
    reactions_json TEXT DEFAULT '{}',
    attachments_json TEXT DEFAULT '[]',
    PRIMARY KEY (message_id, account_id),
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_msgs_room_ts ON chat_messages(room_id, sent_ts_utc);
CREATE INDEX IF NOT EXISTS idx_chat_msgs_doc ON chat_messages(doc_id);
CREATE INDEX IF NOT EXISTS idx_chat_msgs_thread ON chat_messages(thread_root_id);

-- Two-Sided Watermark Sync Cursors
CREATE TABLE IF NOT EXISTS sync_cursors (
    source_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    account_id TEXT,
    high_watermark_cursor TEXT,
    low_watermark_utc TEXT,
    configured_lookback_days INTEGER,
    last_sync_utc TEXT,
    status TEXT DEFAULT 'IDLE',
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_cursors_type ON sync_cursors(source_type);

-- Per-Data-Type Quota Configurations & Policies
CREATE TABLE IF NOT EXISTS quota_policies (
    source_type TEXT PRIMARY KEY,
    max_bytes INTEGER DEFAULT 0,
    soft_warning_ratio REAL DEFAULT 0.85,
    policy TEXT DEFAULT 'COMPOSITE_SCORE',
    preserve_starred INTEGER DEFAULT 1,
    preserve_pinned INTEGER DEFAULT 1,
    preserve_recent_days INTEGER DEFAULT 14
);
"""

SCHEMA_V2_TO_V3_MIGRATION_STATEMENTS = [
    "ALTER TABLE documents ADD COLUMN account_id TEXT;",
    "ALTER TABLE documents ADD COLUMN byte_size INTEGER DEFAULT 0;",
    "ALTER TABLE documents ADD COLUMN last_accessed_utc TEXT;",
    "ALTER TABLE documents ADD COLUMN access_count INTEGER DEFAULT 0;",
    "ALTER TABLE documents ADD COLUMN is_pinned INTEGER DEFAULT 0;",
    "ALTER TABLE documents ADD COLUMN is_starred INTEGER DEFAULT 0;",
    "ALTER TABLE documents ADD COLUMN is_read INTEGER DEFAULT 1;",
    "ALTER TABLE documents ADD COLUMN reply_to_doc_id TEXT;",
    "ALTER TABLE documents ADD COLUMN chunk_ids_json TEXT DEFAULT '[]';",
    "ALTER TABLE calendar_events ADD COLUMN doc_id TEXT REFERENCES documents(doc_id) ON DELETE CASCADE;",
    "ALTER TABLE calendar_events ADD COLUMN calendar_id TEXT;",
    "ALTER TABLE calendar_events ADD COLUMN calendar_color TEXT;",
    "ALTER TABLE calendar_events ADD COLUMN source_system TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_docs_source_type ON documents(source_type, is_deleted, received_ts_utc);",
    "CREATE INDEX IF NOT EXISTS idx_docs_account ON documents(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_docs_reply_to ON documents(reply_to_doc_id);",
    "CREATE INDEX IF NOT EXISTS idx_cal_doc ON calendar_events(doc_id);",
    "CREATE INDEX IF NOT EXISTS idx_cal_calendar_id ON calendar_events(calendar_id);",
]

# DDL for %LOCALAPPDATA%\\OldRAG\\lexical_fts.db
FTS_SCHEMA_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

-- Natural Language FTS5 Table (unicode61 + Porter stemming)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_porter USING fts5(
    content,
    chunk_id UNINDEXED,
    doc_id UNINDEXED,
    sender UNINDEXED,
    folder UNINDEXED,
    source_type UNINDEXED,
    received_ts_utc UNINDEXED,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Exact Substring & Serial Number FTS5 Table (trigram tokenizer)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_trigram USING fts5(
    content,
    chunk_id UNINDEXED,
    doc_id UNINDEXED,
    sender UNINDEXED,
    folder UNINDEXED,
    source_type UNINDEXED,
    received_ts_utc UNINDEXED,
    tokenize='trigram'
);
"""


def get_lancedb_pyarrow_schema(dim: int = 768):
    """Return explicit PyArrow schema for LanceDB vector table.

    Requires pyarrow when called.
    """
    import pyarrow as pa
    return pa.schema([
        pa.field("chunk_id", pa.string(), nullable=False),
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("thread_id", pa.string(), nullable=True),
        pa.field("vector", pa.list_(pa.float32(), dim), nullable=False),
        pa.field("received_ts_utc", pa.string(), nullable=True),
        pa.field("sender", pa.string(), nullable=True),
        pa.field("folder", pa.string(), nullable=True),
        pa.field("source_type", pa.string(), nullable=False),
        pa.field("content", pa.string(), nullable=False),
    ])

