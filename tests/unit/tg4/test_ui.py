"""test_ui.py - Unit tests for pywebview Bridge and Schema Validation."""

import json
from pathlib import Path

from old_rag.contracts import RelevanceTier, SearchResult, SourceType
from old_rag.ui.app import BridgeAPI


def test_bridge_api_mock_call():
    api = BridgeAPI(pipe_name="\\\\.\\pipe\\NonExistentTestPipe")
    # Calling non-existent pipe should gracefully return error dict rather than crashing
    res = api.get_status()
    assert "error" in res


def test_bridge_schema_matches_contracts():
    schema_path = Path(__file__).parent.parent.parent.parent / "src" / "old_rag" / "bridge_schema.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Verify SearchResult model fields are accounted for in bridge schema
    item_def = schema["definitions"]["SearchResultItem"]["properties"]
    assert "chunk_id" in item_def
    assert "rrf_score" in item_def
    assert "relevance_tier" in item_def

    # Verify a SearchResult serializes cleanly
    sr = SearchResult(
        chunk_id="c_1",
        doc_id="d_1",
        content="test content",
        source_type=SourceType.EMAIL,
        rrf_score=0.0328,
        normalized_rrf=0.99,
        relevance_tier=RelevanceTier.HIGH,
    )
    dumped = sr.model_dump()
    for req_field in schema["definitions"]["SearchResultItem"]["required"]:
        assert req_field in dumped

