"""coordinator.py - Single-Writer Outbox Coordinator & Offline Re-indexer."""

from __future__ import annotations

import json
from typing import List, Optional

from old_rag.contracts import (
    CalendarEvent,
    ChatMessageItem,
    ChatRoomSummary,
    NormalizedDocumentJSON,
    OutboxOp,
    OutboxRecord,
    RawIngestPayload,
    SourceType,
)
from old_rag.engine.extractor import _flatten_structured_fields
from old_rag.protocols import (
    CatalogProtocol,
    ChunkerProtocol,
    ContentStoreProtocol,
    CoordinatorProtocol,
    ExtractorProtocol,
    IndexerProtocol,
)


class PipelineCoordinator(CoordinatorProtocol):
    """Coordinates atomic multi-store writes, crash recovery, and offline re-indexing."""

    def __init__(
        self,
        cas: ContentStoreProtocol,
        catalog: CatalogProtocol,
        extractor: ExtractorProtocol,
        chunker: ChunkerProtocol,
        indexer: IndexerProtocol,
    ):
        from old_rag.logger import get_logger
        self.logger = get_logger("pipeline.coordinator")
        self.cas = cas
        self.catalog = catalog
        self.extractor = extractor
        self.chunker = chunker
        self.indexer = indexer

    def _sync_document_entities(self, norm_doc: NormalizedDocumentJSON) -> None:
        """Persist flattened Scryer structured entities into CatalogSchema v2 document_entities."""
        if not hasattr(self.catalog, "upsert_document_entities"):
            return

        entity_rows: List[tuple] = []
        covered_sources: set[str] = set()

        for att in norm_doc.attachments:
            if att.structured_data:
                covered_sources.add(att.filename)
                for k, v in _flatten_structured_fields(att.structured_data):
                    if k not in ("extracted_text", "text_content") and not k.startswith("_"):
                        entity_rows.append((k, v, att.filename))

        if norm_doc.structured_context:
            att_names = {a.filename for a in norm_doc.attachments}
            for top_k, top_v in norm_doc.structured_context.items():
                if top_k in covered_sources:
                    continue
                if isinstance(top_v, dict) and top_k in att_names:
                    for k, v in _flatten_structured_fields(top_v):
                        if k not in ("extracted_text", "text_content") and not k.startswith("_"):
                            entity_rows.append((k, v, top_k))
                else:
                    for k, v in _flatten_structured_fields(top_v, prefix=str(top_k)):
                        if k not in ("extracted_text", "text_content") and not k.startswith("_"):
                            entity_rows.append((k, v, ""))

        if entity_rows:
            self.catalog.upsert_document_entities(norm_doc.doc_id, entity_rows)

    def ingest_payload(self, payload: RawIngestPayload) -> None:
        """Execute full ingestion pipeline with two-phase outbox commit."""
        # 1. Deterministic extraction & conditional VLM
        norm_doc = self.extractor.extract(payload)

        # 2. Persist to CAS tiers
        text_cas_ref = self.cas.put_text(norm_doc.doc_id, norm_doc)
        raw_msg_cas_ref = None
        if payload.raw_msg_bytes:
            raw_msg_cas_ref = self.cas.put_msg(norm_doc.doc_id, payload.raw_msg_bytes)

        # 3. Chunk normalized document
        chunks = self.chunker.chunk(norm_doc)
        chunk_ids = [c.chunk_id for c in chunks]

        # Calculate exact storage byte size
        cas_bytes = self.cas.get_document_storage_bytes(norm_doc.doc_id) if hasattr(self.cas, "get_document_storage_bytes") else 0
        chunk_content_bytes = sum(len(c.content.encode("utf-8")) for c in chunks)
        doc_byte_size = cas_bytes + chunk_content_bytes

        # Upsert document lineage in catalog FIRST (satisfies foreign key in blob_references)
        self.catalog.upsert_document(
            doc_id=norm_doc.doc_id,
            source_type=norm_doc.source_type.value,
            original_path=norm_doc.original_path,
            entry_id=norm_doc.entry_id,
            internet_message_id=norm_doc.internet_message_id,
            conversation_id=norm_doc.conversation_id,
            sender=norm_doc.sender,
            subject=norm_doc.subject,
            folder=norm_doc.folder,
            received_ts_utc=norm_doc.received_ts_utc.isoformat() if norm_doc.received_ts_utc else None,
            text_cas_ref=text_cas_ref,
            raw_msg_cas_ref=raw_msg_cas_ref,
            account_id=norm_doc.account_id,
            byte_size=doc_byte_size,
            chunk_ids=chunk_ids,
        )
        self._sync_document_entities(norm_doc)

        # Persist chat_meta if present
        if norm_doc.chat_meta and hasattr(self.catalog, "upsert_chat_messages"):
            sent_ts = norm_doc.received_ts_utc or norm_doc.extracted_at_utc
            msg_item = ChatMessageItem(
                message_id=norm_doc.entry_id or norm_doc.doc_id,
                doc_id=norm_doc.doc_id,
                account_id=norm_doc.chat_meta.account_id,
                adapter_type=norm_doc.chat_meta.adapter_type,
                room_id=norm_doc.chat_meta.room_id,
                room_name=norm_doc.chat_meta.room_name,
                is_dm=norm_doc.chat_meta.is_dm,
                sender_id=norm_doc.chat_meta.sender_id,
                sender_name=norm_doc.chat_meta.sender_name,
                thread_root_id=norm_doc.chat_meta.thread_root_id,
                sent_ts_utc=sent_ts,
                body_text=norm_doc.clean_body_text,
                reactions=norm_doc.chat_meta.reactions,
            )
            self.catalog.upsert_chat_messages([msg_item])
            if hasattr(self.catalog, "upsert_chat_room"):
                self.catalog.upsert_chat_room(
                    ChatRoomSummary(
                        room_id=norm_doc.chat_meta.room_id,
                        account_id=norm_doc.chat_meta.account_id,
                        adapter_type=norm_doc.chat_meta.adapter_type,
                        room_name=norm_doc.chat_meta.room_name,
                        is_dm=norm_doc.chat_meta.is_dm,
                        last_message_ts_utc=sent_ts,
                        last_snippet=norm_doc.clean_body_text[:120],
                    )
                )

        # Record blob references in catalog and persist to CAS Tier 2
        copy_blobs = True
        if payload.source_type == SourceType.FILE:
            cas_cfg = getattr(self.cas, "config", None)
            if cas_cfg and not getattr(cas_cfg, "copy_storage_files", False):
                copy_blobs = False

        for att in norm_doc.attachments:
            raw_att = next((a for a in payload.attachments if a.filename == att.filename), None)
            size = raw_att.size_bytes if raw_att else len(att.blob_hash)
            if copy_blobs and raw_att and raw_att.data:
                self.cas.put_blob(att.blob_hash, raw_att.data)
            self.catalog.record_blob_ref(att.blob_hash, norm_doc.doc_id, size)

        # 4. Outbox journal write (PENDING)
        outbox_id = self.catalog.enqueue_outbox(
            op=OutboxOp.INGEST,
            doc_id=norm_doc.doc_id,
            chunk_ids=chunk_ids,
        )

        # 5. Mutate lexical and vector indexes
        self.indexer.upsert_chunks(chunks)

        # 6. Mark outbox COMMITTED
        self.catalog.mark_outbox_committed(outbox_id)
        self.logger.info(
            "Ingested document doc_id=%s source_type=%s chunks=%d byte_size=%d",
            norm_doc.doc_id,
            norm_doc.source_type.value,
            len(chunks),
            doc_byte_size,
        )

    def _execute_delete_outbox(self, doc_id: str, chunk_ids: List[str], outbox_id: Optional[int] = None) -> None:
        """Execute atomic multi-store deletion without modifying source files."""
        # 1. Delete chunks from FTS5 & LanceDB
        if hasattr(self.indexer, "delete_document_chunks"):
            self.indexer.delete_document_chunks(doc_id, chunk_ids)
        elif hasattr(self.indexer, "delete_chunks"):
            self.indexer.delete_chunks(chunk_ids)

        # 2. Delete CAS text and msg
        self.cas.delete_text(doc_id)
        self.cas.delete_msg(doc_id)

        # 3. Hard delete from catalog and prune zero-ref blobs
        if hasattr(self.catalog, "delete_document_hard"):
            pruned_blobs = self.catalog.delete_document_hard(doc_id)
            for b in pruned_blobs:
                self.cas.delete_blob(b)
        else:
            if hasattr(self.catalog, "_get_connection"):
                conn = self.catalog._get_connection()
                with self.catalog._lock:
                    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                    conn.commit()

        # 4. Mark outbox COMMITTED
        if outbox_id is not None:
            self.catalog.mark_outbox_committed(outbox_id)

    def delete_document(self, doc_id: str) -> int:
        """Enqueue OutboxOp.DELETE and execute deletion across all internal stores."""
        doc = self.catalog.get_document(doc_id)
        if not doc:
            return 0
        chunk_ids: List[str] = []
        if "chunk_ids_json" in doc and doc["chunk_ids_json"]:
            try:
                chunk_ids = json.loads(doc["chunk_ids_json"])
            except Exception:
                pass
        outbox_id = self.catalog.enqueue_outbox(OutboxOp.DELETE, doc_id, chunk_ids)
        self._execute_delete_outbox(doc_id, chunk_ids, outbox_id)
        return 1

    def purge_account_data(self, account_id: str, source_type: Optional[str] = None) -> Dict[str, Any]:
        """Purge all indexed documents, chunks, CAS files, chat messages, and calendar events for an account.

        Source File Safety Invariant: NEVER deletes or modifies source files at original_path.
        """
        conn = self.catalog._get_connection()
        doc_ids: List[str] = []

        with self.catalog._lock:
            # Match account documents
            if account_id in ("default_outlook", "outlook"):
                query = (
                    "SELECT doc_id FROM documents WHERE account_id = ? "
                    "OR account_id = 'outlook' OR account_id = 'default_outlook' "
                    "OR (account_id IS NULL AND source_type = 'EMAIL') "
                    "OR original_path LIKE 'Outlook://%' OR original_path LIKE 'outlook://%'"
                )
                params: List[Any] = [account_id]
            elif source_type == "CALENDAR":
                query = (
                    "SELECT doc_id FROM documents WHERE (account_id = ? "
                    "OR original_path LIKE 'calendar://' || ? || '/%') AND source_type = 'CALENDAR'"
                )
                params = [account_id, account_id]
            else:
                query = "SELECT doc_id FROM documents WHERE account_id = ?"
                params = [account_id]

            cur = conn.execute(query, params)
            doc_ids = [r["doc_id"] for r in cur.fetchall()]

        deleted_docs = 0
        for did in doc_ids:
            deleted_docs += self.delete_document(did)

        # Purge associated chat messages and rooms
        with self.catalog._lock:
            conn.execute("DELETE FROM chat_messages WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM chat_rooms WHERE account_id = ?", (account_id,))

            # Purge calendar events if table exists
            cur = conn.execute("PRAGMA table_info(calendar_events)")
            cols = {row["name"] for row in cur.fetchall()}
            if "calendar_id" in cols:
                conn.execute(
                    "DELETE FROM calendar_events WHERE account_id = ? OR calendar_id = ?",
                    (account_id, account_id),
                )
            else:
                conn.execute("DELETE FROM calendar_events WHERE account_id = ?", (account_id,))

            # Reset or delete sync cursors
            conn.execute(
                "DELETE FROM sync_cursors WHERE account_id = ? OR source_key LIKE ?",
                (account_id, f"%{account_id}%"),
            )
            conn.commit()

        if hasattr(self.catalog, "checkpoint_and_vacuum"):
            self.catalog.checkpoint_and_vacuum()

        self.logger.info("Purged account data account_id=%s deleted_docs=%d", account_id, deleted_docs)
        return {
            "success": True,
            "account_id": account_id,
            "deleted_docs_count": deleted_docs,
        }

    def process_outbox(self) -> int:
        """Startup crash recovery: replay any PENDING outbox records."""
        pending = self.catalog.get_pending_outbox()
        count = 0
        for record in pending:
            if record.op == OutboxOp.INGEST:
                norm_doc = self.cas.get_text(record.doc_id)
                if norm_doc:
                    self._sync_document_entities(norm_doc)
                    chunks = self.chunker.chunk(norm_doc)
                    self.indexer.upsert_chunks(chunks)
                    self.catalog.mark_outbox_committed(record.outbox_id)
                    count += 1
            elif record.op == OutboxOp.DELETE:
                self._execute_delete_outbox(record.doc_id, record.chunk_ids, record.outbox_id)
                count += 1
        return count

    def reindex_all(self) -> int:
        """100% offline rebuild reading from CAS Tier 1 without Outlook or VLM."""
        if hasattr(self.catalog, "_get_connection"):
            conn = self.catalog._get_connection()
            cur = conn.execute("SELECT doc_id FROM documents WHERE is_deleted = 0")
            doc_ids = [r["doc_id"] for r in cur.fetchall()]
        else:
            doc_ids = []

        count = 0
        for did in doc_ids:
            norm_doc = self.cas.get_text(did)
            if norm_doc:
                self._sync_document_entities(norm_doc)
                chunks = self.chunker.chunk(norm_doc)
                self.indexer.upsert_chunks(chunks)

                # Re-populate chat_messages if chat_meta present
                if norm_doc.chat_meta and hasattr(self.catalog, "upsert_chat_messages"):
                    sent_ts = norm_doc.received_ts_utc or norm_doc.extracted_at_utc
                    msg_item = ChatMessageItem(
                        message_id=norm_doc.entry_id or norm_doc.doc_id,
                        doc_id=norm_doc.doc_id,
                        account_id=norm_doc.chat_meta.account_id,
                        adapter_type=norm_doc.chat_meta.adapter_type,
                        room_id=norm_doc.chat_meta.room_id,
                        room_name=norm_doc.chat_meta.room_name,
                        is_dm=norm_doc.chat_meta.is_dm,
                        sender_id=norm_doc.chat_meta.sender_id,
                        sender_name=norm_doc.chat_meta.sender_name,
                        thread_root_id=norm_doc.chat_meta.thread_root_id,
                        sent_ts_utc=sent_ts,
                        body_text=norm_doc.clean_body_text,
                        reactions=norm_doc.chat_meta.reactions,
                    )
                    self.catalog.upsert_chat_messages([msg_item])
                count += 1
        return count

