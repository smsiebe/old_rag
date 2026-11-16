"""contracts.py - Immutable Phase 0 Pydantic DTOs, Enums, and Exception Contracts."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


# --- Custom JSON Serialization Infrastructure ---

def json_default_serializer(obj: Any) -> Any:
    """Fallback serializer ensuring datetimes, paths, enums, and models serialize cleanly to JSON."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """Robust JSON serializer that guarantees standard Python JSON serialization without TypeError."""
    if "default" not in kwargs:
        kwargs["default"] = json_default_serializer
    return json.dumps(obj, **kwargs)


def safe_json_loads(s: Union[str, bytes], **kwargs) -> Any:
    """Parse JSON strings or bytes."""
    return json.loads(s, **kwargs)


class SovereignBaseModel(BaseModel):
    """Base model enforcing JSON-safe serialization standards across all DTOs."""

    def model_dump(self, *args, **kwargs) -> Dict[str, Any]:
        if "mode" not in kwargs:
            kwargs["mode"] = "json"
        return super().model_dump(*args, **kwargs)


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
    CALENDAR = "CALENDAR"
    CHAT = "CHAT"


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

class StorageConfig(SovereignBaseModel):
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


class ModelConfig(SovereignBaseModel):
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

class IngestSource(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    source_type: SourceType
    locator: str  # Original file path or MAPI EntryID
    as_of_date_utc: Optional[datetime] = None


class RawAttachment(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    data: bytes
    size_bytes: int


class ChatIngestMeta(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    adapter_type: str
    room_id: str
    room_name: str
    is_dm: bool = False
    sender_id: str = ""
    sender_name: str = ""
    thread_root_id: Optional[str] = None
    message_ids: List[str] = Field(default_factory=list)
    reactions: Dict[str, List[str]] = Field(default_factory=dict)


class CalendarIngestMeta(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    account_id: Optional[str] = None
    location: Optional[str] = None
    start_ts_utc: datetime
    end_ts_utc: datetime
    is_all_day: bool = False
    organizer: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)
    status: str = "CONFIRMED"


class RawIngestPayload(SovereignBaseModel):
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
    account_id: Optional[str] = None
    chat_meta: Optional[ChatIngestMeta] = None
    calendar_meta: Optional[CalendarIngestMeta] = None


# --- Canonical Extracted Text DTO (CAS Tier 1) ---

class ExtractedAttachmentMeta(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    blob_hash: str
    vlm_markdown: Optional[str] = None
    template_key: str = "default"
    structured_data: Dict[str, Any] = Field(default_factory=dict)
    document_metadata: Dict[str, Any] = Field(default_factory=dict)
    unstructured_annotations: List[str] = Field(default_factory=list)
    route_telemetry: Dict[str, Any] = Field(default_factory=dict)


class NormalizedDocumentJSON(SovereignBaseModel):
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
    structured_context: Dict[str, Any] = Field(default_factory=dict)
    document_metadata: Dict[str, Any] = Field(default_factory=dict)
    unstructured_annotations: List[str] = Field(default_factory=list)
    extracted_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    account_id: Optional[str] = None
    chat_meta: Optional[ChatIngestMeta] = None
    calendar_meta: Optional[CalendarIngestMeta] = None


# --- Chunking & Search DTOs ---

class DocumentChunk(SovereignBaseModel):
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


class SearchQuery(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    query_text: str
    top_k: int = 50
    as_of_date_utc: Optional[datetime] = None
    sender: Optional[str] = None
    folder: Optional[str] = None
    source_type: Optional[SourceType] = None
    source_types: Optional[List[SourceType]] = None
    account_id: Optional[str] = None


class SearchResult(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    content: str
    source_type: SourceType
    subject: Optional[str] = None
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
    cosine_similarity: Optional[float] = None
    confidence_score: Optional[float] = None
    exact_identifier_match: bool = False


# --- Outbox / Replication DTOs ---

class OutboxRecord(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    outbox_id: int
    op: OutboxOp
    doc_id: str
    chunk_ids: List[str]
    status: OutboxStatus
    created_at_utc: datetime
    error_message: Optional[str] = None


# --- Multi-Account, Mail Client & Calendar DTOs ---

class EmailAccountConfig(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    account_type: str = "imap_smtp"  # "outlook_mapi", "imap_smtp", "exchange_owa"
    email_address: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    username: str = ""
    password: str = ""
    server_url: str = ""
    folders: List[str] = Field(default_factory=lambda: ["ALL"])
    date_range: str = "1yr"
    enabled: bool = True
    created_at_utc: Optional[datetime] = None


class CalendarEvent(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    account_id: Optional[str] = None
    calendar_id: Optional[str] = None
    calendar_color: Optional[str] = "#339af0"
    source_system: Optional[str] = "outlook"
    doc_id: Optional[str] = None
    source_type: SourceType = SourceType.CALENDAR
    subject: str = ""
    location: Optional[str] = None
    start_ts_utc: datetime
    end_ts_utc: datetime
    is_all_day: bool = False
    organizer: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)
    body_text: str = ""
    status: str = "CONFIRMED"


class OutgoingEmail(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    to: List[str]
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class EmailMessageSummary(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    account_id: Optional[str] = None
    sender: Optional[str] = None
    subject: Optional[str] = None
    folder: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    snippet: str = ""
    is_read: bool = True
    is_starred: bool = False
    has_attachments: bool = False


# --- Chat DTOs ---

class ChatAdapterCapability(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    supports_dms: bool = True
    supports_rooms: bool = True
    supports_threads: bool = True
    supports_streaming: bool = False
    supports_send: bool = True


class ChatPluginDescriptor(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    adapter_type: str
    display_name: str
    version: str = "1.0.0"
    capabilities: ChatAdapterCapability = Field(default_factory=ChatAdapterCapability)
    config_schema: Dict[str, Any] = Field(default_factory=dict)
    is_builtin: bool = True


class CalendarAccountConfig(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    connector_type: str = "outlook_mapi"  # outlook_mapi, google_calendar, exchange, confluence, caldav_ics
    color: str = "#339af0"
    url: Optional[str] = None
    username: Optional[str] = None
    credentials: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sync_interval_mins: int = 15
    created_at_utc: Optional[datetime] = None


class ChatAccountConfig(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    adapter_type: str
    server_url: str = ""
    username: str = ""
    credentials: Dict[str, str] = Field(default_factory=dict)
    lookback_days: Optional[int] = 30
    include_dms: bool = True
    rooms: List[str] = Field(default_factory=list)
    poll_interval_sec: float = 30.0
    realtime_enabled: bool = True
    enabled: bool = True
    created_at_utc: Optional[datetime] = None


class ChatRoomSummary(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    room_id: str
    account_id: str
    adapter_type: str
    room_name: str
    is_dm: bool
    participant_count: int = 0
    unread_count: int = 0
    last_message_ts_utc: Optional[datetime] = None
    last_snippet: str = ""
    subscribed: bool = True


class ChatMessageItem(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    doc_id: str
    account_id: str
    adapter_type: str
    room_id: str
    room_name: str
    is_dm: bool
    sender_id: str
    sender_name: str
    thread_root_id: Optional[str] = None
    sent_ts_utc: datetime
    body_text: str
    reactions: Dict[str, List[str]] = Field(default_factory=dict)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class OutgoingChatMessage(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    room_id: str
    body_text: str
    thread_root_id: Optional[str] = None
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


# --- Sync & Quota DTOs ---

class SyncCursorRecord(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_key: str
    source_type: SourceType
    account_id: Optional[str] = None
    high_watermark_cursor: Optional[str] = None
    low_watermark_utc: Optional[datetime] = None
    configured_lookback_days: Optional[int] = None
    last_sync_utc: Optional[datetime] = None
    status: str = "IDLE"
    last_error: Optional[str] = None


class SyncPolicyConfig(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_key: str
    source_type: SourceType
    account_id: Optional[str] = None
    lookback_days: Optional[int] = 30
    realtime_enabled: bool = True
    poll_interval_sec: float = 30.0
    enabled: bool = True


class DashboardWindowConfig(SovereignBaseModel):
    id: str
    type: str  # "chat", "mail", "calendar", "search"
    title: str = ""
    target_id: Optional[str] = None
    x: int = 100
    y: int = 100
    width: int = 480
    height: int = 520
    is_open: bool = True
    auto_pop: bool = False
    sound_enabled: bool = False


class DashboardLayoutConfig(SovereignBaseModel):
    windows: List[DashboardWindowConfig] = Field(default_factory=list)


class QuotaPolicyType(str, Enum):
    OLDEST_FIRST = "OLDEST_FIRST"
    LRU_ACCESS = "LRU_ACCESS"
    LOWEST_CENTRALITY = "LOWEST_CENTRALITY"
    COMPOSITE_SCORE = "COMPOSITE_SCORE"


class DataTypeQuotaConfig(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    max_bytes: int = 0
    soft_warning_ratio: float = 0.85
    policy: QuotaPolicyType = QuotaPolicyType.COMPOSITE_SCORE
    preserve_starred: bool = True
    preserve_pinned: bool = True
    preserve_recent_days: int = 14


class EvictionCandidateSummary(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    source_type: SourceType
    subject: Optional[str] = None
    sender: Optional[str] = None
    received_ts_utc: Optional[datetime] = None
    byte_size: int
    estimated_freed_bytes: int
    retention_score: float
    reason: str


class DataTypeQuotaStatus(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    used_bytes: int
    max_bytes: int
    usage_ratio: float
    document_count: int
    chunk_count: int
    shared_blob_bytes: int
    exclusive_blob_bytes: int
    policy: QuotaPolicyType
    over_soft_limit: bool
    over_hard_limit: bool


class QuotaPurgeResult(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    dry_run: bool
    evicted_doc_ids: List[str]
    freed_bytes: int
    used_bytes_before: int
    used_bytes_after: int
    candidates: List[EvictionCandidateSummary] = Field(default_factory=list)


# --- UI Bridge & Event DTOs ---

class BridgeErrorCode(str, Enum):
    OK = "OK"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BridgeResponseEnvelope(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    status: int
    data: Any = None
    error: Optional[str] = None
    error_code: BridgeErrorCode = BridgeErrorCode.OK


class UIEventDTO(SovereignBaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    event_type: str
    source_type: Optional[SourceType] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

