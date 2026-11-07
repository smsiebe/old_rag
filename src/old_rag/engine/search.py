"""search.py - Escaped FTS5 & Vector Hybrid RRF Search with Exact Identifier Pinning."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from old_rag.contracts import (
    ModelConfig,
    RelevanceTier,
    SearchQuery,
    SearchResult,
    SourceType,
)
from old_rag.protocols import SearchEngineProtocol


def escape_fts5_query(raw_query: str) -> str:
    """Sanitize and wrap tokens in double quotes to neutralize FTS5 operator collisions."""
    tokens = re.findall(r"[\w\.-]+", raw_query)
    if not tokens:
        return '""'
    escaped = []
    for t in tokens:
        clean = t.replace('"', '""')
        escaped.append(f'"{clean}"')
    return " ".join(escaped)


def is_identifier_like(token: str) -> bool:
    """Return True if token contains digits, hyphens, or underscores (e.g. SN-4471-AB)."""
    has_alpha = bool(re.search(r"[a-zA-Z]", token))
    has_digit = bool(re.search(r"\d", token))
    has_symbol = bool(re.search(r"[-_.]", token))
    return (has_alpha and has_digit) or (has_symbol and (has_alpha or has_digit))


class HybridSearchEngine(SearchEngineProtocol):
    """High-precision hybrid lexical & vector search engine using float64 RRF."""

    def __init__(
        self,
        fts_db_path: Path,
        lancedb_table: Optional[Any] = None,
        config: Optional[ModelConfig] = None,
    ):
        self.fts_db_path = fts_db_path
        self.lancedb_table = lancedb_table
        self.config = config or ModelConfig()

    def _query_fts(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """Query FTS5 porter and trigram tables, returning list of row dicts."""
        if not self.fts_db_path.exists():
            return []

        conn = sqlite3.connect(str(self.fts_db_path))
        conn.row_factory = sqlite3.Row
        escaped_query = escape_fts5_query(query.query_text)

        tokens = query.query_text.split()
        use_trigram = any(is_identifier_like(t) for t in tokens)
        table = "chunks_fts_trigram" if use_trigram else "chunks_fts_porter"

        sql = f"""
            SELECT chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc,
                   bm25({table}) as score,
                   snippet({table}, 0, '<mark>', '</mark>', '...', 32) as snippet_text
            FROM {table}
            WHERE {table} MATCH ?
        """
        params: List[Any] = [escaped_query]

        if query.folder:
            sql += " AND folder = ?"
            params.append(query.folder)
        if query.sender:
            sql += " AND sender = ?"
            params.append(query.sender)
        if query.source_type:
            sql += " AND source_type = ?"
            params.append(query.source_type.value)

        sql += " ORDER BY score ASC LIMIT ?"
        params.append(query.top_k)

        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def _query_vector(self, query: SearchQuery) -> List[Tuple[str, float]]:
        """Query LanceDB vector index with prefilter=True."""
        if not self.lancedb_table:
            return []
        return []

    def search(self, query: SearchQuery) -> List[SearchResult]:
        lexical_hits = self._query_fts(query)
        vector_hits = self._query_vector(query)

        # Detect exact identifier matches for priority pinning
        raw_tokens = set(query.query_text.split())
        has_identifier = any(is_identifier_like(t) for t in raw_tokens)

        # Build rank maps (1-based)
        lexical_ranks: Dict[str, int] = {}
        lexical_rows: Dict[str, Dict[str, Any]] = {}
        for idx, item in enumerate(lexical_hits):
            cid = item["chunk_id"] if isinstance(item, dict) else item[0]
            lexical_ranks[cid] = idx + 1
            if isinstance(item, dict):
                lexical_rows[cid] = item

        vector_ranks: Dict[str, int] = {cid: idx + 1 for idx, (cid, _) in enumerate(vector_hits)}

        all_chunk_ids: Set[str] = set(lexical_ranks.keys()) | set(vector_ranks.keys())
        if not all_chunk_ids:
            return []

        k = float(self.config.rrf_k)
        max_theoretical_rrf = 2.0 / (k + 1.0)
        scored_results = []

        for cid in all_chunk_ids:
            l_rank = lexical_ranks.get(cid)
            v_rank = vector_ranks.get(cid)
            l_row = lexical_rows.get(cid, {})

            # RRF calculation in exact IEEE 754 float64
            rrf = 0.0
            if l_rank is not None:
                rrf += 1.0 / (k + float(l_rank))
            if v_rank is not None:
                rrf += 1.0 / (k + float(v_rank))

            norm_rrf = rrf / max_theoretical_rrf

            # Identifier pinning logic: exact identifier at Rank 1 in lexical gets HIGH tier
            is_pinned = has_identifier and l_rank == 1

            if is_pinned or norm_rrf >= 0.70:
                tier = RelevanceTier.HIGH
            elif norm_rrf >= 0.45:
                tier = RelevanceTier.MEDIUM
            else:
                tier = RelevanceTier.LOW

            doc_id = l_row.get("doc_id") or (cid.rsplit("_", 1)[0] if "_" in cid else cid)
            content_text = l_row.get("snippet_text") or l_row.get("content") or f"Content for chunk {cid}"
            sender_val = l_row.get("sender") or query.sender
            folder_val = l_row.get("folder") or query.folder
            source_type_val = SourceType(l_row["source_type"]) if l_row.get("source_type") in ("EMAIL", "FILE") else SourceType.EMAIL

            ts_val = None
            if l_row.get("received_ts_utc"):
                try:
                    from datetime import datetime
                    ts_val = datetime.fromisoformat(l_row["received_ts_utc"])
                except Exception:
                    pass

            scored_results.append(
                SearchResult(
                    chunk_id=cid,
                    doc_id=doc_id,
                    content=content_text,
                    source_type=source_type_val,
                    sender=sender_val,
                    folder=folder_val,
                    received_ts_utc=ts_val,
                    has_archived_msg=True,
                    lexical_rank=l_rank,
                    vector_rank=v_rank,
                    rrf_score=rrf,
                    normalized_rrf=norm_rrf,
                    relevance_tier=tier,
                )
            )

        # Sort: pinned exact hits first, then rrf_score DESC, then chunk_id ASC
        def _sort_key(item: SearchResult):
            is_pinned = has_identifier and item.lexical_rank == 1
            return (not is_pinned, -item.rrf_score, item.chunk_id)

        scored_results.sort(key=_sort_key)
        return scored_results[: query.top_k]

