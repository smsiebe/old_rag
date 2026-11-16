"""protocols.py - Immutable Phase 0 typing.Protocol interfaces."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable
from old_rag.contracts import (
    ChatAccountConfig,
    ChatMessageItem,
    ChatPluginDescriptor,
    ChatRoomSummary,
    DataTypeQuotaStatus,
    DocumentChunk,
    EvictionCandidateSummary,
    NormalizedDocumentJSON,
    OutboxOp,
    OutboxRecord,
    OutgoingChatMessage,
    QuotaPurgeResult,
    RawIngestPayload,
    SearchQuery,
    SearchResult,
    SourceType,
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
        account_id: Optional[str] = None,
        byte_size: int = 0,
        chunk_ids: Optional[List[str]] = None,
        is_starred: bool = False,
        is_pinned: bool = False,
        is_read: bool = True,
    ) -> None:
        """Upsert document lineage and metadata in catalog. Thread-safe via SQLite WAL mode."""
        ...

    def record_blob_ref(self, blob_hash: str, doc_id: str, size_bytes: int) -> int:
        """Record or increment reference count for attachment blob. Returns new ref_count."""
        ...

    def release_blob_ref(self, blob_hash: str, doc_id: str) -> int:
        """Decrement reference count for blob. Returns new ref_count."""
        ...

    def get_vlm_cache(
        self,
        blob_hash: str,
        template_key: str = "default",
        schema_hash: str = "",
    ) -> Optional[str]:
        """Retrieve cached VLM Markdown/context by blob SHA-256, template key, and schema hash."""
        ...

    def put_vlm_cache(
        self,
        blob_hash: str,
        markdown: str,
        template_key: str = "default",
        schema_hash: str = "",
        structured_json: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> None:
        """Store VLM Markdown and structured context in catalog cache."""
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

    def upsert_chunks(self, chunks: List[DocumentChunk], embeddings: Optional[List[List[float]]] = None) -> None:
        """Idempotently insert/update chunks in lexical FTS5 and LanceDB vector stores.

        Concurrency: Serialized by single-writer outbox queue; safe with concurrent readers.
        """
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
        """Run full ingestion pipeline: extract -> CAS -> chunk -> embed -> outbox commit.

        Concurrency: Serialized gate for ingestion transactions.
        """
        ...

    def process_outbox(self) -> int:
        """Process pending outbox tasks. Returns number of tasks completed."""
        ...

    def reindex_all(self) -> int:
        """Perform 100% offline rebuild reading from CAS Tier 1 without Outlook or VLM."""
        ...


# --- v3 Extensible Chat, Sync, Quota & Bridge Protocols ---

@runtime_checkable
class ChatAdapterProtocol(Protocol):
    """Interface for real-time chat connector adapters (Rocket.Chat, Google Chat, Teams, Jabber/XMPP)."""

    adapter_type: str

    def get_descriptor(self) -> ChatPluginDescriptor:
        """Return plugin descriptor and declared capabilities."""
        ...

    def test_connection(self) -> Dict[str, Any]:
        """Verify network connectivity and credentials. Must raise on network/auth failure."""
        ...

    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        """Fetch accessible rooms and 1:1 direct message threads."""
        ...

    def fetch_messages(
        self,
        room_id: str,
        since_utc: Optional[datetime] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[ChatMessageItem], Optional[str]]:
        """Fetch historical or incremental messages from a room/DM honoring lookback_days."""
        ...

    def send_message(self, msg: OutgoingChatMessage) -> Dict[str, Any]:
        """Dispatch an outgoing message or thread reply to the chat network."""
        ...

    def stream_events(self, on_message: Any, stop_event: Any) -> None:
        """Stream real-time stanzas/events into the ingest queue until stop_event is set."""
        ...


@runtime_checkable
class ChatPluginRegistryProtocol(Protocol):
    """Host registry for built-in and third-party chat adapter plugins."""

    def register(self, adapter_cls: type) -> None:
        """Register a chat adapter class."""
        ...

    def discover_plugins(self, plugin_dir: Optional[Path] = None) -> List[ChatPluginDescriptor]:
        """Discover external chat plugins via entry_points and local plugin directory."""
        ...

    def list_plugins(self) -> List[ChatPluginDescriptor]:
        """List descriptors for all currently registered chat adapters."""
        ...

    def create_adapter(self, config: ChatAccountConfig) -> ChatAdapterProtocol:
        """Factory method to instantiate a configured ChatAdapterProtocol."""
        ...


@runtime_checkable
class QuotaGovernorProtocol(Protocol):
    """Hard-drive quota monitor and atomic cross-store eviction engine."""

    def get_quota_status(self, source_type: Optional[SourceType] = None) -> List[DataTypeQuotaStatus]:
        """Compute exact byte usage and quota ratios across all or filtered source types."""
        ...

    def rank_eviction_candidates(self, source_type: SourceType, bytes_to_free: int) -> List[EvictionCandidateSummary]:
        """Rank non-pinned eviction candidates according to the configured QuotaPolicyType."""
        ...

    def enforce_quota(self, source_type: SourceType, dry_run: bool = False) -> QuotaPurgeResult:
        """Enforce quota limits by purging ranked candidates across all stores and compacting disk.

        Concurrency: Serialized execution with WAL checkpointing.
        """
        ...


@runtime_checkable
class OldRAGBridgeProtocol(Protocol):
    """Pure-Python Desktop Bridge and programmatic GenAI SDK interface (zero HTTP server)."""

    def dispatch(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch a named bridge action with a payload dict, returning BridgeResponseEnvelope."""
        ...

    def dispatch_json(self, request_json: str) -> str:
        """JSON-string serialization wrapper around dispatch() for pywebview/IPC."""
        ...

    def render_desktop_html(self, inline_assets: bool = True) -> str:
        """Render self-contained single-page desktop workspace with zero external CDNs."""
        ...

    def describe_actions(self) -> Dict[str, Any]:
        """Return schema descriptions of all bridge actions."""
        ...

    def get_genai_tool_definitions(self) -> List[Dict[str, Any]]:
        """Export OpenAI/Anthropic/MCP tool calling function definitions for GenAI agents."""
        ...

    def search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute unified cross-type hybrid search."""
        ...

    def list_mail(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """List indexed mail messages with pagination and filtering."""
        ...

    def list_chat_rooms(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """List direct message and group chat rooms across connected chat accounts."""
        ...

    def list_chat_messages(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """List messages within a chat room or direct message thread."""
        ...

    def send_chat_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a message to a chat room or DM thread."""
        ...

    def list_calendar_events(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """List calendar events in a date window."""
        ...

    def get_quota_status(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Retrieve per-data-type quota consumption and eviction status."""
        ...

    def run_quota_purge(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute or preview a quota purge for a specific source type."""
        ...

    def get_sync_status(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Retrieve real-time connector sync watermarks and health status."""
        ...

    def trigger_sync(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Trigger an immediate real-time sync cycle for one or all sources."""
        ...

    def poll_events(self, since_seq: int = 0) -> Dict[str, Any]:
        """Poll the thread-safe UI event bus ring buffer since monotonic sequence seq."""
        ...

