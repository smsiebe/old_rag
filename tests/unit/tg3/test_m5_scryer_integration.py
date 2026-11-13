"""test_m5_scryer_integration.py - Milestone 5 verification tests for Deep Scryer & OldRAG integration."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image
import pytest

from old_rag.contracts import (
    ExtractedAttachmentMeta,
    NormalizedDocumentJSON,
    RawAttachment,
    RawIngestPayload,
    SearchQuery,
    SourceType,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine
from old_rag.engine.search import HybridSearchEngine
from old_rag.schema import CURRENT_SCHEMA_VERSION
from old_rag.storage.catalog import CatalogRepository
from scryer.bridge import BaseRouterBridge, BridgeResponse, TriModeBridge
from scryer.contracts import ExtractionTemplate, ScryerExtractionOptions, TriMode
from scryer.extractor import ScryerExtractor


class _StubInProcessBridge(BaseRouterBridge):
    """Minimal in-process stub implementing BaseRouterBridge for TriModeBridge."""

    def __init__(self, responses_by_prompt: Optional[Dict[str, Dict[str, Any]]] = None):
        self._config_path = None
        self._enable_privacy = True
        self._enable_savings = True
        self.calls: List[Dict[str, Any]] = []
        self.responses_by_prompt = responses_by_prompt or {}

    def is_available(self) -> bool:
        return True

    def execute_multimodal(
        self,
        task: str,
        images: Any,
        response_schema: Optional[Dict[str, Any]] = None,
        intent: Optional[str] = None,
        context: Optional[str] = None,
        execution_mode: Optional[str] = None,
        route_override: Optional[str] = None,
        prefer_local: Optional[bool] = None,
    ) -> BridgeResponse:
        self.calls.append({"task": task, "schema": response_schema, "intent": intent})
        payload = {"invoice_number": "INV-2026-8842", "vendor": "Orbit Dynamics", "total": "$1420.50"}
        for needle, override in self.responses_by_prompt.items():
            if needle in task or needle in json.dumps(response_schema or {}):
                payload = override
                break
        return BridgeResponse(
            raw_output=json.dumps(payload),
            structured_output=payload,
            intent=intent or "VISION_EXTRACTION",
            model_id="mock-vlm-v2",
            provider="local_mock",
            success=True,
        )


class _MockScryerBridge(TriModeBridge):
    """Deterministic stub TriModeBridge returning template-dependent structured outputs."""

    def __init__(self, responses_by_prompt: Optional[Dict[str, Dict[str, Any]]] = None):
        stub = _StubInProcessBridge(responses_by_prompt=responses_by_prompt)
        super().__init__(in_process=stub)
        self.calls = stub.calls


def _make_png_bytes(width: int = 128, height: int = 128) -> bytes:
    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_m5_schema_v1_to_v2_migration(tmp_path: Path):
    db_path = tmp_path / "legacy_catalog.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        INSERT INTO schema_version (version, applied_at_utc) VALUES (1, '2026-01-01T00:00:00Z');
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            original_path TEXT NOT NULL,
            entry_id TEXT,
            internet_message_id TEXT,
            conversation_id TEXT,
            sender TEXT,
            subject TEXT,
            folder TEXT,
            received_ts_utc TEXT,
            text_cas_ref TEXT,
            raw_msg_cas_ref TEXT,
            ingested_at_utc TEXT NOT NULL,
            is_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE blobs (
            blob_hash TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            ref_count INTEGER NOT NULL DEFAULT 1,
            first_seen_utc TEXT NOT NULL
        );
        CREATE TABLE blob_references (
            blob_hash TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            PRIMARY KEY (blob_hash, doc_id)
        );
        CREATE TABLE vlm_cache (
            blob_hash TEXT PRIMARY KEY,
            markdown TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        INSERT INTO vlm_cache (blob_hash, markdown, created_at_utc)
        VALUES ('abc123', '| Legacy Table |', '2026-01-01T00:00:00Z');
        CREATE TABLE vlm_skip_hashes (
            blob_hash TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT 'signature',
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            op TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            chunk_ids_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at_utc TEXT NOT NULL,
            committed_at_utc TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    repo = CatalogRepository(db_path)
    assert CURRENT_SCHEMA_VERSION == 2
    assert repo.get_vlm_cache("abc123") == "| Legacy Table |"

    repo.put_vlm_cache(
        "abc123",
        "Invoice #42",
        template_key="invoice",
        schema_hash="deadbeef",
        structured_json='{"invoice_id": "42"}',
    )
    assert repo.get_vlm_cache("abc123") == "| Legacy Table |"
    entry = repo.get_vlm_cache_entry("abc123", template_key="invoice", schema_hash="deadbeef")
    assert entry is not None
    assert entry["markdown"] == "Invoice #42"
    assert json.loads(entry["structured_json"]) == {"invoice_id": "42"}

    repo.upsert_document("doc_mig", "FILE", "C:/test.png")
    repo.upsert_document_entities("doc_mig", [("vendor", "Orbit Dynamics", "test.png")])
    entities = repo.get_document_entities("doc_mig")
    assert len(entities) == 1
    assert entities[0]["entity_value"] == "Orbit Dynamics"

    versions = [
        r["version"]
        for r in repo._get_connection().execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    ]
    assert 2 in versions


def test_m5_template_keyed_cache_invalidation(catalog_repo: CatalogRepository):
    png_bytes = _make_png_bytes()
    bridge = _MockScryerBridge(
        responses_by_prompt={
            "alpha_code": {"alpha_code": "ALPHA-001"},
            "beta_code": {"beta_code": "BETA-002"},
        }
    )
    scryer_ext = ScryerExtractor(bridge=bridge)

    tpl_a = ExtractionTemplate(
        name="tpl_alpha",
        description="Alpha extractor",
        schema_dict={
            "type": "object",
            "properties": {"alpha_code": {"type": "string"}},
            "required": ["alpha_code"],
        },
    )
    tpl_b = ExtractionTemplate(
        name="tpl_beta",
        description="Beta extractor",
        schema_dict={
            "type": "object",
            "properties": {"beta_code": {"type": "string"}},
            "required": ["beta_code"],
        },
    )

    payload = RawIngestPayload(
        doc_id="doc_tpl_cache",
        source_type=SourceType.FILE,
        original_path="C:/docs/receipt.png",
        attachments=[
            RawAttachment(
                filename="receipt.png",
                content_type="image/png",
                size_bytes=len(png_bytes),
                data=png_bytes,
            )
        ],
    )

    ext_a = DocumentExtractor(
        catalog=catalog_repo,
        scryer_extractor=scryer_ext,
        default_options=ScryerExtractionOptions(template=tpl_a, tri_mode=TriMode.PYTHON),
    )
    res_a1 = ext_a.extract(payload)
    res_a2 = ext_a.extract(payload)
    assert len(bridge.calls) == 1
    assert res_a1.attachments[0].template_key == "tpl_alpha"
    assert res_a2.attachments[0].structured_data == {"alpha_code": "ALPHA-001"}

    ext_b = DocumentExtractor(
        catalog=catalog_repo,
        scryer_extractor=scryer_ext,
        default_options=ScryerExtractionOptions(template=tpl_b, tri_mode=TriMode.PYTHON),
    )
    res_b = ext_b.extract(payload)
    assert len(bridge.calls) == 2
    assert res_b.attachments[0].template_key == "tpl_beta"
    assert res_b.attachments[0].structured_data == {"beta_code": "BETA-002"}


def test_m5_normalized_doc_backward_compat():
    legacy_payload = {
        "doc_id": "legacy_01",
        "source_type": "EMAIL",
        "original_path": "Outlook://Inbox/legacy_01",
        "clean_body_text": "Hello world",
        "attachments": [
            {
                "filename": "old.png",
                "content_type": "image/png",
                "blob_hash": "abcd",
                "vlm_markdown": "Old table",
            }
        ],
    }
    doc = NormalizedDocumentJSON.model_validate(legacy_payload)
    assert doc.structured_context == {}
    assert doc.document_metadata == {}
    assert doc.unstructured_annotations == []
    assert doc.attachments[0].template_key == "default"
    assert doc.attachments[0].structured_data == {}


def test_m5_structured_entities_indexed_and_searchable(temp_app_dir: Path, catalog_repo: CatalogRepository):
    png_bytes = _make_png_bytes(200, 100)
    bridge = _MockScryerBridge()
    scryer_ext = ScryerExtractor(bridge=bridge)

    extractor = DocumentExtractor(
        catalog=catalog_repo,
        scryer_extractor=scryer_ext,
        default_options=ScryerExtractionOptions(enable_phase2=True, tri_mode=TriMode.PYTHON),
    )
    payload = RawIngestPayload(
        doc_id="doc_searchable_01",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/doc_searchable_01",
        subject="Invoice attached",
        sender="billing@orbit.example",
        body_text="Please find the attached invoice scan.",
        attachments=[
            RawAttachment(
                filename="scan.png",
                content_type="image/png",
                size_bytes=len(png_bytes),
                data=png_bytes,
            )
        ],
    )

    norm_doc = extractor.extract(payload)
    assert norm_doc.structured_context["scan.png"]["invoice_number"] == "INV-2026-8842"
    assert "dimensions" in norm_doc.document_metadata["scan.png"]

    chunker = ParentAwareChunker(max_chunk_tokens=200, chunk_overlap=20, tokenizer=None)
    chunks = chunker.chunk(norm_doc)
    assert len(chunks) >= 1

    fts_db = temp_app_dir / "fts.db"
    indexer = IndexEngine(fts_db_path=fts_db, lancedb_table=None)
    indexer.upsert_chunks(chunks)

    searcher = HybridSearchEngine(fts_db_path=fts_db, lancedb_table=None, catalog=catalog_repo)
    hits_id = searcher.search(SearchQuery(query_text="INV-2026-8842"))
    hits_vendor = searcher.search(SearchQuery(query_text="Orbit Dynamics"))

    assert len(hits_id) == 1
    assert hits_id[0].doc_id == "doc_searchable_01"
    assert len(hits_vendor) == 1
    assert hits_vendor[0].doc_id == "doc_searchable_01"


def test_m5_legacy_vlm_worker_removed():
    import old_rag.cli as cli_mod
    import old_rag.engine as engine_mod

    assert not hasattr(cli_mod, "cmd_vlm_worker")
    assert "VLMWorker" not in engine_mod.__all__
    with pytest.raises(ImportError):
        import old_rag.engine.vlm_worker  # noqa: F401

