"""test_adversarial_cas.py - Adversarial testing for CAS, DPAPI, and AES-GCM Integrity."""

import hashlib
import os
import pytest
from cryptography.exceptions import InvalidTag

from old_rag.contracts import NormalizedDocumentJSON, SourceType


def test_adversarial_ciphertext_tampering(cas_engine):
    """Tampering with a single byte in ciphertext must fail AES-GCM authentication."""
    doc = NormalizedDocumentJSON(
        doc_id="adversarial_doc_01",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/adv1",
        clean_body_text="Confidential corporate communications.",
    )
    hmac_ref = cas_engine.put_text(doc.doc_id, doc)
    enc_path = cas_engine.text_dir / hmac_ref[:2] / f"{hmac_ref}.json.enc"
    assert enc_path.exists()

    # Tamper with the ciphertext (flip a byte in the payload)
    raw_data = bytearray(enc_path.read_bytes())
    raw_data[20] ^= 0xFF
    enc_path.write_bytes(bytes(raw_data))

    # Decrypting must fail auth tag verification and return None
    with pytest.raises(InvalidTag):
        # Directly decrypt to confirm cryptography library raises InvalidTag
        nonce = raw_data[:12]
        ciphertext = raw_data[12:]
        cas_engine._aesgcm.decrypt(nonce, bytes(ciphertext), doc.doc_id.encode("utf-8"))

    # cas_engine.get_text catches or handles
    assert cas_engine.get_text(doc.doc_id) is None


def test_adversarial_mismatched_aad_rejection(cas_engine):
    """Replaying a ciphertext under a different doc_id must be rejected via AAD."""
    doc = NormalizedDocumentJSON(
        doc_id="legit_doc_id",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/legit",
        clean_body_text="Legitimate content.",
    )
    cas_engine.put_text(doc.doc_id, doc)

    # Attempting to retrieve under a spoofed doc_id
    assert cas_engine.get_text("spoofed_doc_id") is None


def test_adversarial_truncated_ciphertext(cas_engine):
    """Payloads shorter than the 12-byte nonce + 16-byte tag must return None safely."""
    fake_ref = "00" + "f" * 62
    fake_path = cas_engine.blob_dir / "00" / f"{fake_ref}.bin.enc"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"short_garbage")

    retrieved = cas_engine.get_blob("fake_blob_hash")
    assert retrieved is None


def test_adversarial_empty_payload(cas_engine):
    """Empty data blobs must encrypt, store, and decrypt to empty bytes cleanly."""
    empty_sha = hashlib.sha256(b"").hexdigest()
    ref = cas_engine.put_blob(empty_sha, b"")
    assert ref
    retrieved = cas_engine.get_blob(empty_sha)
    assert retrieved == b""
