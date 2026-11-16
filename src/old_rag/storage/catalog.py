"""catalog.py - Sole code owner of %LOCALAPPDATA%\\OldRAG\\catalog.db."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import (
    ChatMessageItem,
    ChatRoomSummary,
    OutboxOp,
    OutboxRecord,
    OutboxStatus,
    SourceType,
    SyncCursorRecord,
)
from old_rag.protocols import CatalogProtocol
from old_rag.schema import CATALOG_SCHEMA_DDL, CURRENT_SCHEMA_VERSION, SCHEMA_V2_TO_V3_MIGRATION_STATEMENTS


class CatalogRepository(CatalogProtocol):
    """Thread-safe SQLite WAL repository for document lineage, blob ref-counting, and outbox."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        with self._lock:
            # Auto-migrate v2 documents and calendar tables if they already exist
            existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "documents" in existing_tables:
                doc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
                for stmt in SCHEMA_V2_TO_V3_MIGRATION_STATEMENTS:
                    try:
                        conn.execute(stmt)
                    except Exception:
                        pass

            conn.executescript(CATALOG_SCHEMA_DDL)
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(vlm_cache)").fetchall()}
            if "template_key" not in cols:
                conn.execute("ALTER TABLE vlm_cache RENAME TO _vlm_cache_v1")
                conn.execute(
                    """
                    CREATE TABLE vlm_cache (
                        blob_hash TEXT NOT NULL,
                        template_key TEXT NOT NULL DEFAULT 'default',
                        schema_hash TEXT NOT NULL DEFAULT '',
                        markdown TEXT NOT NULL,
                        structured_json TEXT,
                        metadata_json TEXT,
                        created_at_utc TEXT NOT NULL,
                        PRIMARY KEY (blob_hash, template_key, schema_hash)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO vlm_cache (
                        blob_hash, template_key, schema_hash, markdown, created_at_utc
                    )
                    SELECT blob_hash, 'default', '', markdown, created_at_utc
                    FROM _vlm_cache_v1
                    """
                )
                conn.execute("DROP TABLE _vlm_cache_v1")

            # Backfill documents.byte_size if 0
            cur = conn.execute("SELECT doc_id FROM documents WHERE byte_size = 0 OR byte_size IS NULL")
            zero_docs = [r["doc_id"] for r in cur.fetchall()]
            if zero_docs:
                try:
                    from old_rag.storage.cas import CASEngine, StorageConfig
                    cas = CASEngine(StorageConfig(app_data_dir=str(self.db_path.parent)))
                    for d_id in zero_docs:
                        sz = cas.get_document_storage_bytes(d_id)
                        if sz > 0:
                            conn.execute("UPDATE documents SET byte_size = ? WHERE doc_id = ?", (sz, d_id))
                except Exception:
                    pass

            existing_versions = {r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()}
            now_str = datetime.now(timezone.utc).isoformat()
            for v in (2, 3):
                if v not in existing_versions:
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at_utc) VALUES (?, ?)",
                        (v, now_str),
                    )
            conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_documents_by_ids(self, doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch document metadata by doc_ids."""
        if not doc_ids:
            return {}
        conn = self._get_connection()
        with self._lock:
            placeholders = ",".join(["?"] * len(doc_ids))
            cur = conn.execute(
                f"SELECT doc_id, source_type, original_path, entry_id, internet_message_id, conversation_id, sender, subject, folder, received_ts_utc FROM documents WHERE doc_id IN ({placeholders})",
                doc_ids,
            )
            return {r["doc_id"]: dict(r) for r in cur.fetchall()}

    def upsert_document(
        self,
        doc_id: str,
        source_type: str,
        original_path: str,
        entry_id: Optional[str] = None,
        internet_message_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
        folder: Optional[str] = None,
        received_ts_utc: Optional[str] = None,
        text_cas_ref: Optional[str] = None,
        raw_msg_cas_ref: Optional[str] = None,
        account_id: Optional[str] = None,
        byte_size: int = 0,
        chunk_ids: Optional[List[str]] = None,
        is_starred: bool = False,
        is_pinned: bool = False,
        is_read: bool = True,
        reply_to_doc_id: Optional[str] = None,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        chunk_json = json.dumps(chunk_ids) if chunk_ids is not None else "[]"
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO documents (
                    doc_id, source_type, original_path, entry_id, internet_message_id,
                    conversation_id, sender, subject, folder, received_ts_utc,
                    text_cas_ref, raw_msg_cas_ref, ingested_at_utc, is_deleted,
                    account_id, byte_size, last_accessed_utc, access_count,
                    is_pinned, is_starred, is_read, reply_to_doc_id, chunk_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    original_path=excluded.original_path,
                    entry_id=excluded.entry_id,
                    folder=excluded.folder,
                    text_cas_ref=COALESCE(excluded.text_cas_ref, documents.text_cas_ref),
                    raw_msg_cas_ref=COALESCE(excluded.raw_msg_cas_ref, documents.raw_msg_cas_ref),
                    account_id=COALESCE(excluded.account_id, documents.account_id),
                    byte_size=CASE WHEN excluded.byte_size > 0 THEN excluded.byte_size ELSE documents.byte_size END,
                    is_pinned=CASE WHEN excluded.is_pinned != 0 THEN excluded.is_pinned ELSE documents.is_pinned END,
                    is_starred=CASE WHEN excluded.is_starred != 0 THEN excluded.is_starred ELSE documents.is_starred END,
                    is_read=excluded.is_read,
                    reply_to_doc_id=COALESCE(excluded.reply_to_doc_id, documents.reply_to_doc_id),
                    chunk_ids_json=CASE WHEN excluded.chunk_ids_json != '[]' THEN excluded.chunk_ids_json ELSE documents.chunk_ids_json END,
                    is_deleted=0;
                """,
                (
                    doc_id, source_type, original_path, entry_id, internet_message_id,
                    conversation_id, sender, subject, folder, received_ts_utc,
                    text_cas_ref, raw_msg_cas_ref, now_str,
                    account_id, byte_size, now_str,
                    1 if is_pinned else 0, 1 if is_starred else 0, 1 if is_read else 0,
                    reply_to_doc_id, chunk_json,
                ),
            )
            conn.commit()

    def record_blob_ref(self, blob_hash: str, doc_id: str, size_bytes: int) -> int:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO blobs (blob_hash, size_bytes, ref_count, first_seen_utc)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(blob_hash) DO UPDATE SET
                    ref_count = ref_count + 1;
                """,
                (blob_hash, size_bytes, now_str),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO blob_references (blob_hash, doc_id)
                VALUES (?, ?);
                """,
                (blob_hash, doc_id),
            )
            cur = conn.execute("SELECT ref_count FROM blobs WHERE blob_hash = ?", (blob_hash,))
            row = cur.fetchone()
            conn.commit()
            return row["ref_count"] if row else 1

    def release_blob_ref(self, blob_hash: str, doc_id: str) -> int:
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                "DELETE FROM blob_references WHERE blob_hash = ? AND doc_id = ?",
                (blob_hash, doc_id),
            )
            conn.execute(
                "UPDATE blobs SET ref_count = MAX(0, ref_count - 1) WHERE blob_hash = ?",
                (blob_hash,),
            )
            cur = conn.execute("SELECT ref_count FROM blobs WHERE blob_hash = ?", (blob_hash,))
            row = cur.fetchone()
            conn.commit()
            return row["ref_count"] if row else 0

    def get_vlm_cache(
        self,
        blob_hash: str,
        template_key: str = "default",
        schema_hash: str = "",
    ) -> Optional[str]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(
                """
                SELECT markdown FROM vlm_cache
                WHERE blob_hash = ? AND template_key = ? AND schema_hash = ?
                """,
                (blob_hash, template_key, schema_hash),
            )
            row = cur.fetchone()
            return row["markdown"] if row else None

    def get_vlm_cache_entry(
        self,
        blob_hash: str,
        template_key: str = "default",
        schema_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(
                """
                SELECT markdown, structured_json, metadata_json
                FROM vlm_cache
                WHERE blob_hash = ? AND template_key = ? AND schema_hash = ?
                """,
                (blob_hash, template_key, schema_hash),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def put_vlm_cache(
        self,
        blob_hash: str,
        markdown: str,
        template_key: str = "default",
        schema_hash: str = "",
        structured_json: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO vlm_cache (
                    blob_hash, template_key, schema_hash, markdown,
                    structured_json, metadata_json, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(blob_hash, template_key, schema_hash) DO UPDATE SET
                    markdown = excluded.markdown,
                    structured_json = COALESCE(excluded.structured_json, vlm_cache.structured_json),
                    metadata_json = COALESCE(excluded.metadata_json, vlm_cache.metadata_json);
                """,
                (
                    blob_hash,
                    template_key,
                    schema_hash,
                    markdown,
                    structured_json,
                    metadata_json,
                    now_str,
                ),
            )
            conn.commit()

    def upsert_document_entities(
        self,
        doc_id: str,
        entities: List[tuple],
    ) -> None:
        """Persist flattened structured entities [(entity_key, entity_value, source_filename)] for a doc."""
        if not entities:
            return
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.executemany(
                """
                INSERT OR REPLACE INTO document_entities (
                    doc_id, entity_key, entity_value, source_filename, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [(doc_id, str(k), str(v), str(src or ""), now_str) for k, v, src in entities],
            )
            conn.commit()

    def get_document_entities(self, doc_id: str) -> List[Dict[str, Any]]:
        """Retrieve persisted structured entities for a document."""
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(
                """
                SELECT entity_key, entity_value, source_filename, created_at_utc
                FROM document_entities
                WHERE doc_id = ?
                ORDER BY entity_key, entity_value
                """,
                (doc_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def is_vlm_skipped(self, blob_hash: str) -> bool:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT 1 FROM vlm_skip_hashes WHERE blob_hash = ?", (blob_hash,))
            return cur.fetchone() is not None

    def add_vlm_skip_hash(self, blob_hash: str, reason: str = "signature") -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT OR IGNORE INTO vlm_skip_hashes (blob_hash, reason, created_at_utc)
                VALUES (?, ?, ?);
                """,
                (blob_hash, reason, now_str),
            )
            conn.commit()

    def enqueue_outbox(self, op: OutboxOp, doc_id: str, chunk_ids: List[str]) -> int:
        now_str = datetime.now(timezone.utc).isoformat()
        chunk_json = json.dumps(chunk_ids)
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(
                """
                INSERT INTO outbox (op, doc_id, chunk_ids_json, status, created_at_utc)
                VALUES (?, ?, ?, ?, ?);
                """,
                (op.value, doc_id, chunk_json, OutboxStatus.PENDING.value, now_str),
            )
            outbox_id = cur.lastrowid
            conn.commit()
            return outbox_id

    def mark_outbox_committed(self, outbox_id: int) -> None:
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                "UPDATE outbox SET status = ? WHERE outbox_id = ?",
                (OutboxStatus.COMMITTED.value, outbox_id),
            )
            conn.commit()

    def get_pending_outbox(self) -> List[OutboxRecord]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(
                "SELECT * FROM outbox WHERE status = ? ORDER BY outbox_id ASC",
                (OutboxStatus.PENDING.value,),
            )
            results = []
            for row in cur.fetchall():
                results.append(
                    OutboxRecord(
                        outbox_id=row["outbox_id"],
                        op=OutboxOp(row["op"]),
                        doc_id=row["doc_id"],
                        chunk_ids=json.loads(row["chunk_ids_json"]),
                        status=OutboxStatus(row["status"]),
                        created_at_utc=datetime.fromisoformat(row["created_at_utc"]),
                        error_message=row["error_message"],
                    )
                )
            return results

    def prune_orphans(self, purge_source_deleted: bool) -> List[str]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT blob_hash FROM blobs WHERE ref_count <= 0")
            orphans = [r["blob_hash"] for r in cur.fetchall()]
            if orphans:
                placeholders = ",".join("?" for _ in orphans)
                conn.execute(f"DELETE FROM blobs WHERE blob_hash IN ({placeholders})", orphans)
                conn.commit()
            return orphans

    def record_query_metric(
        self,
        query_text: str,
        resolved_by: str,
        hit_count: int,
        top_confidence: float,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO search_metrics (query_text, resolved_by, hit_count, top_confidence, executed_at_utc)
                VALUES (?, ?, ?, ?, ?);
                """,
                (query_text, resolved_by, hit_count, float(top_confidence), now_str),
            )
            conn.commit()

    def get_statistics(self) -> Dict[str, Any]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT COUNT(*) FROM search_metrics")
            total_queries = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM search_metrics WHERE resolved_by = 'hybrid'")
            resolved_by_hybrid = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM search_metrics WHERE resolved_by = 'vector'")
            resolved_by_vector = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM search_metrics WHERE resolved_by = 'lexical'")
            resolved_by_lexical = cur.fetchone()[0]

            cur = conn.execute("SELECT COALESCE(SUM(size_bytes * (ref_count - 1)), 0) FROM blobs WHERE ref_count > 1")
            storage_saved_bytes = int(cur.fetchone()[0])

            cur = conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted = 0")
            total_documents = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM blobs")
            row = cur.fetchone()
            total_blobs = row[0]
            total_blob_bytes = int(row[1])

            cur = conn.execute(
                """
                SELECT query_text, resolved_by, hit_count, top_confidence, executed_at_utc
                FROM search_metrics
                ORDER BY query_id DESC
                LIMIT 10
                """
            )
            recent = [dict(r) for r in cur.fetchall()]

            return {
                "total_queries": total_queries,
                "resolved_by_hybrid": resolved_by_hybrid,
                "resolved_by_vector": resolved_by_vector,
                "resolved_by_lexical": resolved_by_lexical,
                "storage_saved_bytes": storage_saved_bytes,
                "total_documents": total_documents,
                "total_blobs": total_blobs,
                "total_blob_bytes": total_blob_bytes,
                "recent_queries": recent,
                "is_sovereign": True,
            }

    def record_document_access(self, doc_id: str) -> None:
        """Increment access_count and set last_accessed_utc = now_utc."""
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                UPDATE documents
                SET access_count = access_count + 1, last_accessed_utc = ?
                WHERE doc_id = ?
                """,
                (now_str, doc_id),
            )
            conn.commit()

    def set_document_flags(
        self,
        doc_id: str,
        is_starred: Optional[bool] = None,
        is_pinned: Optional[bool] = None,
        is_read: Optional[bool] = None,
    ) -> None:
        """Update flags on documents table."""
        updates = []
        params = []
        if is_starred is not None:
            updates.append("is_starred = ?")
            params.append(1 if is_starred else 0)
        if is_pinned is not None:
            updates.append("is_pinned = ?")
            params.append(1 if is_pinned else 0)
        if is_read is not None:
            updates.append("is_read = ?")
            params.append(1 if is_read else 0)
        if not updates:
            return
        params.append(doc_id)
        conn = self._get_connection()
        with self._lock:
            conn.execute(f"UPDATE documents SET {', '.join(updates)} WHERE doc_id = ?", params)
            conn.commit()

    def upsert_chat_room(self, room: ChatRoomSummary) -> None:
        """Insert or update a chat room in catalog."""
        conn = self._get_connection()
        last_msg_ts = room.last_message_ts_utc.isoformat() if room.last_message_ts_utc else None
        with self._lock:
            conn.execute(
                """
                INSERT INTO chat_rooms (
                    room_id, account_id, adapter_type, room_name, is_dm,
                    participant_count, unread_count, last_message_ts_utc, last_snippet, subscribed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id, account_id) DO UPDATE SET
                    room_name=excluded.room_name,
                    is_dm=excluded.is_dm,
                    participant_count=excluded.participant_count,
                    unread_count=excluded.unread_count,
                    last_message_ts_utc=COALESCE(excluded.last_message_ts_utc, chat_rooms.last_message_ts_utc),
                    last_snippet=COALESCE(excluded.last_snippet, chat_rooms.last_snippet),
                    subscribed=excluded.subscribed;
                """,
                (
                    room.room_id, room.account_id, room.adapter_type, room.room_name,
                    1 if room.is_dm else 0, room.participant_count, room.unread_count,
                    last_msg_ts, room.last_snippet, 1 if room.subscribed else 0,
                ),
            )
            conn.commit()

    def list_chat_rooms(self, account_id: Optional[str] = None, include_dms: bool = True) -> List[ChatRoomSummary]:
        """List chat rooms filtered by account and DM inclusion."""
        conn = self._get_connection()
        query = "SELECT * FROM chat_rooms WHERE 1=1"
        params: List[Any] = []
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        if not include_dms:
            query += " AND is_dm = 0"
        query += " ORDER BY last_message_ts_utc DESC NULLS LAST"
        with self._lock:
            rows = conn.execute(query, params).fetchall()
            results = []
            for r in rows:
                last_ts = datetime.fromisoformat(r["last_message_ts_utc"]) if r["last_message_ts_utc"] else None
                results.append(
                    ChatRoomSummary(
                        room_id=r["room_id"],
                        account_id=r["account_id"],
                        adapter_type=r["adapter_type"],
                        room_name=r["room_name"],
                        is_dm=bool(r["is_dm"]),
                        participant_count=r["participant_count"],
                        unread_count=r["unread_count"],
                        last_message_ts_utc=last_ts,
                        last_snippet=r["last_snippet"] or "",
                        subscribed=bool(r["subscribed"]),
                    )
                )
            return results

    def upsert_chat_messages(self, messages: List[ChatMessageItem]) -> None:
        """Insert or replace chat messages in catalog."""
        if not messages:
            return
        conn = self._get_connection()
        with self._lock:
            for msg in messages:
                conn.execute(
                    """
                    INSERT INTO chat_messages (
                        message_id, doc_id, account_id, adapter_type, room_id,
                        room_name, is_dm, sender_id, sender_name, thread_root_id,
                        sent_ts_utc, body_text, reactions_json, attachments_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id, account_id) DO UPDATE SET
                        doc_id=excluded.doc_id,
                        room_name=excluded.room_name,
                        sender_name=excluded.sender_name,
                        thread_root_id=excluded.thread_root_id,
                        body_text=excluded.body_text,
                        reactions_json=excluded.reactions_json,
                        attachments_json=excluded.attachments_json;
                    """,
                    (
                        msg.message_id, msg.doc_id, msg.account_id, msg.adapter_type, msg.room_id,
                        msg.room_name, 1 if msg.is_dm else 0, msg.sender_id, msg.sender_name,
                        msg.thread_root_id, msg.sent_ts_utc.isoformat(), msg.body_text,
                        json.dumps(msg.reactions), json.dumps(msg.attachments),
                    ),
                )
            conn.commit()

    def list_chat_messages(
        self,
        room_id: str,
        account_id: Optional[str] = None,
        thread_root_id: Optional[str] = None,
        limit: int = 100,
        before_ts_utc: Optional[str] = None,
    ) -> List[ChatMessageItem]:
        """List chat messages in a room or thread."""
        conn = self._get_connection()
        query = "SELECT * FROM chat_messages WHERE room_id = ?"
        params: List[Any] = [room_id]
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        if thread_root_id:
            query += " AND (thread_root_id = ? OR message_id = ?)"
            params.extend([thread_root_id, thread_root_id])
        if before_ts_utc:
            query += " AND sent_ts_utc < ?"
            params.append(before_ts_utc)
        query += " ORDER BY sent_ts_utc ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = conn.execute(query, params).fetchall()
            results = []
            for r in rows:
                results.append(
                    ChatMessageItem(
                        message_id=r["message_id"],
                        doc_id=r["doc_id"],
                        account_id=r["account_id"],
                        adapter_type=r["adapter_type"],
                        room_id=r["room_id"],
                        room_name=r["room_name"] or "",
                        is_dm=bool(r["is_dm"]),
                        sender_id=r["sender_id"] or "",
                        sender_name=r["sender_name"] or "",
                        thread_root_id=r["thread_root_id"],
                        sent_ts_utc=datetime.fromisoformat(r["sent_ts_utc"]),
                        body_text=r["body_text"],
                        reactions=json.loads(r["reactions_json"] or "{}"),
                        attachments=json.loads(r["attachments_json"] or "[]"),
                    )
                )
            return results

    def upsert_sync_cursor(self, cursor: SyncCursorRecord) -> None:
        """Insert or replace sync cursor record."""
        low_utc = cursor.low_watermark_utc.isoformat() if cursor.low_watermark_utc else None
        last_sync = cursor.last_sync_utc.isoformat() if cursor.last_sync_utc else None
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO sync_cursors (
                    source_key, source_type, account_id, high_watermark_cursor,
                    low_watermark_utc, configured_lookback_days, last_sync_utc,
                    status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    high_watermark_cursor=COALESCE(excluded.high_watermark_cursor, sync_cursors.high_watermark_cursor),
                    low_watermark_utc=COALESCE(excluded.low_watermark_utc, sync_cursors.low_watermark_utc),
                    configured_lookback_days=COALESCE(excluded.configured_lookback_days, sync_cursors.configured_lookback_days),
                    last_sync_utc=COALESCE(excluded.last_sync_utc, sync_cursors.last_sync_utc),
                    status=excluded.status,
                    last_error=excluded.last_error;
                """,
                (
                    cursor.source_key, cursor.source_type.value, cursor.account_id,
                    cursor.high_watermark_cursor, low_utc, cursor.configured_lookback_days,
                    last_sync, cursor.status, cursor.last_error,
                ),
            )
            conn.commit()

    def get_sync_cursors(self) -> List[SyncCursorRecord]:
        """Fetch all sync cursor records."""
        conn = self._get_connection()
        with self._lock:
            rows = conn.execute("SELECT * FROM sync_cursors").fetchall()
            results = []
            for r in rows:
                low_utc = datetime.fromisoformat(r["low_watermark_utc"]) if r["low_watermark_utc"] else None
                last_sync = datetime.fromisoformat(r["last_sync_utc"]) if r["last_sync_utc"] else None
                results.append(
                    SyncCursorRecord(
                        source_key=r["source_key"],
                        source_type=SourceType(r["source_type"]),
                        account_id=r["account_id"],
                        high_watermark_cursor=r["high_watermark_cursor"],
                        low_watermark_utc=low_utc,
                        configured_lookback_days=r["configured_lookback_days"],
                        last_sync_utc=last_sync,
                        status=r["status"] or "IDLE",
                        last_error=r["last_error"],
                    )
                )
            return results

    def get_usage_by_source_type(self) -> Dict[str, Dict[str, int]]:
        """Calculate exact byte usage, doc_count, chunk_count, exclusive and shared blob bytes per source type."""
        conn = self._get_connection()
        usage: Dict[str, Dict[str, int]] = {}
        for st in SourceType:
            usage[st.value] = {
                "used_bytes": 0,
                "document_count": 0,
                "chunk_count": 0,
                "exclusive_blob_bytes": 0,
                "shared_blob_bytes": 0,
            }
        with self._lock:
            rows = conn.execute(
                """
                SELECT source_type, COUNT(*) as doc_count, COALESCE(SUM(byte_size), 0) as total_bytes
                FROM documents
                WHERE is_deleted = 0
                GROUP BY source_type
                """
            ).fetchall()
            for r in rows:
                st = r["source_type"]
                if st in usage:
                    usage[st]["used_bytes"] = int(r["total_bytes"])
                    usage[st]["document_count"] = int(r["doc_count"])

            chunk_rows = conn.execute(
                "SELECT source_type, chunk_ids_json FROM documents WHERE is_deleted = 0"
            ).fetchall()
            for r in chunk_rows:
                st = r["source_type"]
                if st in usage:
                    c_json = r["chunk_ids_json"]
                    try:
                        c_list = json.loads(c_json) if c_json else []
                        usage[st]["chunk_count"] += len(c_list)
                    except Exception:
                        pass

            blob_rows = conn.execute(
                """
                SELECT d.source_type, b.blob_hash, b.size_bytes, b.ref_count
                FROM blob_references br
                JOIN documents d ON br.doc_id = d.doc_id
                JOIN blobs b ON br.blob_hash = b.blob_hash
                WHERE d.is_deleted = 0
                """
            ).fetchall()
            for r in blob_rows:
                st = r["source_type"]
                if st in usage:
                    sz = int(r["size_bytes"])
                    if r["ref_count"] == 1:
                        usage[st]["exclusive_blob_bytes"] += sz
                    else:
                        usage[st]["shared_blob_bytes"] += sz
        return usage

    def delete_document_hard(self, doc_id: str) -> List[str]:
        """Hard delete document from catalog with cascade, releasing blob refs and returning pruned blob hashes."""
        conn = self._get_connection()
        pruned_blobs: List[str] = []
        with self._lock:
            rows = conn.execute("SELECT blob_hash FROM blob_references WHERE doc_id = ?", (doc_id,)).fetchall()
            referenced_hashes = [r["blob_hash"] for r in rows]

            conn.execute("DELETE FROM blob_references WHERE doc_id = ?", (doc_id,))

            for bh in referenced_hashes:
                conn.execute("UPDATE blobs SET ref_count = MAX(0, ref_count - 1) WHERE blob_hash = ?", (bh,))
                check = conn.execute("SELECT ref_count FROM blobs WHERE blob_hash = ?", (bh,)).fetchone()
                if check and check["ref_count"] <= 0:
                    pruned_blobs.append(bh)

            if pruned_blobs:
                placeholders = ",".join("?" for _ in pruned_blobs)
                conn.execute(f"DELETE FROM blobs WHERE blob_hash IN ({placeholders})", pruned_blobs)
                conn.execute(f"DELETE FROM vlm_cache WHERE blob_hash IN ({placeholders})", pruned_blobs)

            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()
        return pruned_blobs

    def checkpoint_and_vacuum(self) -> None:
        """Run WAL checkpoint truncate to reclaim disk space after deletions."""
        conn = self._get_connection()
        with self._lock:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

