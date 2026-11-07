"""protocols.py - Immutable Phase 0 typing.Protocol interfaces."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable
from old_rag.contracts import (
    DocumentChunk,
    NormalizedDocumentJSON,
    OutboxOp,
    OutboxRecord,
    RawIngestPayload,
    SearchQuery,
    SearchResult,
)


@runtime_checkable
class ContentStoreProtocol(Protocol):
    """3-Tier Envelope-Encrypted Content Addressable Storage interface."""

    def put_text(self, doc_id: str, payload: NormalizedDocumentJSON) -> str:
        """Store canonical extraction JSON (Tier 1). Returns storage reference."""
        ...

    def get_text(self, doc_id: str) -> Optional[NormalizedDocumentJSON]:
        """Retrieve canonical extraction JSON (Tier 1)."""
        ...

    def put_blob(self, blob_hash: str, data: bytes) -> str:
        """Store deduplicated binary attachment/image (Tier 2). Returns storage reference."""
        ...

    def get_blob(self, blob_hash: str) -> Optional[bytes]:
        """Retrieve binary blob (Tier 2)."""
        ...

    def put_msg(self, doc_id: str, msg_bytes: bytes) -> str:
        """Store raw .msg email archive (Tier 3). Returns storage reference."""
        ...

    def get_msg(self, doc_id: str) -> Optional[bytes]:
        """Retrieve raw .msg email archive (Tier 3)."""
        ...

    def delete_text(self, doc_id: str) -> bool:
        """Delete text archive tier for doc_id."""
        ...

    def delete_blob(self, blob_hash: str) -> bool:
        """Delete blob from Tier 2."""
        ...

    def delete_msg(self, doc_id: str) -> bool:
        """Delete raw .msg archive from Tier 3."""
        ...


@runtime_checkable
class CatalogProtocol(Protocol):
    """Sole interface to %LOCALAPPDATA%\\OldRAG\\catalog.db."""

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve document metadata by doc_id."""
        ...

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
        """Upsert document lineage and metadata in catalog."""
        ...

    def record_blob_ref(self, blob_hash: str, doc_id: str, size_bytes: int) -> int:
        """Record or increment reference count for attachment blob. Returns new ref_count."""
        ...

    def release_blob_ref(self, blob_hash: str, doc_id: str) -> int:
        """Decrement reference count for blob. Returns new ref_count."""
        ...

    def get_vlm_cache(self, blob_hash: str) -> Optional[str]:
        """Retrieve cached VLM Markdown extraction by image/blob SHA-256."""
        ...

    def put_vlm_cache(self, blob_hash: str, markdown: str) -> None:
        """Store VLM Markdown extraction in catalog cache."""
        ...

    def is_vlm_skipped(self, blob_hash: str) -> bool:
        """Check if blob is on the high-frequency skip list (signatures, icons)."""
        ...

    def add_vlm_skip_hash(self, blob_hash: str, reason: str = "signature") -> None:
        """Mark a hash to skip future VLM inference."""
        ...

    def enqueue_outbox(self, op: OutboxOp, doc_id: str, chunk_ids: List[str]) -> int:
        """Enqueue outbox transaction record with PENDING status. Returns outbox_id."""
        ...

    def mark_outbox_committed(self, outbox_id: int) -> None:
        """Mark outbox transaction record as COMMITTED."""
        ...

    def get_pending_outbox(self) -> List[OutboxRecord]:
        """Retrieve all uncommitted outbox entries for startup crash recovery."""
        ...

    def prune_orphans(self, purge_source_deleted: bool) -> List[str]:
        """Scan catalog for 0-ref blobs and return list of pruned hashes."""
        ...


@runtime_checkable
class ModelResolverProtocol(Protocol):
    """Resolves local weights or downloads with checksum verification."""

    def resolve_vlm_pair(self) -> Tuple[str, str]:
        """Return absolute local paths to (model.gguf, mmproj.gguf)."""
        ...

    def resolve_embedder_model(self) -> Tuple[str, str]:
        """Return absolute local path to ONNX model dir and tokenizer.json."""
        ...


@runtime_checkable
class VLMClientProtocol(Protocol):
    """Dispatches visual extraction requests to the isolated VLM worker subprocess."""

    def extract_image_markdown(self, image_bytes: bytes, filename: str) -> str:
        """Convert visual document/diagram bytes into structured Markdown tables/text."""
        ...


@runtime_checkable
class ExtractorProtocol(Protocol):
    """Deterministic document parser interface with conditional VLM gating."""

    def extract(self, payload: RawIngestPayload) -> NormalizedDocumentJSON:
        """Convert raw ingest payload into normalized extracted text and attachment metadata."""
        ...

    def can_extract(self, filename: str) -> bool:
        """Return True if extractor supports the given file extension."""
        ...


@runtime_checkable
class ChunkerProtocol(Protocol):
    """Parent-safe chunking with WordPiece token budgeting."""

    def chunk(self, doc: NormalizedDocumentJSON) -> List[DocumentChunk]:
        """Convert normalized document into token-bounded chunks with lineage."""
        ...


@runtime_checkable
class IndexerProtocol(Protocol):
    """Atomic multi-index mutator for SQLite FTS5 and LanceDB."""

    def upsert_chunks(self, chunks: List[DocumentChunk], embeddings: List[List[float]]) -> None:
        """Idempotently insert/update chunks in lexical FTS5 and LanceDB vector stores."""
        ...

    def delete_chunks(self, chunk_ids: List[str]) -> None:
        """Delete chunks from both lexical and vector indexes."""
        ...

    def update_folder(self, doc_id: str, new_folder: str) -> None:
        """Update folder predicate metadata across indexes without re-embedding."""
        ...


@runtime_checkable
class SearchEngineProtocol(Protocol):
    """High-precision hybrid RRF search engine."""

    def search(self, query: SearchQuery) -> List[SearchResult]:
        """Execute unified lexical and prefiltered vector search with float64 RRF fusion."""
        ...


@runtime_checkable
class CoordinatorProtocol(Protocol):
    """Single-writer coordinator for pipeline, outbox recovery, and offline re-indexing."""

    def ingest_payload(self, payload: RawIngestPayload) -> None:
        """Run full ingestion pipeline: extract -> CAS -> chunk -> embed -> outbox commit."""
        ...

    def process_outbox(self) -> int:
        """Process pending outbox tasks. Returns number of tasks completed."""
        ...

    def reindex_all(self) -> int:
        """Perform 100% offline rebuild reading from CAS Tier 1 without Outlook or VLM."""
        ...
