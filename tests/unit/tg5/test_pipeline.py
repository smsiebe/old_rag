"""test_pipeline.py - Unit tests for Coordinator, Daemon, and Lifecycle (TG5)."""

import json
from datetime import datetime, timezone
import pytest

from old_rag.contracts import RawIngestPayload, SourceType
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.pipeline.lifecycle import (
    NamedPipeClient,
    NamedPipeServer,
    SingleInstanceMutex,
    get_scoped_mutex_name,
    get_scoped_pipe_name,
    set_user_autostart,
)


# --- T5.1: Two-Phase Outbox Coordinator Tests ---

def test_t5_1_coordinator_end_to_end_ingest_and_reindex(cas_engine, catalog_repo, temp_app_dir):
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


# --- T5.2: Daemon & Battery Governor Tests ---

def test_t5_2_daemon_ipc_request_handling(temp_app_dir, storage_config):
    daemon = DaemonProcess(storage_cfg=storage_config)

    # Status request
    req_status = json.dumps({"action": "status"})
    res_status = json.loads(daemon.handle_client_request(req_status))
    assert res_status["daemon_running"] is True

    # Reindex request
    req_reindex = json.dumps({"action": "reindex"})
    res_reindex = json.loads(daemon.handle_client_request(req_reindex))
    assert res_reindex["success"] is True

    # Purge request
    req_purge = json.dumps({"action": "purge"})
    res_purge = json.loads(daemon.handle_client_request(req_purge))
    assert res_purge["success"] is True

    # Battery sensor check returns boolean
    assert isinstance(daemon.is_on_battery(), bool)

    # Negative assertion: unknown action returns error
    req_bad = json.dumps({"action": "unknown_action_xyz"})
    res_bad = json.loads(daemon.handle_client_request(req_bad))
    assert "error" in res_bad


# --- T5.3: Lifecycle, Mutex & Named Pipe Tests ---

def test_t5_3_scoped_pipe_and_mutex_naming():
    pipe_name = get_scoped_pipe_name()
    mutex_name = get_scoped_mutex_name()
    assert "\\\\.\\pipe\\OldRAG_" in pipe_name
    assert "Local\\OldRAG_Mutex_" in mutex_name


def test_t5_3_single_instance_mutex():
    m1 = SingleInstanceMutex("Local\\OldRAG_Test_Mutex_123")
    assert m1.acquire() is True
    # Second acquisition fails on same name on Windows
    m2 = SingleInstanceMutex("Local\\OldRAG_Test_Mutex_123")
    # Release clean
    m1.release()
    # Releasing already closed handle does not fail
    m1.release()


def test_t5_3_named_pipe_ipc_roundtrip():
    test_pipe = "\\\\.\\pipe\\OldRAG_TestPipe_Unit"
    server = NamedPipeServer(test_pipe, handler=lambda req: json.dumps({"echo": json.loads(req)}))
    server.start()

    client = NamedPipeClient(test_pipe)
    resp = client.call({"test_key": "test_val"})
    assert resp.get("echo", {}).get("test_key") == "test_val"

    server.stop()
