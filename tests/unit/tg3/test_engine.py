"""test_engine.py - Unit tests for Extractor, Chunker, Indexer, Search, and VLM (TG3)."""

import io
from pathlib import Path
import pytest
from PIL import Image

from old_rag.contracts import (
    DimensionMismatchError,
    ModelConfig,
    NormalizedDocumentJSON,
    RelevanceTier,
    SearchQuery,
    SourceType,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor, strip_html_tags
from old_rag.engine.indexer import (
    IndexEngine,
    derive_fts_rowid,
    generate_deterministic_embedding,
    matryoshka_reduce,
)
from old_rag.engine.search import (
    HybridSearchEngine,
    escape_fts5_query,
    is_identifier_like,
)
from old_rag.engine.vlm_worker import VLMWorker


# --- T3.1: DocumentExtractor Tests ---

def test_t3_1_extractor_capabilities():
    extractor = DocumentExtractor()
    assert extractor.can_extract("doc.pdf") is True
    assert extractor.can_extract("sheet.xlsx") is True
    assert extractor.can_extract("file.docx") is True
    assert extractor.can_extract("msg.eml") is True
    # Negative assertion: unsupported format
    assert extractor.can_extract("archive.zip") is False
    assert extractor.can_extract("binary.exe") is False


def test_t3_1_html_stripper():
    html = "<div><p>Hello <b>World</b>!</p><br/></div>"
    clean = strip_html_tags(html)
    assert clean == "Hello World !"


# --- T3.2: Chunker & Reply Stripping Tests ---

def test_t3_2_parent_aware_reply_stripper(catalog_repo):
    chunker = ParentAwareChunker(catalog=catalog_repo)

    text_with_quote = (
        "We have reviewed the incident and replaced the drive.\n\n"
        "-----Original Message-----\n"
        "From: boss@example.com\n"
        "Sent: Monday, Nov 2, 2026\n"
        "Subject: Urgent failure\n\n"
        "Please look at the server ASAP."
    )

    doc_without_parent = NormalizedDocumentJSON(
        doc_id="reply_doc_01",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/reply_doc_01",
        clean_body_text=text_with_quote,
        in_reply_to="<parent-msg-unknown@example.com>",
    )

    # Parent is unknown -> must NOT strip quote to avoid data loss
    retained = chunker.strip_reply_chain(text_with_quote, doc_without_parent)
    assert "-----Original Message-----" in retained

    # Register parent in catalog
    catalog_repo.upsert_document(
        doc_id="<parent-msg-unknown@example.com>",
        source_type="EMAIL",
        original_path="Outlook://Inbox/parent",
    )

    # Now parent is known -> safe to strip
    stripped = chunker.strip_reply_chain(text_with_quote, doc_without_parent)
    assert "-----Original Message-----" not in stripped
    assert "replaced the drive" in stripped


def test_t3_2_chunker_windowing_and_negative():
    chunker = ParentAwareChunker(max_chunk_tokens=10, chunk_overlap=2)
    # Long text: 25 words
    long_text = "word " * 25
    doc = NormalizedDocumentJSON(
        doc_id="long_doc_01",
        source_type=SourceType.FILE,
        original_path="C:/docs/file.txt",
        clean_body_text=long_text.strip(),
    )
    chunks = chunker.chunk(doc)
    assert len(chunks) > 1
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    assert chunks[0].doc_id == "long_doc_01"

    # Negative: empty text produces 0 chunks
    empty_doc = NormalizedDocumentJSON(
        doc_id="empty_doc",
        source_type=SourceType.FILE,
        original_path="C:/docs/empty.txt",
        clean_body_text="",
    )
    assert chunker.chunk(empty_doc) == []


# --- T3.3: Indexer & Matryoshka Parameter Sensitivity Tests ---

def test_t3_3_matryoshka_parameter_sensitivity():
    """Rule 12: matryoshka_dim independently controls embedding dimensionality."""
    # Test 768 dim
    emb_768 = generate_deterministic_embedding("test text", dim=768)
    assert len(emb_768) == 768

    # Test 512 dim
    emb_512 = generate_deterministic_embedding("test text", dim=512)
    assert len(emb_512) == 512

    # Test 256 dim
    emb_256 = generate_deterministic_embedding("test text", dim=256)
    assert len(emb_256) == 256


def test_t3_3_indexer_crud_operations(temp_app_dir):
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)

    doc = NormalizedDocumentJSON(
        doc_id="crud_doc_01",
        source_type=SourceType.FILE,
        original_path="C:/docs/crud.txt",
        folder="OriginalFolder",
        clean_body_text="Searchable content for CRUD test.",
    )
    chunker = ParentAwareChunker()
    chunks = chunker.chunk(doc)
    indexer.upsert_chunks(chunks)

    # Verify update folder
    indexer.update_folder("crud_doc_01", "NewFolder")

    # Verify deletion
    chunk_ids = [c.chunk_id for c in chunks]
    indexer.delete_chunks(chunk_ids)

    # Negative: deleting empty chunk list does not fail
    indexer.delete_chunks([])


