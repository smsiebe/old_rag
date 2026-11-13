"""test_remediation.py - Verification for R1-R4: FTS5 Stemming, Vector Search, Rank-1 Pinning, and MCP."""

import json
from pathlib import Path
import lancedb
import pytest

from old_rag.contracts import (
    DocumentChunk,
    NormalizedDocumentJSON,
    RelevanceTier,
    SearchQuery,
    SourceType,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.indexer import IndexEngine, matryoshka_reduce
from old_rag.engine.search import (
    HybridSearchEngine,
    escape_fts5_query,
    extract_trigram_query,
    is_identifier_like,
)
from old_rag.mcp_server import (
    get_archived_document,
    list_recent_threads,
    query_personal_archive,
)
from old_rag.schema import get_lancedb_pyarrow_schema


def test_fts5_plural_stemming(temp_app_dir):
    """Behavioral proof: querying 'dish' matches 'dishes', 'email' matches 'emails' via Porter stemmer."""
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)
    searcher = HybridSearchEngine(fts_db)

    doc = NormalizedDocumentJSON(
        doc_id="doc_stem_01",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/stem_01",
        clean_body_text="We washed all the dishes and reviewed the incoming emails yesterday.",
    )
    chunker = ParentAwareChunker()
    chunks = chunker.chunk(doc)
    indexer.upsert_chunks(chunks)

    # 1. Singular 'dish' must match plural 'dishes'
    q_dish = SearchQuery(query_text="dish")
    res_dish = searcher.search(q_dish)
    assert len(res_dish) > 0
    assert res_dish[0].doc_id == "doc_stem_01"
    assert "dishes" in res_dish[0].content

    # 2. Singular 'email' must match plural 'emails'
    q_email = SearchQuery(query_text="email")
    res_email = searcher.search(q_email)
    assert len(res_email) > 0
    assert res_email[0].doc_id == "doc_stem_01"

    # 3. Plural query 'emails' must match
    q_emails = SearchQuery(query_text="emails")
    res_emails = searcher.search(q_emails)
    assert len(res_emails) > 0


def test_exact_identifier_pinning_rank1(temp_app_dir):
    """Behavioral proof: Exact serial number or IP is pinned at Rank 1 with exact_identifier_match=True."""
    fts_db = temp_app_dir / "lexical_fts.db"
    indexer = IndexEngine(fts_db)
    searcher = HybridSearchEngine(fts_db)

    doc_gen = NormalizedDocumentJSON(
        doc_id="doc_general",
        source_type=SourceType.FILE,
        original_path="C:/docs/gen.txt",
        clean_body_text="System update report with routine maintenance performed on servers.",
    )
    doc_part = NormalizedDocumentJSON(
        doc_id="doc_target_part",
        source_type=SourceType.FILE,
        original_path="C:/docs/part.txt",
        clean_body_text="Replaced critical component serial SN-4471-AB in rack chassis 2.",
    )
    doc_ip = NormalizedDocumentJSON(
        doc_id="doc_target_ip",
        source_type=SourceType.FILE,
        original_path="C:/docs/network.txt",
        clean_body_text="Static management gateway assigned IP 156.0.4314.58 on switch port 3.",
    )

    chunker = ParentAwareChunker()
    for d in (doc_gen, doc_part, doc_ip):
        indexer.upsert_chunks(chunker.chunk(d))

    # Query with identifier mixed with natural words
    q_serial = SearchQuery(query_text="report on SN-4471-AB")
    res_serial = searcher.search(q_serial)

    assert len(res_serial) > 0
    top_hit = res_serial[0]
    assert top_hit.doc_id == "doc_target_part"
    assert top_hit.exact_identifier_match is True
    assert top_hit.relevance_tier == RelevanceTier.HIGH

    # Query IP identifier
    q_ip = SearchQuery(query_text="gateway 156.0.4314.58")
    res_ip = searcher.search(q_ip)
    assert len(res_ip) > 0
    assert res_ip[0].doc_id == "doc_target_ip"
    assert res_ip[0].exact_identifier_match is True


def test_semantic_vector_retrieval_cosine_similarity(temp_app_dir):
    """Rule 10 & Behavioral proof: Live FastEmbed + LanceDB vector retrieval with non-zero cosine similarity."""
    fts_db = temp_app_dir / "lexical_fts.db"
    lance_dir = temp_app_dir / "lancedb"
    lance_dir.mkdir(parents=True, exist_ok=True)

    db = lancedb.connect(str(lance_dir))
    schema = get_lancedb_pyarrow_schema(dim=768)
    lance_tbl = db.create_table("chunks_vec", schema=schema, exist_ok=True)

    indexer = IndexEngine(fts_db_path=fts_db, lancedb_table=lance_tbl)
    searcher = HybridSearchEngine(fts_db_path=fts_db, lancedb_table=lance_tbl)

    doc_culinary = NormalizedDocumentJSON(
        doc_id="doc_culinary",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/dinner",
        clean_body_text="The culinary chef prepared gourmet seafood and exquisite cuisine for dinner.",
    )
    doc_code = NormalizedDocumentJSON(
        doc_id="doc_code",
        source_type=SourceType.EMAIL,
        original_path="Outlook://Inbox/code",
        clean_body_text="Python programming, compilers, and database administration internals.",
    )

    chunker = ParentAwareChunker()
    indexer.upsert_chunks(chunker.chunk(doc_culinary))
    indexer.upsert_chunks(chunker.chunk(doc_code))

    # Conceptual search with ZERO exact word overlap (restaurant cooking meals)
    q_concept = SearchQuery(query_text="restaurant cooking meals")
    results = searcher.search(q_concept)

    assert len(results) > 0
    # Vector retrieval must find culinary document
    top = results[0]
    assert top.doc_id == "doc_culinary"
    # Cosine similarity must be a real non-zero float
    assert top.cosine_similarity is not None
    assert isinstance(top.cosine_similarity, float)
    assert top.cosine_similarity > 0.0
    assert top.vector_rank == 1


def test_mcp_server_tools_interface():
    """Verify old_rag FastMCP tool exports and JSON-serializable responses."""
    # Test query_personal_archive tool
    resp_search = query_personal_archive(query="test", top_k=5)
    parsed = json.loads(resp_search)
    assert isinstance(parsed, dict)
    assert "results" in parsed or "error" in parsed

    # Test list_recent_threads tool
    resp_threads = list_recent_threads(limit=5)
    parsed_threads = json.loads(resp_threads)
    assert isinstance(parsed_threads, dict)
    assert "threads" in parsed_threads or "error" in parsed_threads

    # Test get_archived_document with non-existent id
    resp_doc = get_archived_document(doc_id="non_existent_doc_id")
    parsed_doc = json.loads(resp_doc)
    assert isinstance(parsed_doc, dict)

