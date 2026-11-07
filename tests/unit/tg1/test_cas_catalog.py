"""test_cas_catalog.py - Unit tests for CAS, Catalog, and KeyManager."""

import hashlib
import json
from pathlib import Path

from old_rag.contracts import NormalizedDocumentJSON, OutboxOp, OutboxStatus, SourceType
from old_rag.storage.cas import KeyManager


def test_key_manager_export_and_unwrap(temp_app_dir):
    key_path = temp_app_dir / "master_key.dpapi"
    km = KeyManager(key_path)
    k1 = km.get_master_key()
    assert len(k1) == 32
    assert key_path.exists()

    # Re-reading unwraps to same key
    km2 = KeyManager(key_path)
    k2 = km2.get_master_key()
    assert k1 == k2

    # Export with passphrase
    backup_json = km.export_key("secure_passphrase")
    payload = json.loads(backup_json.decode("utf-8"))
    assert "salt_hex" in payload
    assert "ciphertext_hex" in payload


def test_cas_blob_deduplication(cas_engine):
    data = b"Shared PDF attachment content repeated across 10 emails"
    sha = hashlib.sha256(data).hexdigest()

    ref1 = cas_engine.put_blob(sha, data)
    ref2 = cas_engine.put_blob(sha, data)
    assert ref1 == ref2

    # Check that decrypted payload matches
    retrieved = cas_engine.get_blob(sha)
    assert retrieved == data

    # Count physical files on disk
    blob_files = list(cas_engine.blob_dir.rglob("*.bin.enc"))
    assert len(blob_files) == 1


def test_cas_text_tier_roundtrip(cas_engine):
    doc = NormalizedDocumentJSON(
        doc_id="test_doc_001",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/test_doc_001",
        clean_body_text="Hello, this is a test email body for canonical text CAS storage.",
    )
    ref = cas_engine.put_text(doc.doc_id, doc)
    assert ref

    loaded = cas_engine.get_text(doc.doc_id)
    assert loaded is not None
    assert loaded.doc_id == doc.doc_id
    assert loaded.clean_body_text == doc.clean_body_text


def test_catalog_blob_refcounting_and_prune(catalog_repo):
    catalog_repo.upsert_document("doc_1", "FILE", "/path/doc1")
    catalog_repo.upsert_document("doc_2", "FILE", "/path/doc2")
    h = "test_blob_hash_abc123"
    r1 = catalog_repo.record_blob_ref(h, "doc_1", 1024)
    assert r1 == 1

    r2 = catalog_repo.record_blob_ref(h, "doc_2", 1024)
    assert r2 == 2

    # Decrement ref
    r3 = catalog_repo.release_blob_ref(h, "doc_1")
    assert r3 == 1
    assert catalog_repo.prune_orphans(purge_source_deleted=False) == []

    # Decrement to zero
    r4 = catalog_repo.release_blob_ref(h, "doc_2")
    assert r4 == 0
    pruned = catalog_repo.prune_orphans(purge_source_deleted=True)
    assert pruned == [h]


def test_catalog_outbox_enqueue_and_commit(catalog_repo):
    outbox_id = catalog_repo.enqueue_outbox(
        op=OutboxOp.INGEST,
        doc_id="doc_outbox_test",
        chunk_ids=["chunk_1", "chunk_2"],
    )
    assert outbox_id > 0

    pending = catalog_repo.get_pending_outbox()
    assert len(pending) == 1
    assert pending[0].doc_id == "doc_outbox_test"
    assert pending[0].status == OutboxStatus.PENDING

    catalog_repo.mark_outbox_committed(outbox_id)
    assert len(catalog_repo.get_pending_outbox()) == 0
