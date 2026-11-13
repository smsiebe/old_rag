"""test_admin_and_confidence.py - Behavioral verification of confidence calibration, storage privacy, connectors & metrics."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from old_rag.contracts import RelevanceTier, SearchQuery, SourceType
from old_rag.engine.search import HybridSearchEngine, escape_fts5_query
from old_rag.ingest.imap import IMAPConnector, PRESETS
from old_rag.schema import CATALOG_SCHEMA_DDL, FTS_SCHEMA_DDL
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.settings import (
    get_storage_breakdown,
    load_settings,
    save_settings,
    sweep_temp_views,
    validate_storage_location,
)


@pytest.fixture
def fts_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "lexical_fts.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(FTS_SCHEMA_DDL)
    conn.execute(
        """
        INSERT INTO chunks_fts_porter (chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc)
        VALUES ('chunk_lease_1', 'doc_lease', 'Residential Lease Agreement for Apartment 4B signed on May 1st.', 'landlord@property.com', 'Inbox', 'EMAIL', '2026-05-01T12:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO chunks_fts_porter (chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc)
        VALUES ('chunk_misc_2', 'doc_misc', 'Weekly grocery shopping list and milk.', 'friend@domain.com', 'Inbox', 'EMAIL', '2026-05-02T12:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_conversational_query_stopword_fallback(fts_db: Path):
    """Verify conversational queries with filler words ('give me stuff about my lease') successfully find documents via stopword fallback."""
    engine = HybridSearchEngine(fts_db_path=fts_db)
    query = SearchQuery(query_text="give me stuff about my lease", top_k=10)

    results = engine.search(query)
    assert len(results) > 0, "Conversational query must not return empty results"
    top_hit = results[0]
    assert top_hit.doc_id == "doc_lease"
    assert "Lease" in top_hit.content or "lease" in top_hit.content


def test_confidence_calibration_vector_and_hybrid(fts_db: Path):
    """Verify strong semantic vector matches receive HIGH tier and calibrated confidence score."""
    engine = HybridSearchEngine(fts_db_path=fts_db)

    # Mock _query_vector returning a strong cosine similarity hit (0.68)
    with patch.object(engine, "_query_vector", return_value=([("chunk_lease_1", 0.68)], {"chunk_lease_1": {"content": "Lease Agreement", "doc_id": "doc_lease", "source_type": "EMAIL"}})):
        query = SearchQuery(query_text="lease agreement", top_k=10)
        results = engine.search(query)

        assert len(results) > 0
        hit = results[0]
        assert hit.relevance_tier == RelevanceTier.HIGH
        assert hit.confidence_score is not None
        assert hit.confidence_score >= 0.65


def test_storage_validation_privacy_warnings():
    """Verify that storage paths outside safe local boundaries trigger privacy and security warnings."""
    # 1. UNC Path
    unc_res = validate_storage_location(r"\\192.168.1.50\Public\OldRAG")
    assert not unc_res["is_safe"]
    assert unc_res["is_critical"]
    assert "UNC network share" in unc_res["warning"]

    # 2. Cloud-synchronized directory (OneDrive)
    onedrive_res = validate_storage_location(r"C:\Users\JaneDoe\OneDrive\OldRAG_Data")
    assert not onedrive_res["is_safe"]
    assert onedrive_res["is_critical"]
    assert "cloud-synchronized" in onedrive_res["warning"]
    assert "OneDrive" in onedrive_res["warning"]

    # 3. Cloud-synchronized directory (Dropbox)
    dropbox_res = validate_storage_location(r"C:\Users\JaneDoe\Dropbox\OldRAG_Data")
    assert not dropbox_res["is_safe"]
    assert "Dropbox" in dropbox_res["warning"]

    # 4. Standard local AppData directory
    safe_res = validate_storage_location(r"C:\Users\JaneDoe\AppData\Local\OldRAG")
    assert safe_res["is_safe"]
    assert not safe_res["is_critical"]
    assert safe_res["warning"] is None


def test_storage_breakdown_and_sweep_views(tmp_path: Path):
    """Verify calculation of disk breakdown and sweeping of temporary decrypted view files."""
    app_dir = tmp_path / "OldRAG"
    app_dir.mkdir()

    # Create dummy files across tiers
    blob_dir = app_dir / "archive" / "blob"
    blob_dir.mkdir(parents=True)
    (blob_dir / "blob1.bin").write_bytes(b"A" * 1024)

    text_dir = app_dir / "archive" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "doc1.json.enc").write_bytes(b"B" * 2048)

    views_dir = app_dir / "views"
    views_dir.mkdir(parents=True)
    (views_dir / "temp_preview.msg").write_bytes(b"C" * 4096)

    breakdown = get_storage_breakdown(app_dir)
    assert breakdown["total_bytes"] == 1024 + 2048 + 4096
    assert breakdown["breakdown"]["cas_blobs"]["bytes"] == 1024
    assert breakdown["breakdown"]["cas_text"]["bytes"] == 2048
    assert breakdown["breakdown"]["views"]["bytes"] == 4096

    # Sweep views
    sweep_res = sweep_temp_views(app_dir)
    assert sweep_res["swept_files"] == 1
    assert sweep_res["freed_bytes"] == 4096
    assert not (views_dir / "temp_preview.msg").exists()

    # Re-check breakdown after sweep
    breakdown_after = get_storage_breakdown(app_dir)
    assert breakdown_after["breakdown"]["views"]["bytes"] == 0


def test_catalog_search_metrics_and_value_proposition(tmp_path: Path):
    """Verify that search metrics and deduplication storage savings are tracked and retrieved."""
    db_path = tmp_path / "catalog.db"
    cat = CatalogRepository(db_path)

    # 1. Insert documents first so foreign keys satisfy constraint
    cat.upsert_document("doc_1", "EMAIL", "path1")
    cat.upsert_document("doc_2", "EMAIL", "path2")
    cat.upsert_document("doc_3", "EMAIL", "path3")

    # 2. Record blob refs to simulate deduplication savings
    # Blob referenced by 3 documents: ref_count=3, size=10,000 bytes. Saved = 2 * 10,000 = 20,000 bytes
    cat.record_blob_ref("sha256_blob_dedup", "doc_1", 10000)
    cat.record_blob_ref("sha256_blob_dedup", "doc_2", 10000)
    cat.record_blob_ref("sha256_blob_dedup", "doc_3", 10000)

    # 2. Record query metrics
    cat.record_query_metric("lease agreement", "hybrid", 5, 0.88)
    cat.record_query_metric("invoice 2026", "vector", 2, 0.72)
    cat.record_query_metric("SN-4471-AB", "lexical", 1, 1.0)

    stats = cat.get_statistics()
    assert stats["total_queries"] == 3
    assert stats["resolved_by_hybrid"] == 1
    assert stats["resolved_by_vector"] == 1
    assert stats["resolved_by_lexical"] == 1
    assert stats["storage_saved_bytes"] == 20000
    assert len(stats["recent_queries"]) == 3
    assert stats["is_sovereign"] is True


def test_imap_connector_test_connection_mock():
    """Verify IMAPConnector test_connection parses folders and catches authentication failure without external network calls."""
    connector = IMAPConnector(host="imap.gmail.com", username="test@gmail.com", password="app_password")

    # Mock successful IMAP response
    mock_imap = MagicMock()
    mock_imap.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"'])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        res = connector.test_connection()
        assert res["success"] is True
        assert "INBOX" in res["folders"]
        assert "[Gmail]/Sent Mail" in res["folders"]

    # Mock auth failure
    with patch("imaplib.IMAP4_SSL", side_effect=Exception("[AUTHENTICATIONFAILED] Invalid credentials")):
        fail_res = connector.test_connection()
        assert fail_res["success"] is False
        assert "App-Specific Password" in fail_res["error"]
