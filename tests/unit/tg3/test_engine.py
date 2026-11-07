"""test_engine.py - Unit tests for Chunker, Extractor, and Search Engine."""

from datetime import datetime, timezone
from pathlib import Path

from old_rag.contracts import (
    NormalizedDocumentJSON,
    RelevanceTier,
    SearchQuery,
    SourceType,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.indexer import IndexEngine
from old_rag.engine.search import (
    HybridSearchEngine,
    escape_fts5_query,
    is_identifier_like,
)


def test_parent_aware_reply_stripper(catalog_repo):
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


def test_fts5_escaping_and_identifier_detection():
    assert escape_fts5_query('SN-4471-AB OR "malicious NEAR"') == '"SN-4471-AB" "OR" "malicious" "NEAR"'
    assert is_identifier_like("SN-4471-AB") is True
    assert is_identifier_like("PART_9921_X") is True
    assert is_identifier_like("regularword") is False


def test_exact_identifier_pinning_rank1(temp_app_dir):
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