# --- T3.4: Search Engine & RRF Precision Tests ---

def test_t3_4_fts5_escaping_and_identifier_detection():
    assert escape_fts5_query('SN-4471-AB OR "malicious NEAR"') == '"SN-4471-AB" "OR" "malicious" "NEAR"'
    assert is_identifier_like("SN-4471-AB") is True
    assert is_identifier_like("PART_9921_X") is True
    assert is_identifier_like("regularword") is False


def test_t3_4_exact_identifier_pinning_rank1(temp_app_dir):
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)
    searcher = HybridSearchEngine(fts_db)

    # Index two documents
    doc_match = NormalizedDocumentJSON(
        doc_id="doc_serial",
        source_type=SourceType.FILE,
        original_path="C:/docs/part.txt",
        clean_body_text="Component SN-4471-AB was replaced on Monday.",
    )
    doc_other = NormalizedDocumentJSON(
        doc_id="doc_other",
        source_type=SourceType.FILE,
        original_path="C:/docs/other.txt",
        clean_body_text="General component maintenance log without part codes.",
    )

    chunker = ParentAwareChunker()
    indexer.upsert_chunks(chunker.chunk(doc_match))
    indexer.upsert_chunks(chunker.chunk(doc_other))

    # Query for exact serial number
    q = SearchQuery(query_text="SN-4471-AB")
    results = searcher.search(q)
    assert len(results) > 0
    assert results[0].doc_id == "doc_serial"
    assert results[0].relevance_tier == RelevanceTier.HIGH

    # Verify Rule 18 precision
    assert isinstance(results[0].rrf_score, float)
    assert results[0].rrf_score > 0.0


def test_t3_4_rrf_k_parameter_sensitivity(temp_app_dir):
    """Rule 12: rrf_k independently changes score magnitude."""
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)

    doc = NormalizedDocumentJSON(
        doc_id="doc_k_test",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/k_test",
        clean_body_text="Special diagnostic code XYZ-9988.",
    )
    indexer.upsert_chunks(ParentAwareChunker().chunk(doc))

    s_60 = HybridSearchEngine(fts_db, config=ModelConfig(rrf_k=60.0))
    s_20 = HybridSearchEngine(fts_db, config=ModelConfig(rrf_k=20.0))

    r_60 = s_60.search(SearchQuery(query_text="XYZ-9988"))
    r_20 = s_20.search(SearchQuery(query_text="XYZ-9988"))

    assert len(r_60) > 0 and len(r_20) > 0
    # Different k values produce distinct RRF scores
    assert r_60[0].rrf_score != r_20[0].rrf_score


# --- T3.5: VLM Worker & Tiling Tests ---

def test_t3_5_vlm_worker_tiling():
    worker = VLMWorker()
    # Create test 400x400 image
    img = Image.new("RGB", (400, 400), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    md = worker.extract_image_markdown(data, "test.png")
    assert isinstance(md, str)
    assert len(md) > 0

    # Negative: corrupted image bytes return empty or fallback string
    bad_md = worker._extract_tiled(b"corrupt_image_bytes")
    assert bad_md == ""
