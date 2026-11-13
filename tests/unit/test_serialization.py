"""test_serialization.py - Comprehensive tests for datetime and type JSON serialization."""

import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

from old_rag.contracts import (
    DocumentChunk,
    NormalizedDocumentJSON,
    OutboxOp,
    OutboxRecord,
    OutboxStatus,
    RawIngestPayload,
    RelevanceTier,
    SearchQuery,
    SearchResult,
    SourceType,
    StorageConfig,
    safe_json_dumps,
    safe_json_loads,
)
from old_rag.ui.app import BridgeAPI


def test_search_result_model_dump_json_serializable():
    """Verify SearchResult with datetime received_ts_utc serializes with standard json.dumps."""
    now = datetime(2026, 11, 7, 14, 30, 0, tzinfo=timezone.utc)
    sr = SearchResult(
        chunk_id="chunk_1",
        doc_id="doc_1",
        content="Test email content",
        source_type=SourceType.EMAIL,
        sender="sender@example.com",
        folder="Inbox",
        received_ts_utc=now,
        rrf_score=0.0328,
        normalized_rrf=0.85,
        relevance_tier=RelevanceTier.HIGH,
    )

    dumped = sr.model_dump()
    # received_ts_utc must be a string in dumped dict
    assert isinstance(dumped["received_ts_utc"], str)
    assert dumped["received_ts_utc"].startswith("2026-11-07")

    # json.dumps must not raise TypeError: Object of type datetime is not JSON serializable
    json_str = json.dumps(dumped)
    assert "2026-11-07" in json_str

    # safe_json_dumps directly on SearchResult object
    direct_json = safe_json_dumps(sr)
    assert "2026-11-07" in direct_json


def test_safe_json_dumps_all_types():
    """Verify safe_json_dumps handles datetime, date, Enum, Path, and custom nested dicts."""
    test_dict = {
        "timestamp": datetime.now(timezone.utc),
        "date_only": date(2026, 11, 7),
        "tier": RelevanceTier.MEDIUM,
        "source": SourceType.EMAIL,
        "path": Path("C:/projects/old_rag"),
        "nested": {
            "created_at": datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc),
            "op": OutboxOp.INGEST,
        },
        "items": [1, 2.5, "test", True, None],
    }

    serialized = safe_json_dumps(test_dict)
    assert isinstance(serialized, str)

    parsed = safe_json_loads(serialized)
    assert parsed["tier"] == "MEDIUM"
    assert parsed["source"] == "EMAIL"
    assert "2026" in parsed["timestamp"]
    assert "2026-11-07" in parsed["date_only"]
    assert parsed["nested"]["op"] == "INGEST"


def test_all_contract_dtos_model_dump():
    """Ensure all contract DTOs produce JSON-serializable dictionaries."""
    now = datetime.now(timezone.utc)

    payload = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_test",
        original_path="mailbox://test",
        received_ts_utc=now,
    )
    assert isinstance(json.dumps(payload.model_dump()), str)

    chunk = DocumentChunk(
        chunk_id="c_1",
        doc_id="d_1",
        chunk_index=0,
        source_type=SourceType.EMAIL,
        received_ts_utc=now,
        content="test",
        token_count=1,
    )
    assert isinstance(json.dumps(chunk.model_dump()), str)

    query = SearchQuery(query_text="USAA", as_of_date_utc=now)
    assert isinstance(json.dumps(query.model_dump()), str)

    outbox = OutboxRecord(
        outbox_id=1,
        op=OutboxOp.INGEST,
        doc_id="d_1",
        chunk_ids=["c_1"],
        status=OutboxStatus.COMMITTED,
        created_at_utc=now,
    )
    assert isinstance(json.dumps(outbox.model_dump()), str)


def test_bridge_api_search_returns_json_serializable():
    """Verify BridgeAPI search output can be encoded by pywebview / json.dumps."""
    api = BridgeAPI()
    res = api.search({"query_text": "USAA"})
    # pywebview internally does json.dumps on the returned dictionary
    serialized = json.dumps(res)
    assert isinstance(serialized, str)
    assert "results" in res

