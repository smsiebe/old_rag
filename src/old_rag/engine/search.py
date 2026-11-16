"""search.py - Escaped FTS5 & Vector Hybrid RRF Search with Exact Identifier Pinning."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from old_rag.contracts import (
    ModelConfig,
    RelevanceTier,
    SearchQuery,
    SearchResult,
    SourceType,
)
from old_rag.protocols import SearchEngineProtocol


FTS5_OPERATORS = re.compile(r'[*:\^"(){}]')
FTS5_BOOLEANS = {"AND", "OR", "NOT", "NEAR"}

ENGLISH_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but",
    "by", "can", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for",
    "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just",
    "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "with", "would", "you", "your", "yours", "yourself", "yourselves",
    # Common conversational query filler
    "give", "tell", "show", "find", "get", "look", "stuff", "things", "please",
}


def is_identifier_like(token: str) -> bool:
    """Return True if token contains digits, internal '-', '_', '.', or alphanumeric code patterns."""
    t = token.strip().strip('"').strip("'")
    if not t:
        return False
    has_internal_punct = bool(re.search(r"[a-zA-Z0-9][\-_.\/][a-zA-Z0-9]", t))
    has_digit = bool(re.search(r"\d", t))
    has_alpha = bool(re.search(r"[a-zA-Z]", t))
    return has_internal_punct or (has_digit and (has_alpha or bool(re.search(r"[\-_.]", t)))) or (has_digit and len(t) >= 2)


def escape_fts5_query(raw_query: str) -> str:
    """Sanitize and format query for chunks_fts_porter.
    
    Splits on whitespace first before stripping operators.
    Identifier tokens are wrapped in double quotes.
    Pure natural-language word tokens have operators/booleans stripped and are passed
    unquoted to activate the Porter stemmer.
    """
    raw_tokens = raw_query.strip().split()
    if not raw_tokens:
        return '""'

    escaped: List[str] = []
    for token in raw_tokens:
        clean_token = token.strip()
        if not clean_token:
            continue
        if is_identifier_like(clean_token):
            quoted = clean_token.replace('"', '""')
            escaped.append(f'"{quoted}"')
        else:
            cleaned = FTS5_OPERATORS.sub('', clean_token).strip("'-_.,;!?")
            if not cleaned or cleaned.upper() in FTS5_BOOLEANS:
                continue
            escaped.append(cleaned)

    if not escaped:
        return '""'
    return " ".join(escaped)


def extract_trigram_query(raw_query: str) -> str:
    """Extract identifier-like tokens of length >= 3 for chunks_fts_trigram matching."""
    raw_tokens = raw_query.strip().split()
    trigram_tokens: List[str] = []
    for token in raw_tokens:
        clean = token.strip().strip("'").strip('"')
        if is_identifier_like(clean) and len(clean) >= 3:
            quoted = clean.replace('"', '""')
            trigram_tokens.append(f'"{quoted}"')
    if not trigram_tokens:
        return ""
    return " ".join(trigram_tokens)


class HybridSearchEngine(SearchEngineProtocol):
    """High-precision hybrid lexical & vector search engine using float64 RRF."""

    def __init__(
        self,
        fts_db_path: Path,
        lancedb_table: Optional[Any] = None,
        config: Optional[ModelConfig] = None,
        resolver: Optional[Any] = None,
        catalog: Optional[Any] = None,
    ):
        self.fts_db_path = fts_db_path
        self.lancedb_table = lancedb_table
        self.config = config or ModelConfig()
        self.resolver = resolver
        self.catalog = catalog
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from old_rag.storage.models import ModelResolver
            res = self.resolver or ModelResolver(self.config)
            self._embedder = res.get_text_embedding_model()
        return self._embedder

    def _query_fts_table(
        self,
        conn: sqlite3.Connection,
        table: str,
        match_query: str,
        query: SearchQuery,
    ) -> List[Dict[str, Any]]:
        """Query a specific FTS5 table with parameter binding."""
        if not match_query or match_query == '""' or not match_query.strip():
            return []

        sql = f"""
            SELECT chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc,
                   bm25({table}) as score,
                   snippet({table}, 0, '<mark>', '</mark>', '...', 32) as snippet_text
            FROM {table}
            WHERE {table} MATCH ?
        """
        params: List[Any] = [match_query]

        if query.folder:
            sql += " AND folder = ?"
            params.append(query.folder)
        if query.sender:
            sql += " AND sender = ?"
            params.append(query.sender)

        effective_types = query.source_types or ([query.source_type] if query.source_type else None)
        if effective_types:
            placeholders = ",".join(["?"] * len(effective_types))
            sql += f" AND source_type IN ({placeholders})"
            params.extend([st.value if hasattr(st, "value") else str(st) for st in effective_types])

        sql += " ORDER BY score ASC LIMIT ?"
        params.append(query.top_k)

        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _query_fts(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """Execute Stage 1 Inner RRF merging chunks_fts_porter and chunks_fts_trigram."""
        if not self.fts_db_path.exists():
            return []

        conn = sqlite3.connect(str(self.fts_db_path))
        conn.row_factory = sqlite3.Row

        try:
            porter_q = escape_fts5_query(query.query_text)
            porter_hits = self._query_fts_table(conn, "chunks_fts_porter", porter_q, query)

            # Conversational filler fallback:
            # If porter_hits is empty, check if query contains non-stopword tokens.
            raw_tokens = query.query_text.strip().split()
            content_tokens = [
                FTS5_OPERATORS.sub('', t).strip("'-_.,;!?")
                for t in raw_tokens
                if t.lower().strip("'-_.,;!?") not in ENGLISH_STOPWORDS and not is_identifier_like(t)
            ]
            content_tokens = [t for t in content_tokens if t and t.upper() not in FTS5_BOOLEANS]

            if not porter_hits and content_tokens:
                clean_content_q = " ".join(content_tokens)
                porter_hits = self._query_fts_table(conn, "chunks_fts_porter", clean_content_q, query)
                if not porter_hits and len(content_tokens) > 1:
                    or_content_q = " OR ".join(content_tokens)
                    porter_hits = self._query_fts_table(conn, "chunks_fts_porter", or_content_q, query)

            trigram_q = extract_trigram_query(query.query_text)
            trigram_hits = self._query_fts_table(conn, "chunks_fts_trigram", trigram_q, query) if trigram_q else []

            # Inner RRF merging
            if not trigram_hits:
                return porter_hits
            if not porter_hits:
                return trigram_hits

            k_inner = 60.0
            porter_ranks = {hit["chunk_id"]: idx + 1 for idx, hit in enumerate(porter_hits)}
            trigram_ranks = {hit["chunk_id"]: idx + 1 for idx, hit in enumerate(trigram_hits)}

            all_hits: Dict[str, Dict[str, Any]] = {}
            for hit in porter_hits:
                all_hits[hit["chunk_id"]] = dict(hit)
            for hit in trigram_hits:
                if hit["chunk_id"] not in all_hits:
                    all_hits[hit["chunk_id"]] = dict(hit)

            scored = []
            for cid, hit in all_hits.items():
                p_rank = porter_ranks.get(cid)
                t_rank = trigram_ranks.get(cid)
                inner_rrf = 0.0
                if p_rank is not None:
                    inner_rrf += 1.0 / (k_inner + float(p_rank))
                if t_rank is not None:
                    inner_rrf += 1.0 / (k_inner + float(t_rank))
                hit["inner_rrf_score"] = inner_rrf
                scored.append(hit)

            scored.sort(key=lambda h: (-h.get("inner_rrf_score", 0.0), h.get("chunk_id", "")))
            return scored[: query.top_k]
        except Exception:
            return []
        finally:
            conn.close()

    def _query_vector(self, query: SearchQuery) -> Tuple[List[Tuple[str, float]], Dict[str, Dict[str, Any]]]:
        """Query LanceDB vector index with prefilter=True and cosine metric."""
        if self.lancedb_table is None:
            return [], {}

        try:
            embedder = self._get_embedder()
            prefixed = f"search_query: {query.query_text}"
            raw_vec = list(embedder.embed([prefixed]))[0]

            from old_rag.engine.indexer import matryoshka_reduce
            reduced = matryoshka_reduce(np.array(raw_vec, dtype=np.float32), self.config.matryoshka_dim).tolist()

            search_builder = self.lancedb_table.search(reduced).metric("cosine").limit(query.top_k)

            where_clauses = []
            if query.folder:
                where_clauses.append(f"folder = '{query.folder}'")
            if query.sender:
                where_clauses.append(f"sender = '{query.sender}'")

            effective_types = query.source_types or ([query.source_type] if query.source_type else None)
            if effective_types:
                st_clauses = [f"source_type = '{st.value if hasattr(st, 'value') else st}'" for st in effective_types]
                where_clauses.append(f"({' OR '.join(st_clauses)})")

            if where_clauses:
                search_builder = search_builder.where(" AND ".join(where_clauses), prefilter=True)

            records = search_builder.to_list()
            vector_hits: List[Tuple[str, float]] = []
            vector_rows: Dict[str, Dict[str, Any]] = {}
            for r in records:
                cid = str(r["chunk_id"])
                dist = float(r.get("_distance", 1.0))
                # Cosine similarity in LanceDB with cosine distance d is 1.0 - d
                cos_sim = float(1.0 - dist)
                vector_hits.append((cid, cos_sim))
                vector_rows[cid] = dict(r)
            return vector_hits, vector_rows
        except Exception:
            return [], {}

    def search(self, query: SearchQuery) -> List[SearchResult]:
        lexical_hits = self._query_fts(query)
        vector_res = self._query_vector(query)
        if isinstance(vector_res, tuple) and len(vector_res) == 2:
            vector_hits, vector_rows = vector_res
        else:
            vector_hits, vector_rows = vector_res, {}

        # Detect exact identifier matches for priority pinning
        raw_tokens = [t.strip().strip("'").strip('"') for t in query.query_text.strip().split()]
        identifier_tokens = [t for t in raw_tokens if is_identifier_like(t)]
        has_identifier = len(identifier_tokens) > 0

        # Build rank maps (1-based)
        lexical_ranks: Dict[str, int] = {}
        lexical_rows: Dict[str, Dict[str, Any]] = {}
        for idx, item in enumerate(lexical_hits):
            cid = item["chunk_id"] if isinstance(item, dict) else item[0]
            lexical_ranks[cid] = idx + 1
            if isinstance(item, dict):
                lexical_rows[cid] = item

        vector_ranks: Dict[str, int] = {cid: idx + 1 for idx, (cid, _) in enumerate(vector_hits)}
        vector_sims: Dict[str, float] = {cid: sim for cid, sim in vector_hits}

        all_chunk_ids: Set[str] = set(lexical_ranks.keys()) | set(vector_ranks.keys())
        if not all_chunk_ids:
            return []

        k = float(self.config.rrf_k)
        max_theoretical_rrf = 2.0 / (k + 1.0)
        scored_results: List[SearchResult] = []

        # Batch fetch document metadata (e.g. subjects, original paths) from catalog if available
        doc_metas: Dict[str, Dict[str, Any]] = {}
        if self.catalog and hasattr(self.catalog, "get_documents_by_ids"):
            try:
                candidate_doc_ids = list({
                    lexical_rows.get(c, {}).get("doc_id") or vector_rows.get(c, {}).get("doc_id") or (c.rsplit("_", 1)[0] if "_" in c else c)
                    for c in all_chunk_ids
                })
                doc_metas = self.catalog.get_documents_by_ids(candidate_doc_ids)
            except Exception:
                pass

        # Check document_entities matching in catalog for identifier tokens
        entity_boosted_cids: Set[str] = set()
        if self.catalog and hasattr(self.catalog, "_get_connection"):
            try:
                conn = self.catalog._get_connection()
                for tok in identifier_tokens:
                    if len(tok) >= 2:
                        cur = conn.execute(
                            "SELECT DISTINCT doc_id FROM document_entities WHERE entity_value LIKE ?",
                            (f"%{tok}%",),
                        )
                        for row in cur.fetchall():
                            matched_did = row[0]
                            for cid in all_chunk_ids:
                                if cid == matched_did or cid.startswith(f"{matched_did}_"):
                                    entity_boosted_cids.add(cid)
            except Exception:
                pass

        for cid in all_chunk_ids:
            l_rank = lexical_ranks.get(cid)
            v_rank = vector_ranks.get(cid)
            l_row = lexical_rows.get(cid, {})
            v_row = vector_rows.get(cid, {})

            # Exact IEEE 754 float64 RRF calculation
            rrf = 0.0
            if l_rank is not None:
                rrf += 1.0 / (k + float(l_rank))
            if v_rank is not None:
                rrf += 1.0 / (k + float(v_rank))

            norm_rrf = rrf / max_theoretical_rrf

            doc_id = l_row.get("doc_id") or v_row.get("doc_id") or (cid.rsplit("_", 1)[0] if "_" in cid else cid)
            doc_meta = doc_metas.get(doc_id, {})
            subject_val = doc_meta.get("subject")
            orig_path = doc_meta.get("original_path") or l_row.get("original_path") or v_row.get("original_path")

            content_text = l_row.get("snippet_text") or l_row.get("content") or v_row.get("content") or f"Content for chunk {cid}"
            sender_val = doc_meta.get("sender") or l_row.get("sender") or v_row.get("sender") or query.sender
            folder_val = doc_meta.get("folder") or l_row.get("folder") or v_row.get("folder") or query.folder

            raw_st = doc_meta.get("source_type") or l_row.get("source_type") or v_row.get("source_type")
            try:
                source_type_val = SourceType(raw_st) if raw_st else SourceType.EMAIL
            except ValueError:
                source_type_val = SourceType.EMAIL

            # Check exact substring match for identifier tokens
            is_exact_ident = cid in entity_boosted_cids
            if not is_exact_ident and has_identifier:
                content_lower = (l_row.get("content") or v_row.get("content") or content_text).lower()
                for ident in identifier_tokens:
                    clean_id = ident.lower()
                    if clean_id and clean_id in content_lower:
                        is_exact_ident = True
                        break

            cos_sim_val = vector_sims.get(cid)

            # Calibrate confidence score (0.0 to 1.0)
            sem_strength = 0.0
            if cos_sim_val is not None:
                sem_strength = min(1.0, max(0.0, (cos_sim_val - 0.40) / 0.32))

            if is_exact_ident:
                confidence = 1.0
            elif l_rank is not None and v_rank is not None:
                confidence = min(1.0, max(norm_rrf, 0.65 + 0.35 * norm_rrf))
            elif v_rank is not None:
                confidence = max(norm_rrf, sem_strength)
            else:
                confidence = norm_rrf

            if is_exact_ident or confidence >= 0.65:
                tier = RelevanceTier.HIGH
            elif confidence >= 0.45:
                tier = RelevanceTier.MEDIUM
            else:
                tier = RelevanceTier.LOW

            ts_val = None
            ts_str = doc_meta.get("received_ts_utc") or l_row.get("received_ts_utc") or v_row.get("received_ts_utc")
            if ts_str:
                try:
                    from datetime import datetime
                    ts_val = datetime.fromisoformat(ts_str)
                except Exception:
                    pass

            scored_results.append(
                SearchResult(
                    chunk_id=cid,
                    doc_id=doc_id,
                    content=content_text,
                    source_type=source_type_val,
                    subject=subject_val,
                    sender=sender_val,
                    folder=folder_val,
                    received_ts_utc=ts_val,
                    original_path=orig_path,
                    has_archived_msg=True,
                    lexical_rank=l_rank,
                    vector_rank=v_rank,
                    rrf_score=rrf,
                    normalized_rrf=norm_rrf,
                    relevance_tier=tier,
                    cosine_similarity=cos_sim_val,
                    confidence_score=confidence,
                    exact_identifier_match=is_exact_ident,
                )
            )

        # Sort: pinned exact matches first, then rrf_score DESC, then chunk_id ASC
        def _sort_key(item: SearchResult):
            return (not item.exact_identifier_match, -item.rrf_score, item.chunk_id)

        scored_results.sort(key=_sort_key)
        final_results = scored_results[: query.top_k]

        if self.catalog is not None and final_results:
            try:
                top_hit = final_results[0]
                if top_hit.lexical_rank is not None and top_hit.vector_rank is not None:
                    resolved_by = "hybrid"
                elif top_hit.vector_rank is not None:
                    resolved_by = "vector"
                else:
                    resolved_by = "lexical"
                self.catalog.record_query_metric(
                    query_text=query.query_text,
                    resolved_by=resolved_by,
                    hit_count=len(final_results),
                    top_confidence=float(top_hit.confidence_score or top_hit.normalized_rrf),
                )
            except Exception:
                pass

        return final_results


def resolve_related_context(
    search_engine: Any,
    catalog: Optional[Any] = None,
    calendar_manager: Optional[Any] = None,
    doc_id: Optional[str] = None,
    subject: str = "",
    text: str = "",
    top_k: int = 6,
) -> Dict[str, Any]:
    """Extract ambient related files, emails, and calendar events for a given message or event context."""
    # 1. Clean subject
    clean_subj = re.sub(r"^(Re|Fwd|Fw|Aw|Wg|Antw|R)\s*:\s*", "", subject or "", flags=re.IGNORECASE).strip()
    raw_subj_tokens = [t.strip() for t in re.split(r"\W+", clean_subj) if len(t.strip()) >= 2]
    subj_tokens = [t for t in raw_subj_tokens if t.lower() not in ENGLISH_STOPWORDS]

    # 2. Extract keywords from text if needed
    text_tokens = []
    if text:
        raw_text_tokens = [t.strip() for t in re.split(r"\W+", text[:400]) if len(t.strip()) >= 3]
        text_tokens = [t for t in raw_text_tokens if t.lower() not in ENGLISH_STOPWORDS]

    if subj_tokens:
        query_terms = subj_tokens[:6]
    elif text_tokens:
        query_terms = text_tokens[:6]
    elif raw_subj_tokens:
        query_terms = raw_subj_tokens[:4]
    else:
        query_terms = []

    query_str = " ".join(query_terms)
    if not query_str:
        return {
            "success": True,
            "related_files": [],
            "related_emails": [],
            "related_chats": [],
            "related_calendar": [],
            "query_used": "",
        }

    # 3. Hybrid search
    hits = search_engine.search(SearchQuery(query_text=query_str, top_k=top_k * 3))

    related_files = []
    related_emails = []
    related_chats = []
    related_cal = []
    seen_doc_ids = set()
    if doc_id:
        seen_doc_ids.add(doc_id)

    for h in hits:
        if h.doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(h.doc_id)

        h_dict = h.model_dump(mode="json")
        if h.source_type == SourceType.CHAT:
            related_chats.append(h_dict)
        elif h.source_type == SourceType.CALENDAR:
            related_cal.append(h_dict)
        elif h.source_type == SourceType.FILE:
            related_files.append(h_dict)
        else:
            is_file = bool(
                h.original_path
                and not h.original_path.startswith("Outlook://")
                and not h.original_path.startswith("mailbox://")
                and not h.original_path.endswith(".msg")
                and (h.original_path.startswith("/") or (len(h.original_path) > 2 and h.original_path[1] == ":") or h.original_path.startswith("\\\\"))
            )
            if is_file:
                related_files.append(h_dict)
            else:
                related_emails.append(h_dict)

    # 4. Search calendar
    if calendar_manager:
        try:
            cal_events = calendar_manager.search_events(query_str, limit=top_k)
            for evt in cal_events:
                if doc_id and evt.event_id == doc_id:
                    continue
                evt_dict = evt.model_dump(mode="json")
                if not any(c.get("doc_id") == evt.doc_id or c.get("event_id") == evt.event_id for c in related_cal):
                    related_cal.append(evt_dict)
        except Exception:
            pass

    return {
        "success": True,
        "related_files": related_files[:top_k],
        "related_emails": related_emails[:top_k],
        "related_chats": related_chats[:top_k],
        "related_calendar": related_cal[:top_k],
        "query_used": query_str,
    }


def resolve_open_document(
    cas: Optional[Any] = None,
    catalog: Optional[Any] = None,
    doc_id: Optional[str] = None,
    original_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a document or cached message using the local operating system handler."""
    import os
    import sys

    target_path = None
    if original_path and Path(original_path).exists():
        target_path = Path(original_path)
    elif doc_id:
        if cas and cas.has_msg(doc_id):
            target_path = cas.create_temp_view_file(doc_id, suffix=".msg")
        elif catalog:
            doc = catalog.get_document(doc_id)
            if doc and doc.get("original_path") and Path(doc["original_path"]).exists():
                target_path = Path(doc["original_path"])
        if not target_path and cas:
            target_path = cas.create_temp_view_file(doc_id, suffix=".msg")

    if target_path and target_path.exists():
        if sys.platform == "win32":
            try:
                os.startfile(str(target_path))
            except Exception as e:
                return {"success": False, "error": str(e), "path": str(target_path)}
        return {"success": True, "path": str(target_path)}

    return {"success": False, "error": "Document not found or inaccessible"}
