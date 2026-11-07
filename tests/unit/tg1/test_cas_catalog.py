"""test_cas_catalog.py - Unit tests for CAS, Catalog, KeyManager, and ModelResolver (TG1)."""

import hashlib
import json
from pathlib import Path
import pytest

from old_rag.contracts import (
    ModelConfig,
    NormalizedDocumentJSON,
    OutboxOp,
    OutboxStatus,
    SourceType,
    StorageConfig,
)
from old_rag.storage.cas import CASEngine, KeyManager
from old_rag.storage.models import ModelResolver


# --- T1.1: ModelResolver Tests ---

def test_t1_1_model_resolver_local_paths(temp_app_dir):
    models_dir = temp_app_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    gguf_path = models_dir / "paddleocr-vl-1.6-q4_k_m.gguf"
    mmproj_path = models_dir / "mmproj-model-f16.gguf"

    gguf_path.write_bytes(b"GGUF_WEIGHT_BYTES")
    mmproj_path.write_bytes(b"MMPROJ_WEIGHT_BYTES")

    resolver = ModelResolver(models_dir=models_dir)
    res_gguf, res_mmproj = resolver.resolve_vlm_pair()
    assert res_gguf == str(gguf_path)
    assert res_mmproj == str(mmproj_path)

    # Embedder directory resolution
    emb_dir, tok_path = resolver.resolve_embedder_model()
    assert Path(emb_dir).exists()
    assert "tokenizer.json" in tok_path


def test_t1_1_model_resolver_checksum_verification(temp_app_dir):
    models_dir = temp_app_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    test_file = models_dir / "test.bin"
    test_file.write_bytes(b"DATA")
    expected_sha = hashlib.sha256(b"DATA").hexdigest()

    resolver = ModelResolver(models_dir=models_dir)
    # Positive checksum verification
    assert resolver._verify_sha256(test_file, expected_sha) is True
    # Negative: mismatched checksum returns False
    assert resolver._verify_sha256(test_file, "wrong_checksum_value") is False
    # Negative: non-existent file returns False
    assert resolver._verify_sha256(models_dir / "non_existent.bin", expected_sha) is False


# --- T1.2: CAS Engine & Parameter Sensitivity Tests ---

def test_t1_2_key_manager_export_and_unwrap(temp_app_dir):
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


def test_t1_2_cas_blob_deduplication(cas_engine):
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

    # Negative: getting non-existent blob returns None
    assert cas_engine.get_blob("non_existent_blob_hash") is None


def test_t1_2_cas_parameter_sensitivity_raw_msg(temp_app_dir):
    """Rule 12: copy_storage_emails_raw_msg flag independently controls .msg archival."""
    # When True -> saves raw .msg
    cfg_true = StorageConfig(app_data_dir=str(temp_app_dir), copy_storage_emails_raw_msg=True)
    cas_true = CASEngine(cfg_true)
    ref_true = cas_true.put_msg("doc_with_msg", b"RAW_MSG_BYTES")
    assert ref_true != ""
    assert cas_true.get_msg("doc_with_msg") == b"RAW_MSG_BYTES"

    # When False -> returns empty ref and writes 0 bytes to msg/
    cfg_false = StorageConfig(app_data_dir=str(temp_app_dir), copy_storage_emails_raw_msg=False)
    cas_false = CASEngine(cfg_false)
    ref_false = cas_false.put_msg("doc_without_msg", b"RAW_MSG_BYTES")
    assert ref_false == ""
    assert cas_false.get_msg("doc_without_msg") is None


def test_t1_2_cas_text_tier_roundtrip(cas_engine):
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

    # Negative: get non-existent text returns None
    assert cas_engine.get_text("missing_doc_id") is None


# --- T1.3: Catalog & Reference-Counting GC Tests ---

def test_t1_3_catalog_blob_refcounting_and_prune(catalog_repo):
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

    # Negative: querying non-existent document returns None
    assert catalog_repo.get_document("unknown_doc") is None


def test_t1_3_catalog_outbox_enqueue_and_commit(catalog_repo):
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


def test_t1_3_vlm_cache_and_skip_list(catalog_repo):
    # Test VLM Markdown caching in catalog
    h = "img_hash_999"
    assert catalog_repo.get_vlm_cache(h) is None
    catalog_repo.put_vlm_cache(h, "| Markdown Table |")
    assert catalog_repo.get_vlm_cache(h) == "| Markdown Table |"

    # Test VLM skip list
    assert catalog_repo.is_vlm_skipped(h) is False
    catalog_repo.add_vlm_skip_hash(h, reason="repeated signature")
    assert catalog_repo.is_vlm_skipped(h) is True
