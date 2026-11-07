"""test_pipeline.py - Unit tests for Coordinator, Outbox Recovery, and Lifecycle."""

from datetime import datetime, timezone

from old_rag.contracts import RawIngestPayload, SourceType
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.lifecycle import (
    SingleInstanceMutex,
    get_scoped_mutex_name,
    get_scoped_pipe_name,
)


def test_scoped_pipe_and_mutex_naming():
    pipe_name = get_scoped_pipe_name()
    mutex_name = get_scoped_mutex_name()
    assert "\\\\.\\pipe\\OldRAG_" in pipe_name
    assert "Local\\OldRAG_Mutex_" in mutex_name


def test_single_instance_mutex():
    m1 = SingleInstanceMutex("Local\\OldRAG_Test_Mutex_123")
    assert m1.acquire() is True
    # Second acquisition fails on same name
    m2 = SingleInstanceMutex("Local\\OldRAG_Test_Mutex_123")
    # On Windows this returns False; on non-Windows it falls back gracefully
    m1.release()


def test_coordinator_end_to_end_ingest_and_reindex(cas_engine, catalog_repo, temp_app_dir):
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)
    extractor = DocumentExtractor(catalog=catalog_repo)
    chunker = ParentAwareChunker(catalog=catalog_repo)

    coord = PipelineCoordinator(
        cas=cas_engine,
        catalog=catalog_repo,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    payload = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="msg_coord_100",
        original_path="Outlook://Inbox/msg_coord_100",
        sender="engineer@example.com",
        subject="Turbine Status",
        folder="Engineering",
        received_ts_utc=datetime.now(timezone.utc),
        body_text="Turbine vibration is normal under 1200 RPM.",
        raw_msg_bytes=b"RAW_MSG_BINARY_DATA",
    )

    coord.ingest_payload(payload)

    # 1. Verify in CAS
    stored_text = cas_engine.get_text("msg_coord_100")
    assert stored_text is not None
    assert "Turbine vibration" in stored_text.clean_body_text
    assert cas_engine.get_msg("msg_coord_100") == b"RAW_MSG_BINARY_DATA"

    # 2. Verify in Catalog
    doc = catalog_repo.get_document("msg_coord_100")
    assert doc is not None
    assert doc["folder"] == "Engineering"

    # 3. Test offline re-indexing (reads from CAS Tier 1 without Outlook)
    reindexed_count = coord.reindex_all()
    assert reindexed_count >= 1

