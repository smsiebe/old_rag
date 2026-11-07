"""catalog.py - Sole code owner of %LOCALAPPDATA%\\OldRAG\\catalog.db."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import OutboxOp, OutboxRecord, OutboxStatus
from old_rag.protocols import CatalogProtocol
from old_rag.schema import CATALOG_SCHEMA_DDL, CURRENT_SCHEMA_VERSION


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
            conn.executescript(CATALOG_SCHEMA_DDL)
            cursor = conn.execute("SELECT version FROM schema_version WHERE version = ?", (CURRENT_SCHEMA_VERSION,))
            if not cursor.fetchone():
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at_utc) VALUES (?, ?)",
                    (CURRENT_SCHEMA_VERSION, now_str),
                )
            conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
            row = cur.fetchone()
            return dict(row) if row else None

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
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO documents (
                    doc_id, source_type, original_path, entry_id, internet_message_id,
                    conversation_id, sender, subject, folder, received_ts_utc,
                    text_cas_ref, raw_msg_cas_ref, ingested_at_utc, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(doc_id) DO UPDATE SET
                    original_path=excluded.original_path,
                    entry_id=excluded.entry_id,
                    folder=excluded.folder,
                    text_cas_ref=COALESCE(excluded.text_cas_ref, documents.text_cas_ref),
                    raw_msg_cas_ref=COALESCE(excluded.raw_msg_cas_ref, documents.raw_msg_cas_ref),
                    is_deleted=0;
                """,
                (
                    doc_id, source_type, original_path, entry_id, internet_message_id,
                    conversation_id, sender, subject, folder, received_ts_utc,
                    text_cas_ref, raw_msg_cas_ref, now_str,
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

    def get_vlm_cache(self, blob_hash: str) -> Optional[str]:
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("SELECT markdown FROM vlm_cache WHERE blob_hash = ?", (blob_hash,))
            row = cur.fetchone()
            return row["markdown"] if row else None

    def put_vlm_cache(self, blob_hash: str, markdown: str) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO vlm_cache (blob_hash, markdown, created_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(blob_hash) DO UPDATE SET markdown = excluded.markdown;
                """,
                (blob_hash, markdown, now_str),
            )
            conn.commit()

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
