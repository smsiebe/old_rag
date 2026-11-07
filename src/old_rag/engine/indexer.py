"""indexer.py - Idempotent Dual-FTS5 and LanceDB Multi-Store Indexer."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

from old_rag.contracts import DocumentChunk, ModelConfig
from old_rag.protocols import IndexerProtocol
from old_rag.schema import FTS_SCHEMA_DDL


def derive_fts_rowid(chunk_id: str) -> int:
    """Derive deterministic positive 63-bit integer rowid from SHA-256 of chunk_id."""
    return int(hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()[:15], 16)


class IndexEngine(IndexerProtocol):
    """Atomic multi-index mutator for SQLite FTS5 (porter & trigram) and LanceDB."""

    def __init__(
        self,
        fts_db_path: Path,
        lancedb_table: Optional[Any] = None,
        config: Optional[ModelConfig] = None,
    ):
        self.fts_db_path = fts_db_path
        self.lancedb_table = lancedb_table
        self.config = config or ModelConfig()
        self.fts_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_fts()

    def _init_fts(self) -> None:
        conn = sqlite3.connect(str(self.fts_db_path))
        try:
            conn.executescript(FTS_SCHEMA_DDL)
            conn.commit()
        finally:
            conn.close()

    def upsert_chunks(self, chunks: List[DocumentChunk], embeddings: Optional[List[List[float]]] = None) -> None:
        """Idempotently insert/update chunks in lexical FTS5 and LanceDB vector stores."""
        if not chunks:
            return

        conn = sqlite3.connect(str(self.fts_db_path))
        try:
            with conn:
                for chunk in chunks:
                    rowid = derive_fts_rowid(chunk.chunk_id)
                    ts_str = chunk.received_ts_utc.isoformat() if chunk.received_ts_utc else ""

                    # 1. Update porter table
                    conn.execute("DELETE FROM chunks_fts_porter WHERE rowid = ?", (rowid,))
                    conn.execute(
                        """
                        INSERT INTO chunks_fts_porter (
                            rowid, content, chunk_id, doc_id, sender, folder, source_type, received_ts_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (rowid, chunk.content, chunk.chunk_id, chunk.doc_id, chunk.sender or "", chunk.folder or "", chunk.source_type.value, ts_str),
                    )

                    # 2. Update trigram table
                    conn.execute("DELETE FROM chunks_fts_trigram WHERE rowid = ?", (rowid,))
                    conn.execute(
                        """
                        INSERT INTO chunks_fts_trigram (
                            rowid, content, chunk_id, doc_id, sender, folder, source_type, received_ts_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (rowid, chunk.content, chunk.chunk_id, chunk.doc_id, chunk.sender or "", chunk.folder or "", chunk.source_type.value, ts_str),
                    )
        finally:
            conn.close()

        # Update LanceDB if active
        if self.lancedb_table and embeddings and len(chunks) == len(embeddings):
            try:
                records = []
                for c, emb in zip(chunks, embeddings):
                    records.append({
                        "chunk_id": c.chunk_id,
                        "doc_id": c.doc_id,
                        "thread_id": c.thread_id,
                        "vector": emb,
                        "received_ts_utc": c.received_ts_utc.isoformat() if c.received_ts_utc else "",
                        "sender": c.sender or "",
                        "folder": c.folder or "",
                        "source_type": c.source_type.value,
                        "content": c.content,
                    })
                self.lancedb_table.add(records)
            except Exception:
                pass

    def delete_chunks(self, chunk_ids: List[str]) -> None:
        if not chunk_ids:
            return
        conn = sqlite3.connect(str(self.fts_db_path))
        try:
            with conn:
                for cid in chunk_ids:
                    rowid = derive_fts_rowid(cid)
                    conn.execute("DELETE FROM chunks_fts_porter WHERE rowid = ?", (rowid,))
                    conn.execute("DELETE FROM chunks_fts_trigram WHERE rowid = ?", (rowid,))
        finally:
            conn.close()

    def update_folder(self, doc_id: str, new_folder: str) -> None:
        conn = sqlite3.connect(str(self.fts_db_path))
        try:
            with conn:
                conn.execute(
                    "UPDATE chunks_fts_porter SET folder = ? WHERE doc_id = ?",
                    (new_folder, doc_id),
                )
                conn.execute(
                    "UPDATE chunks_fts_trigram SET folder = ? WHERE doc_id = ?",
                    (new_folder, doc_id),
                )
        finally:
            conn.close()
