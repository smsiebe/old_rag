"""contracts.py - Immutable Phase 0 Pydantic DTOs, Enums, and Exception Contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


# --- Custom Exception Contracts ---

class OldRAGError(Exception):
    """Base exception for all old_rag runtime errors."""


class ServiceUnavailableError(OldRAGError):
    """Raised when an external system (e.g. Classic Outlook MAPI) is missing or unavailable."""


class DimensionMismatchError(OldRAGError):
    """Raised when query embedding dimensions mismatch the index's configured matryoshka_dim."""


class SecurityViolationError(OldRAGError):
    """Raised when access control, pipe impersonation, or unauthenticated listener checks fail."""


class DesignExceptionError(OldRAGError):
    """Raised under Rule 3/SE governance when a task attempts unauthorized contract mutation."""


# --- Core Enumerations ---

class SourceType(str, Enum):
    EMAIL = "EMAIL"
    FILE = "FILE"


class RelevanceTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class OutboxOp(str, Enum):
    INGEST = "INGEST"
    MOVE_FOLDER = "MOVE_FOLDER"
    DELETE = "DELETE"
    REINDEX_DOC = "REINDEX_DOC"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


# --- Configuration DTOs ---

class StorageConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_data_dir: str = Field(
        default="",
        description="Path to root application data directory (defaults to %LOCALAPPDATA%\\OldRAG)"
    )
    copy_storage_emails_raw_msg: bool = Field(
        default=True,
        description="Whether to archive raw full .msg files in CAS Tier 3"
    )
    copy_storage_files: bool = Field(
        default=False,
        description="Whether to archive physical copies of indexed filesystem files in CAS"
    )
    purge_source_deleted: bool = Field(
        default=False,
        description="Whether to delete CAS archives and evict indexes when source item is deleted"
    )


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    vlm_model_id: str = Field(default="PaddleOCR-VL-1.6")
    vlm_gguf_filename: str = Field(default="paddleocr-vl-1.6-q4_k_m.gguf")
    vlm_mmproj_filename: str = Field(default="mmproj-model-f16.gguf")
    embedder_model_name: str = Field(default="nomic-ai/nomic-embed-text-v1.5")
    matryoshka_dim: int = Field(default=768)
    n_ctx: int = Field(default=4096)
    rrf_k: float = Field(default=60.0)
    pause_vlm_on_battery: bool = Field(default=True)


# --- Ingestion DTOs ---

class IngestSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    source_type: SourceType
    locator: str  # Original file path or MAPI EntryID
    as_of_date_utc: Optional[datetime] = None


class RawAttachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    data: bytes
    size_bytes: int


class RawIngestPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    doc_id: str
    original_path: str
    entry_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    sender: Optional[str] = None
    subject: Optional[str] = None
    folder: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    attachments: List[RawAttachment] = Field(default_factory=list)
    raw_msg_bytes: Optional[bytes] = None


# --- Canonical Extracted Text DTO (CAS Tier 1) ---

class ExtractedAttachmentMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    blob_hash: str
    vlm_markdown: Optional[str] = None


class NormalizedDocumentJSON(BaseModel):
    """Canonical extracted text & lineage JSON stored in archive/text/*.json.enc.

    Permits 100% offline re-indexing without touching Outlook or re-running VLM.
    """
    model_config = ConfigDict(frozen=True)

    doc_id: str
    source_type: SourceType
    original_path: str
    entry_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: List[str] = Field(default_factory=list)
    sender: Optional[str] = None
    subject: Optional[str] = None
    folder: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    clean_body_text: str = ""
    attachments: List[ExtractedAttachmentMeta] = Field(default_factory=list)
    extracted_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Chunking & Search DTOs ---

class DocumentChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    thread_id: Optional[str] = None
    chunk_index: int
    page_number: Optional[int] = None
    attachment_index: Optional[int] = None
    source_type: SourceType
    sender: Optional[str] = None
    folder: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    content: str
    token_count: int


class SearchQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    query_text: str
    top_k: int = 50
    as_of_date_utc: Optional[datetime] = None
    sender: Optional[str] = None
    folder: Optional[str] = None
    source_type: Optional[SourceType] = None


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    content: str
    source_type: SourceType
    sender: Optional[str] = None
    folder: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    original_path: Optional[str] = None
    has_archived_msg: bool = False
    lexical_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    rrf_score: float  # IEEE 754 float64 exact
    normalized_rrf: float  # Normalized against theoretical max
    relevance_tier: RelevanceTier


# --- Outbox / Replication DTOs ---

class OutboxRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    outbox_id: int
    op: OutboxOp
    doc_id: str
    chunk_ids: List[str]
    status: OutboxStatus
    created_at_utc: datetime
    error_message: Optional[str] = None

