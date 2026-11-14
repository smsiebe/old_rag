"""coordinator.py - Single-Writer Outbox Coordinator & Offline Re-indexer."""

from __future__ import annotations

from typing import List, Optional

from old_rag.contracts import (
    NormalizedDocumentJSON,
    OutboxOp,
    RawIngestPayload,
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
        )
        self._sync_document_entities(norm_doc)

        # Record blob references in catalog and persist to CAS Tier 2
        for att in norm_doc.attachments:
            raw_att = next((a for a in payload.attachments if a.filename == att.filename), None)
            size = raw_att.size_bytes if raw_att else len(att.blob_hash)
            if raw_att and raw_att.data:
                self.cas.put_blob(att.blob_hash, raw_att.data)
            self.catalog.record_blob_ref(att.blob_hash, norm_doc.doc_id, size)

        # 3. Chunk normalized document
        chunks = self.chunker.chunk(norm_doc)
        chunk_ids = [c.chunk_id for c in chunks]

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
                self.indexer.delete_chunks(record.chunk_ids)
                self.catalog.mark_outbox_committed(record.outbox_id)
                count += 1
        return count

    def reindex_all(self) -> int:
        """100% offline rebuild reading from CAS Tier 1 without Outlook or VLM."""
        # Query all documents from catalog
        import sqlite3
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
                count += 1
        return count

