"""test_adversarial_search.py - Adversarial testing for Search, Escaping, and Numerical Precision."""

import math
from old_rag.contracts import RelevanceTier, SearchQuery
from old_rag.engine.search import HybridSearchEngine, escape_fts5_query


def test_adversarial_fts5_syntax_fuzzing(temp_app_dir):
    """Malicious FTS5 operators and malformed queries must never crash search."""
    fts_db = temp_app_dir / "lexical_fts.db"
    searcher = HybridSearchEngine(fts_db)

    malicious_inputs = [
        '"""',
        'NEAR/0 ()',
        'AND AND OR NOT',
        '***???',
        '-- DROP TABLE chunks_fts_porter;',
        "' OR 1=1; --",
        '""""" unmatched quotes',
        'colon:prefix* without:closing',
        'a' * 1000,  # Extreme query length
        '',  # Empty
        '   \t\n  ',
    ]

    for evil_query in malicious_inputs:
        escaped = escape_fts5_query(evil_query)
        assert isinstance(escaped, str)
        # Search call must return empty or safe list without throwing SQLite syntax error
        q = SearchQuery(query_text=evil_query)
        results = searcher.search(q)
        assert isinstance(results, list)


def test_adversarial_numerical_precision_rule18(temp_app_dir):
    """Rule 18: RRF calculations must match reference float64 math to within 1e-12."""
    fts_db = temp_app_dir / "lexical_fts.db"
    searcher = HybridSearchEngine(fts_db)

    # Reference float64 math
    k = 60.0
    l_rank = 1.0
    v_rank = 2.0
    ref_rrf = (1.0 / (k + l_rank)) + (1.0 / (k + v_rank))
    ref_max = 2.0 / (k + 1.0)
    ref_norm = ref_rrf / ref_max

    # Directly mock query returns to check searcher's exact RRF math
    query = SearchQuery(query_text="precision test")
    searcher._query_fts = lambda q: [("chunk_1", -10.5)]
    searcher._query_vector = lambda q: [("chunk_2", 0.95), ("chunk_1", 0.90)]

    results = searcher.search(query)
    c1 = next(r for r in results if r.chunk_id == "chunk_1")

    # Verify precision to 1e-12
    assert math.isclose(c1.rrf_score, ref_rrf, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(c1.normalized_rrf, ref_norm, rel_tol=1e-12, abs_tol=1e-12)

    # Verify score is not rounded to fewer decimal places
    score_str = f"{c1.rrf_score:.10f}"
    assert not score_str.endswith("00000")
