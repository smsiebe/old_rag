"""test_ui.py - Unit tests for pywebview Bridge and CLI Surface (TG4)."""

import json
from pathlib import Path
import pytest

from old_rag.contracts import RelevanceTier, SearchResult, SourceType
from old_rag.ui.app import BridgeAPI


# --- T4.1: pywebview Bridge & Static Contract Tests ---

def test_t4_1_bridge_api_mock_call():
    api = BridgeAPI(pipe_name="\\\\.\\pipe\\NonExistentTestPipe")
    # Calling non-existent pipe should gracefully return error dict rather than crashing
    res = api.get_status()
    assert "error" in res

    res_search = api.search({"query_text": "test"})
    assert "error" in res_search


def test_t4_1_bridge_schema_matches_contracts():
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


# --- T4.2: CLI Surface Tests ---

def test_t4_2_cli_argument_parsing():
    import argparse
    from old_rag.cli import main

    # Inspect CLI command tree by importing or testing subparser actions
    import old_rag.cli as cli_mod
    # Verify presence of all required command handlers
    assert hasattr(cli_mod, "cmd_init")
    assert hasattr(cli_mod, "cmd_daemon")
    assert hasattr(cli_mod, "cmd_vlm_worker")
    assert hasattr(cli_mod, "cmd_ui")
    assert hasattr(cli_mod, "cmd_status")
    assert hasattr(cli_mod, "cmd_reindex")
    assert hasattr(cli_mod, "cmd_purge")
    assert hasattr(cli_mod, "cmd_export_key")
