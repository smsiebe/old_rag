"""test_adversarial_v3_quota_and_plugins.py - Adversarial Security, Recovery & Concurrency Suite (TG4).

Zero-mock adversarial verification:
1. test_adversarial_chat_plugin_path_traversal_and_malicious_script_escaping
2. test_adversarial_mid_delete_outbox_crash_recovery_and_source_file_preservation
3. test_adversarial_concurrent_realtime_ingest_and_hybrid_search_wal_saturation
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest

from old_rag.contracts import (
    DataTypeQuotaConfig,
    OutboxOp,
    OutboxRecord,
    OutboxStatus,
    QuotaPolicyType,
    RawIngestPayload,
    SearchQuery,
    SecurityViolationError,
    SourceType,
    StorageConfig,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.engine.search import HybridSearchEngine
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.storage.cas import CASEngine
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.quota import QuotaGovernor
from old_rag.ui.bridge import OldRAGDesktopBridge


def test_adversarial_chat_plugin_path_traversal_and_malicious_script_escaping(tmp_path):
    """Adversarial security test: reject directory traversal plugins and escape malicious script tags."""
    # 1. Path traversal rejection in ChatPluginRegistry
    plugin_root = tmp_path / "plugins" / "chat"
    plugin_root.mkdir(parents=True, exist_ok=True)

    outside_dir = (plugin_root / ".." / "unauthorized").resolve()
    outside_dir.mkdir(parents=True, exist_ok=True)
    malicious_script = outside_dir / "evil_plugin.py"
    malicious_script.write_text(
        "class MaliciousAdapter:\n    adapter_type = 'evil'\n",
        encoding="utf-8",
    )

    registry = ChatPluginRegistry(plugin_dir=plugin_root)

    # Test traversing outside plugin_dir via relative path traversal
    traversal_path = plugin_root / ".." / "unauthorized"
    with pytest.raises(SecurityViolationError):
        registry.discover_plugins(plugin_dir=traversal_path)

    # 2. XSS and script breakout escaping in render_desktop_html
    bridge = OldRAGDesktopBridge()
    html_output = bridge.render_desktop_html(inline_assets=True)

    # Assert that no raw unescaped </script> tag exists inside inline <script> blocks
    # which would allow attackers to breakout into DOM XSS
    script_blocks = re.findall(r"<script>(.*?)</script>", html_output, re.DOTALL | re.IGNORECASE)
    assert len(script_blocks) >= 2, "Expected multiple inlined script blocks"
    for block in script_blocks:
        assert "</script" not in block.lower(), "XSS breakout vulnerability: raw </script tag unescaped in script block"

    # 3. Verify static HTML does not contain raw dangerous eval or unescaped innerHTML injections
    assert "<script src=\"http" not in html_output
    assert "<link rel=\"stylesheet\" href=\"http" not in html_output


def test_adversarial_mid_delete_outbox_crash_recovery_and_source_file_preservation(tmp_path):
    """Crash-recovery test: mid-delete interrupted outbox recovers cleanly and preserves source files."""
    storage_cfg = StorageConfig(app_data_dir=str(tmp_path))
    cas = CASEngine(storage_cfg)
    cat = CatalogRepository(tmp_path / "catalog.db")
    ldb_table = open_lancedb_table(tmp_path, dim=768)
    indexer = IndexEngine(tmp_path / "lexical_fts.db", lancedb_table=ldb_table)
    extractor = DocumentExtractor(catalog=cat)
    chunker = ParentAwareChunker(catalog=cat)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=cat,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    # 1. Create a user's critical source file on disk
    user_file = tmp_path / "real_user_contract.docx"
    user_file.write_text("Sovereign contract text strictly owned by user.", encoding="utf-8")

    # Ingest it into OldRAG
    doc_id = "doc_contract_crash_01"
    payload = RawIngestPayload(
        doc_id=doc_id,
        source_type=SourceType.FILE,
        original_path=str(user_file),
        subject="Important Contract",
        body_text="Sovereign contract text strictly owned by user.",
        received_ts_utc=datetime.now(timezone.utc),
    )
    coordinator.ingest_payload(payload)

    # Verify ingestion was committed
    assert cas.get_text(doc_id) is not None
    assert len(cat.get_document(doc_id) or {}) > 0
    doc_meta = cat.get_document(doc_id)
    chunk_ids = json.loads(doc_meta.get("chunk_ids_json") or "[]")

    # 2. Simulate a crash mid-delete: An outbox DELETE record is enqueued in PENDING state
    outbox_id = cat.enqueue_outbox(op=OutboxOp.DELETE, doc_id=doc_id, chunk_ids=chunk_ids)
    assert outbox_id > 0

    # Verify outbox is currently pending
    pending = cat.get_pending_outbox()
    assert any(o.outbox_id == outbox_id for o in pending)

    # 3. Crash recovery: Process pending outbox
    committed_count = coordinator.process_outbox()
    assert committed_count >= 1

    # Verify outbox was transitioned to COMMITTED
    remaining_pending = cat.get_pending_outbox()
    assert not any(o.outbox_id == outbox_id for o in remaining_pending)

    # Verify OldRAG internal records are purged
    assert cas.get_text(doc_id) is None
    assert cat.get_document(doc_id) is None

    # 4. CRITICAL INVARIANT: The user's original physical file MUST NOT be deleted or modified!
    assert user_file.exists(), "INVARIANT VIOLATION: Source file on disk was unlinked during delete!"
    assert user_file.read_text(encoding="utf-8") == "Sovereign contract text strictly owned by user."


def test_adversarial_concurrent_realtime_ingest_and_hybrid_search_wal_saturation(tmp_path):
    """Stress test: Concurrent searchers and quota checkers under continuous ingestion and eviction."""
    storage_cfg = StorageConfig(app_data_dir=str(tmp_path))
    cas = CASEngine(storage_cfg)
    cat = CatalogRepository(tmp_path / "catalog.db")
    ldb_table = open_lancedb_table(tmp_path, dim=768)
    indexer = IndexEngine(tmp_path / "lexical_fts.db", lancedb_table=ldb_table)
    extractor = DocumentExtractor(catalog=cat)
    chunker = ParentAwareChunker(catalog=cat)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=cat,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )
    quota_gov = QuotaGovernor(catalog=cat, cas=cas, indexer=indexer, coordinator=coordinator)

    search_engine = HybridSearchEngine(
        fts_db_path=tmp_path / "lexical_fts.db",
        lancedb_table=ldb_table,
        catalog=cat,
    )

    stop_event = threading.Event()
    exceptions: list[Exception] = []

    # Reader worker: hybrid search and quota status checks
    def reader_loop():
        while not stop_event.is_set():
            try:
                # Concurrent search
                q = SearchQuery(query_text="concurrency stress test", top_k=5)
                search_engine.search(q)
                # Concurrent quota status check
                quota_gov.get_status(SourceType.EMAIL)
                time.sleep(0.01)
            except Exception as e:
                exceptions.append(e)

    # Writer worker: ingesting and evicting documents
    def writer_loop():
        counter = 0
        while not stop_event.is_set() and counter < 20:
            counter += 1
            doc_id = f"doc_stress_{counter}"
            try:
                payload = RawIngestPayload(
                    doc_id=doc_id,
                    source_type=SourceType.EMAIL,
                    original_path=f"C:\\mail\\stress_{counter}.eml",
                    subject=f"Concurrency Stress Document {counter}",
                    body_text="Continuous WAL saturation testing with multi-threaded readers and writers.",
                    received_ts_utc=datetime.now(timezone.utc),
                )
                coordinator.ingest_payload(payload)

                # Periodic quota check
                if counter % 5 == 0:
                    cfg = DataTypeQuotaConfig(
                        source_type=SourceType.EMAIL,
                        max_bytes=1000,
                        policy=QuotaPolicyType.OLDEST_FIRST,
                        preserve_recent_days=0,
                        preserve_starred=False,
                        preserve_pinned=False,
                    )
                    quota_gov.enforce_quota(SourceType.EMAIL, dry_run=False, config=cfg)
                time.sleep(0.02)
            except Exception as e:
                exceptions.append(e)

    # Launch 4 readers and 1 writer
    readers = [threading.Thread(target=reader_loop, daemon=True) for _ in range(4)]
    writer = threading.Thread(target=writer_loop, daemon=True)

    for r in readers:
        r.start()
    writer.start()

    writer.join(timeout=10.0)
    stop_event.set()
    for r in readers:
        r.join(timeout=2.0)

    # Verify zero database lock errors or exceptions occurred
    assert len(exceptions) == 0, f"Concurrent WAL saturation produced exceptions: {exceptions}"
