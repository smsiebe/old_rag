"""explainer.py - Two-Stage Lazy Relevance & Scoring Explanations (Tier 1 Deterministic + Tier 2 AuraRouter)."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from old_rag.contracts import safe_json_dumps
from old_rag.engine.search import (
    ENGLISH_STOPWORDS,
    FTS5_BOOLEANS,
    FTS5_OPERATORS,
    escape_fts5_query,
    extract_trigram_query,
    is_identifier_like,
)


_META_COMMENT_RE = re.compile(r"\n?<!--\s*_aura_routing_context:.*?-->", re.DOTALL)

_TIER2_DEFAULT_ROLES = ("assist", "summarization", "reasoning", "coding")


def _json_safe_scalar(val: Any) -> Any:
    """Convert datetime/date/Path/Enum scalars into JSON-native primitives."""
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Path):
        return str(val)
    if hasattr(val, "value"):
        return _json_safe_scalar(val.value)
    return str(val)


def _json_safe_value(val: Any) -> Any:
    """Recursively ensure dicts/lists contain only JSON-serializable values."""
    if isinstance(val, dict):
        return {str(k): _json_safe_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_json_safe_value(v) for v in val]
    return _json_safe_scalar(val)


def _clean_natural_token(token: str) -> str:
    """Strip punctuation/FTS5 operators cleanly for single-token FTS5 probing."""
    cleaned = FTS5_OPERATORS.sub("", token).strip("'-_.,;!?/\\")
    cleaned = re.sub(r"[^\w]", "", cleaned)
    return cleaned


def _extract_query_breakdown(raw_query: str) -> Dict[str, Any]:
    """Mirror HybridSearchEngine tokenization and escaping logic for inspection."""
    q = (raw_query or "").strip()
    raw_tokens = q.split() if q else []
    identifier_tokens: List[str] = []
    natural_tokens: List[str] = []
    content_tokens: List[str] = []
    stopwords_removed: List[str] = []

    for tok in raw_tokens:
        clean_tok = tok.strip()
        if not clean_tok:
            continue
        stripped = clean_tok.strip().strip('"').strip("'")
        if is_identifier_like(stripped):
            if stripped not in identifier_tokens:
                identifier_tokens.append(stripped)
            continue

        cleaned = _clean_natural_token(clean_tok)
        if not cleaned or cleaned.upper() in FTS5_BOOLEANS:
            continue
        natural_tokens.append(cleaned)
        if cleaned.lower() in ENGLISH_STOPWORDS:
            stopwords_removed.append(cleaned)
        else:
            content_tokens.append(cleaned)

    return {
        "query_text": q,
        "raw_tokens": raw_tokens,
        "natural_tokens": natural_tokens,
        "content_tokens": content_tokens,
        "stopwords_removed": stopwords_removed,
        "identifier_tokens": identifier_tokens,
        "escaped_porter_query": escape_fts5_query(q) if q else '""',
        "trigram_query": extract_trigram_query(q) if q else "",
    }


def _highlight_substrings_outside_marks(text: str, needles: List[str]) -> str:
    """Wrap exact case-insensitive substrings with <mark> without nesting inside existing <mark> tags."""
    if not text or not needles:
        return text
    valid = sorted({n for n in needles if n and len(n) >= 2}, key=len, reverse=True)
    if not valid:
        return text

    parts = re.split(r"(<mark>.*?</mark>)", text, flags=re.DOTALL)
    pattern = re.compile("(" + "|".join(re.escape(n) for n in valid) + ")", re.IGNORECASE)
    out: List[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            # Already inside <mark>...</mark>
            out.append(part)
        else:
            out.append(pattern.sub(r"<mark>\1</mark>", part))
    return "".join(out)


def _compute_scoring_breakdown(snapshot: Dict[str, Any], rrf_k: float) -> Dict[str, Any]:
    """Recompute exact RRF, normalization, semantic calibration, and tier thresholds."""
    k = float(rrf_k or 60.0)
    l_rank = snapshot.get("lexical_rank")
    v_rank = snapshot.get("vector_rank")
    cos_sim = snapshot.get("cosine_similarity")
    exact_id = bool(snapshot.get("exact_identifier_match", False))

    l_rank_int = int(l_rank) if l_rank is not None else None
    v_rank_int = int(v_rank) if v_rank is not None else None
    cos_float = float(cos_sim) if cos_sim is not None else None

    lexical_rrf = (1.0 / (k + float(l_rank_int))) if l_rank_int is not None else 0.0
    vector_rrf = (1.0 / (k + float(v_rank_int))) if v_rank_int is not None else 0.0
    max_theoretical_rrf = 2.0 / (k + 1.0)

    rrf_score = snapshot.get("rrf_score")
    if rrf_score is None:
        rrf_score = lexical_rrf + vector_rrf
    else:
        rrf_score = float(rrf_score)

    norm_rrf = snapshot.get("normalized_rrf")
    if norm_rrf is None:
        norm_rrf = (rrf_score / max_theoretical_rrf) if max_theoretical_rrf > 0 else 0.0
    else:
        norm_rrf = float(norm_rrf)

    sem_strength: Optional[float] = None
    if cos_float is not None:
        sem_strength = min(1.0, max(0.0, (cos_float - 0.40) / 0.32))

    if exact_id:
        calibration_rule = "Exact Identifier Pinning"
        calibration_formula = "exact_identifier_match == True -> 1.000 (Pinned Rank #1)"
        computed_conf = 1.0
    elif l_rank_int is not None and v_rank_int is not None:
        calibration_rule = "Dual-Engine Synergy Boost"
        computed_conf = min(1.0, max(norm_rrf, 0.65 + 0.35 * norm_rrf))
        calibration_formula = (
            f"min(1.0, max(norm_rrf={norm_rrf:.3f}, 0.65 + 0.35 * {norm_rrf:.3f})) = {computed_conf:.3f}"
        )
    elif v_rank_int is not None:
        calibration_rule = "Semantic Cosine Calibration"
        s_val = sem_strength if sem_strength is not None else 0.0
        computed_conf = max(norm_rrf, s_val)
        calibration_formula = (
            f"max(norm_rrf={norm_rrf:.3f}, sem_strength={s_val:.3f}) = {computed_conf:.3f}"
        )
    else:
        calibration_rule = "Lexical Normalized RRF"
        computed_conf = norm_rrf
        calibration_formula = f"norm_rrf = {rrf_score:.6f} / {max_theoretical_rrf:.6f} = {norm_rrf:.3f}"

    raw_conf = snapshot.get("confidence_score")
    final_conf = float(raw_conf) if raw_conf is not None else computed_conf

    tier = snapshot.get("relevance_tier")
    if not tier:
        if exact_id or final_conf >= 0.65:
            tier = "HIGH"
        elif final_conf >= 0.45:
            tier = "MEDIUM"
        else:
            tier = "LOW"

    return {
        "rrf_k": k,
        "lexical_rank": l_rank_int,
        "vector_rank": v_rank_int,
        "lexical_rrf_contribution": round(lexical_rrf, 6),
        "vector_rrf_contribution": round(vector_rrf, 6),
        "rrf_score": round(rrf_score, 6),
        "max_theoretical_rrf": round(max_theoretical_rrf, 6),
        "normalized_rrf": round(norm_rrf, 4),
        "cosine_similarity": round(cos_float, 4) if cos_float is not None else None,
        "semantic_strength": round(sem_strength, 4) if sem_strength is not None else None,
        "confidence_score": round(final_conf, 4),
        "confidence_pct": int(round(final_conf * 100)),
        "relevance_tier": str(tier),
        "exact_identifier_match": exact_id,
        "calibration_rule": calibration_rule,
        "calibration_formula": calibration_formula,
        "tier_thresholds": "HIGH >= 65% (or Exact ID), MEDIUM >= 45%, LOW < 45%",
    }


def _explain_search_hit_tier1(
    fts_db_path: Optional[Path],
    catalog: Optional[Any],
    cas: Optional[Any],
    rrf_k: float,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build deterministic Tier 1 evidence for a hybrid search hit (EMAIL or FILE)."""
    snapshot = dict(payload.get("score_snapshot") or {})
    doc_id = str(payload.get("doc_id") or snapshot.get("doc_id") or "").strip()
    chunk_id = str(payload.get("chunk_id") or snapshot.get("chunk_id") or "").strip()
    query_text = str(payload.get("query_text") or "").strip()
    source_context = dict(payload.get("source_context") or {})

    q_info = _extract_query_breakdown(query_text)
    scoring = _compute_scoring_breakdown(snapshot, rrf_k=rrf_k)

    # Enrich metadata from catalog & CAS if available
    doc_meta: Dict[str, Any] = {}
    if doc_id and catalog is not None and hasattr(catalog, "get_document"):
        try:
            doc_meta = catalog.get_document(doc_id) or {}
        except Exception:
            doc_meta = {}

    cas_doc = None
    if doc_id and cas is not None and hasattr(cas, "get_text"):
        try:
            cas_doc = cas.get_text(doc_id)
        except Exception:
            cas_doc = None

    subject = _json_safe_scalar(
        snapshot.get("subject") or doc_meta.get("subject") or (cas_doc.subject if cas_doc else None)
    )
    sender = _json_safe_scalar(
        snapshot.get("sender") or doc_meta.get("sender") or (cas_doc.sender if cas_doc else None)
    )
    folder = _json_safe_scalar(
        snapshot.get("folder") or doc_meta.get("folder") or (cas_doc.folder if cas_doc else None)
    )
    original_path = _json_safe_scalar(
        snapshot.get("original_path")
        or doc_meta.get("original_path")
        or (cas_doc.original_path if cas_doc else None)
    )
    source_type = _json_safe_scalar(
        snapshot.get("source_type")
        or doc_meta.get("source_type")
        or (cas_doc.source_type.value if cas_doc else "EMAIL")
    )
    received_ts_utc = _json_safe_scalar(
        snapshot.get("received_ts_utc")
        or doc_meta.get("received_ts_utc")
        or (cas_doc.received_ts_utc if cas_doc else None)
    )

    attachment_names: List[str] = []
    if cas_doc and getattr(cas_doc, "attachments", None):
        attachment_names = [a.filename for a in cas_doc.attachments if getattr(a, "filename", None)]

    chat_info = None
    if source_type == "CHAT" or (cas_doc and getattr(cas_doc, "chat_meta", None)):
        if cas_doc and getattr(cas_doc, "chat_meta", None):
            chat_info = cas_doc.chat_meta.model_dump(mode="json")
        elif catalog and hasattr(catalog, "_get_connection"):
            try:
                conn = catalog._get_connection()
                cur = conn.execute("SELECT * FROM chat_messages WHERE doc_id = ? LIMIT 1", (doc_id,))
                c_row = cur.fetchone()
                if c_row:
                    chat_info = dict(c_row)
            except Exception:
                pass

    calendar_info = None
    if source_type == "CALENDAR" or (cas_doc and getattr(cas_doc, "calendar_meta", None)):
        if cas_doc and getattr(cas_doc, "calendar_meta", None):
            calendar_info = cas_doc.calendar_meta.model_dump(mode="json")
        elif catalog and hasattr(catalog, "_get_connection"):
            try:
                conn = catalog._get_connection()
                cur = conn.execute("SELECT * FROM calendar_events WHERE doc_id = ? OR event_id = ? LIMIT 1", (doc_id, doc_id))
                cal_row = cur.fetchone()
                if cal_row:
                    calendar_info = dict(cal_row)
            except Exception:
                pass

    # Query lexical_fts.db for unclipped chunk text & per-token FTS5 evidence
    full_content = ""
    highlighted_passage = ""
    bm25_score: Optional[float] = None
    porter_strategy = "Not Checked"
    trigram_matched = False
    matched_tokens: List[str] = []
    unmatched_tokens: List[str] = []
    matched_identifiers: List[str] = []

    if fts_db_path is not None and Path(fts_db_path).exists() and (chunk_id or doc_id):
        conn = None
        try:
            conn = sqlite3.connect(str(fts_db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            row = None
            if chunk_id:
                row = conn.execute(
                    "SELECT chunk_id, doc_id, content FROM chunks_fts_porter WHERE chunk_id = ? LIMIT 1",
                    (chunk_id,),
                ).fetchone()
            if row is None and doc_id:
                row = conn.execute(
                    "SELECT chunk_id, doc_id, content FROM chunks_fts_porter WHERE doc_id = ? LIMIT 1",
                    (doc_id,),
                ).fetchone()
            if row is not None:
                chunk_id = chunk_id or str(row["chunk_id"])
                full_content = str(row["content"] or "")

                # Probe each content token & natural token individually via Porter stemmer
                probe_tokens = q_info["content_tokens"] or [
                    t for t in q_info["natural_tokens"] if t
                ]
                seen_probe = set()
                for tok in probe_tokens:
                    tok_low = tok.lower()
                    if tok_low in seen_probe:
                        continue
                    seen_probe.add(tok_low)
                    try:
                        hit = conn.execute(
                            "SELECT 1 FROM chunks_fts_porter WHERE chunk_id = ? AND chunks_fts_porter MATCH ? LIMIT 1",
                            (chunk_id, tok),
                        ).fetchone()
                        if hit is not None:
                            matched_tokens.append(tok)
                        else:
                            unmatched_tokens.append(tok)
                    except Exception:
                        unmatched_tokens.append(tok)

                # Determine exact FTS5 query strategy & BM25 score
                full_porter_q = q_info["escaped_porter_query"]
                hl_row = None
                if full_porter_q and full_porter_q != '""':
                    try:
                        hl_row = conn.execute(
                            "SELECT highlight(chunks_fts_porter, 0, '<mark>', '</mark>') AS hl, "
                            "bm25(chunks_fts_porter) AS score "
                            "FROM chunks_fts_porter WHERE chunk_id = ? AND chunks_fts_porter MATCH ? LIMIT 1",
                            (chunk_id, full_porter_q),
                        ).fetchone()
                        if hl_row is not None:
                            porter_strategy = "Full Query AND (Porter Stemmer)"
                    except Exception:
                        hl_row = None

                if hl_row is None and q_info["content_tokens"]:
                    and_q = " ".join(q_info["content_tokens"])
                    try:
                        hl_row = conn.execute(
                            "SELECT highlight(chunks_fts_porter, 0, '<mark>', '</mark>') AS hl, "
                            "bm25(chunks_fts_porter) AS score "
                            "FROM chunks_fts_porter WHERE chunk_id = ? AND chunks_fts_porter MATCH ? LIMIT 1",
                            (chunk_id, and_q),
                        ).fetchone()
                        if hl_row is not None:
                            porter_strategy = "Content Tokens AND (Stopwords Filtered, Porter Stemmer)"
                    except Exception:
                        hl_row = None

                if hl_row is None and len(matched_tokens) >= 1:
                    or_q = " OR ".join(matched_tokens)
                    try:
                        hl_row = conn.execute(
                            "SELECT highlight(chunks_fts_porter, 0, '<mark>', '</mark>') AS hl, "
                            "bm25(chunks_fts_porter) AS score "
                            "FROM chunks_fts_porter WHERE chunk_id = ? AND chunks_fts_porter MATCH ? LIMIT 1",
                            (chunk_id, or_q),
                        ).fetchone()
                        if hl_row is not None:
                            porter_strategy = "Partial Token OR (Porter Stemmer)"
                    except Exception:
                        hl_row = None

                # Trigram identifier probe (run before final strategy label)
                if q_info["identifier_tokens"]:
                    content_low = full_content.lower()
                    for ident in q_info["identifier_tokens"]:
                        if ident.lower() in content_low:
                            matched_identifiers.append(ident)
                    trigram_q = q_info["trigram_query"]
                    if trigram_q:
                        try:
                            t_row = conn.execute(
                                "SELECT 1 FROM chunks_fts_trigram WHERE chunk_id = ? AND chunks_fts_trigram MATCH ? LIMIT 1",
                                (chunk_id, trigram_q),
                            ).fetchone()
                            trigram_matched = t_row is not None
                        except Exception:
                            trigram_matched = False

                if hl_row is not None:
                    highlighted_passage = str(hl_row["hl"] or full_content)
                    try:
                        bm25_score = round(float(hl_row["score"]), 4)
                    except Exception:
                        bm25_score = None
                    if not matched_tokens and (trigram_matched or matched_identifiers):
                        porter_strategy = "Exact Trigram Identifier + Quoted Phrase Match"
                    elif trigram_matched or matched_identifiers:
                        porter_strategy = f"{porter_strategy} + Trigram Identifier"
                else:
                    highlighted_passage = full_content
                    if trigram_matched or matched_identifiers or scoring["exact_identifier_match"]:
                        porter_strategy = "Exact Trigram Identifier Match (Porter Stemmer Bypassed)"
                    elif scoring["vector_rank"] is not None:
                        porter_strategy = "Semantic Vector Only (0 stemmed token hits in chunk)"
                    else:
                        porter_strategy = "No direct Porter FTS match on chunk"
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # Fallback passage if FTS db unavailable or chunk not in FTS
    if not full_content:
        snap_content = str(snapshot.get("content") or "")
        if snap_content:
            full_content = re.sub(r"</?mark>", "", snap_content)
            highlighted_passage = snap_content
        elif cas_doc and getattr(cas_doc, "clean_body_text", ""):
            full_content = cas_doc.clean_body_text[:2400]
            highlighted_passage = full_content
        else:
            full_content = ""
            highlighted_passage = ""

        # Plain fallback token check if we didn't have FTS db
        if not matched_tokens and full_content:
            low = full_content.lower()
            for tok in q_info["content_tokens"] or q_info["natural_tokens"]:
                if tok.lower() in low:
                    matched_tokens.append(tok)
                else:
                    unmatched_tokens.append(tok)
            for ident in q_info["identifier_tokens"]:
                if ident.lower() in low:
                    matched_identifiers.append(ident)
            if not ("<mark>" in highlighted_passage):
                highlighted_passage = _highlight_substrings_outside_marks(
                    full_content, matched_tokens + matched_identifiers
                )

    if matched_identifiers:
        highlighted_passage = _highlight_substrings_outside_marks(highlighted_passage, matched_identifiers)

    # Active channels list
    channels_matched: List[str] = []
    if scoring["exact_identifier_match"] or trigram_matched or matched_identifiers:
        channels_matched.append("TRIGRAM_EXACT_ID")
    if scoring["lexical_rank"] is not None or len(matched_tokens) > 0:
        channels_matched.append("FTS5_PORTER")
    if scoring["vector_rank"] is not None or scoring["cosine_similarity"] is not None:
        channels_matched.append("LANCEDB_VECTOR")

    # Summary bullets
    summary_bullets: List[str] = []
    if source_context.get("origin_subject"):
        summary_bullets.append(
            f"Surfaced as ambient context related to '{source_context['origin_subject']}' using extracted keywords \"{query_text}\"."
        )
    elif query_text:
        summary_bullets.append(f"Evaluated against query \"{query_text}\".")

    if scoring["exact_identifier_match"]:
        id_label = ", ".join(matched_identifiers or q_info["identifier_tokens"])
        summary_bullets.append(
            f"Exact Identifier Pinning triggered for [{id_label}] (confidence forced to 100%, pinned at Rank #1)."
        )

    if scoring["lexical_rank"] is not None and scoring["vector_rank"] is not None:
        summary_bullets.append(
            f"Dual-Engine Hybrid hit: ranked #{scoring['lexical_rank']} lexically (FTS5 Porter) and #{scoring['vector_rank']} semantically (LanceDB Nomic), earning a Synergy Boost to {scoring['confidence_pct']}% ({scoring['relevance_tier']})."
        )
    elif scoring["vector_rank"] is not None:
        cos_txt = (
            f"cosine similarity {scoring['cosine_similarity']:.3f} (semantic strength {round((scoring['semantic_strength'] or 0.0) * 100)}%)"
            if scoring["cosine_similarity"] is not None
            else "vector similarity"
        )
        summary_bullets.append(
            f"Matched via LanceDB Semantic Vector engine at Rank #{scoring['vector_rank']} with {cos_txt}."
        )
    elif scoring["lexical_rank"] is not None:
        bm25_txt = f" (BM25 {bm25_score})" if bm25_score is not None else ""
        summary_bullets.append(
            f"Matched via SQLite FTS5 Porter lexical search at Rank #{scoring['lexical_rank']}{bm25_txt}."
        )
    else:
        summary_bullets.append(
            f"Scored at {scoring['confidence_pct']}% ({scoring['relevance_tier']}) via {scoring['calibration_rule']}."
        )

    if matched_tokens:
        summary_bullets.append(
            f"Porter-stemmed keyword terms matched in chunk: {', '.join(matched_tokens)}."
        )
    elif q_info["content_tokens"] and not scoring["exact_identifier_match"]:
        summary_bullets.append(
            f"No exact/stemmed occurrences of [{', '.join(q_info['content_tokens'])}] appeared verbatim in this chunk — retrieved by conceptual vector similarity."
        )

    return {
        "success": True,
        "item_kind": "search_hit",
        "source_context": _json_safe_value(source_context),
        "document": {
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "source_type": source_type,
            "title": str(subject or (Path(str(original_path)).name if original_path else None) or doc_id or "Archived Item"),
            "subject": subject,
            "sender": sender,
            "folder": folder,
            "original_path": original_path,
            "received_ts_utc": received_ts_utc,
            "attachments": _json_safe_value(attachment_names),
            "chat_meta": _json_safe_value(chat_info),
            "calendar_meta": _json_safe_value(calendar_info),
        },
        "query_analysis": q_info,
        "channels_matched": channels_matched,
        "scoring": scoring,
        "lexical_evidence": {
            "strategy": porter_strategy,
            "bm25_score": bm25_score,
            "matched_tokens": matched_tokens,
            "unmatched_tokens": unmatched_tokens,
            "matched_identifiers": matched_identifiers,
            "trigram_matched": trigram_matched,
        },
        "passage": {
            "raw_text": full_content,
            "highlighted_html": highlighted_passage,
            "char_length": len(full_content),
        },
        "summary_bullets": summary_bullets,
    }


def _explain_calendar_event_tier1(
    calendar_manager: Optional[Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build deterministic Tier 1 evidence for an Ambient Related Calendar Event."""
    snapshot = dict(payload.get("score_snapshot") or {})
    event_id = str(payload.get("event_id") or snapshot.get("event_id") or "").strip()
    query_text = str(payload.get("query_text") or "").strip()
    source_context = dict(payload.get("source_context") or {})

    evt_dict = dict(snapshot)
    if event_id and calendar_manager is not None:
        try:
            conn = calendar_manager._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM calendar_events WHERE event_id = ? LIMIT 1",
                    (event_id,),
                ).fetchone()
                if row is not None:
                    evt_dict = dict(row)
            finally:
                conn.close()
        except Exception:
            pass

    subject = str(evt_dict.get("subject") or "Calendar Event")
    location = str(evt_dict.get("location") or "")
    body_text = str(evt_dict.get("body_text") or "")
    organizer = str(evt_dict.get("organizer") or "")
    start_ts_utc = _json_safe_scalar(evt_dict.get("start_ts_utc"))
    end_ts_utc = _json_safe_scalar(evt_dict.get("end_ts_utc"))

    # Mirror CalendarManager.search_events() tokenization
    raw_tokens = [t.strip().lower() for t in re.split(r"\W+", query_text) if len(t.strip()) >= 2]
    search_tokens = [t for t in raw_tokens if t not in ENGLISH_STOPWORDS]
    if not search_tokens and raw_tokens:
        search_tokens = raw_tokens[:3]
    effective_tokens = search_tokens[:5]

    subj_low = subject.lower()
    body_low = body_text.lower()
    loc_low = location.lower()
    org_low = organizer.lower()

    matched_tokens: List[str] = []
    unmatched_tokens: List[str] = []
    matched_fields: Dict[str, List[str]] = {
        "subject": [],
        "location": [],
        "body_text": [],
        "organizer": [],
    }

    for t in effective_tokens:
        hit_any = False
        if t in subj_low:
            matched_fields["subject"].append(t)
            hit_any = True
        if t in loc_low:
            matched_fields["location"].append(t)
            hit_any = True
        if t in body_low:
            matched_fields["body_text"].append(t)
            hit_any = True
        if t in org_low:
            matched_fields["organizer"].append(t)
            hit_any = True
        if hit_any:
            matched_tokens.append(t)
        else:
            unmatched_tokens.append(t)

    ratio = (len(matched_tokens) / float(len(effective_tokens))) if effective_tokens else 0.0
    conf_pct = int(round(ratio * 100))
    tier = "HIGH" if ratio >= 0.65 else ("MEDIUM" if ratio >= 0.40 else "LOW")

    combined_raw = f"Subject: {subject}"
    if location:
        combined_raw += f"\nLocation: {location}"
    if organizer:
        combined_raw += f"\nOrganizer: {organizer}"
    if body_text:
        combined_raw += f"\nNotes:\n{body_text}"
    highlighted = _highlight_substrings_outside_marks(combined_raw, matched_tokens)

    summary_bullets: List[str] = []
    if source_context.get("origin_subject"):
        summary_bullets.append(
            f"Surfaced as an ambient related event while viewing '{source_context['origin_subject']}' via extracted keywords \"{query_text}\"."
        )
    if matched_tokens:
        where_matched = [
            f"{field} ({', '.join(toks)})"
            for field, toks in matched_fields.items()
            if toks
        ]
        summary_bullets.append(
            f"SQLite calendar substring search matched {len(matched_tokens)}/{len(effective_tokens)} tokens in: {'; '.join(where_matched)}."
        )
    else:
        summary_bullets.append(
            "Matched via calendar event lookup on query tokens."
        )

    return {
        "success": True,
        "item_kind": "calendar_event",
        "source_context": _json_safe_value(source_context),
        "document": {
            "event_id": event_id,
            "source_type": "CALENDAR",
            "title": subject,
            "subject": subject,
            "location": location or None,
            "organizer": organizer or None,
            "start_ts_utc": start_ts_utc,
            "end_ts_utc": end_ts_utc,
            "status": str(_json_safe_scalar(evt_dict.get("status") or "CONFIRMED")),
        },
        "query_analysis": {
            "query_text": query_text,
            "raw_tokens": raw_tokens,
            "content_tokens": effective_tokens,
            "stopwords_removed": [t for t in raw_tokens if t in ENGLISH_STOPWORDS],
            "identifier_tokens": [],
        },
        "channels_matched": ["CALENDAR_SQL_LIKE"],
        "scoring": {
            "lexical_rank": None,
            "vector_rank": None,
            "rrf_score": None,
            "normalized_rrf": round(ratio, 4),
            "cosine_similarity": None,
            "semantic_strength": None,
            "confidence_score": round(ratio, 4),
            "confidence_pct": conf_pct,
            "relevance_tier": tier,
            "exact_identifier_match": False,
            "calibration_rule": "Calendar Token Overlap Ratio",
            "calibration_formula": f"{len(matched_tokens)} / {max(1, len(effective_tokens))} effective tokens matched ({conf_pct}%)",
            "tier_thresholds": "Calendar SQLite LIKE lookup across Subject, Location, Description",
        },
        "lexical_evidence": {
            "strategy": "SQLite LOWER() LIKE substring search (top 5 non-stopword tokens)",
            "bm25_score": None,
            "matched_tokens": matched_tokens,
            "unmatched_tokens": unmatched_tokens,
            "matched_fields": matched_fields,
            "matched_identifiers": [],
            "trigram_matched": False,
        },
        "passage": {
            "raw_text": combined_raw,
            "highlighted_html": highlighted,
            "char_length": len(combined_raw),
        },
        "summary_bullets": summary_bullets,
    }


def explain_match_tier1(
    fts_db_path: Optional[Path] = None,
    catalog: Optional[Any] = None,
    cas: Optional[Any] = None,
    calendar_manager: Optional[Any] = None,
    rrf_k: float = 60.0,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synchronously compute Tier 1 deterministic match & scoring explanation (<10ms)."""
    req = dict(payload or {})
    item_kind = str(req.get("item_kind") or "search_hit").lower()
    if item_kind == "calendar_event":
        return _explain_calendar_event_tier1(
            calendar_manager=calendar_manager,
            payload=req,
        )
    return _explain_search_hit_tier1(
        fts_db_path=fts_db_path,
        catalog=catalog,
        cas=cas,
        rrf_k=rrf_k,
        payload=req,
    )


class MatchExplainer:
    """Lazy Tier 2 AI Synthesis over AuraRouter with bounded caching & graceful degradation."""

    def __init__(
        self,
        settings_override: Optional[Dict[str, Any]] = None,
        fabric: Optional[Any] = None,
        max_cache_size: int = 256,
    ):
        self._settings_override = settings_override
        self._injected_fabric = fabric
        self._max_cache_size = max(16, int(max_cache_size))
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._fabric_lock = threading.Lock()
        self._cached_fabric: Any = None
        self._cached_fabric_sig: Optional[Tuple[str, float]] = None

    def _get_settings(self) -> Dict[str, Any]:
        defaults = {
            "enabled": True,
            "mode": "auto",
            "endpoint_url": os.environ.get("AURAROUTER_ENDPOINT_URL", ""),
            "config_path": os.environ.get("AURAROUTER_CONFIG_PATH", ""),
            "role": "assist",
            "local_only": True,
            "timeout_sec": 30.0,
        }
        if self._settings_override is not None:
            merged = dict(defaults)
            merged.update(self._settings_override)
            return merged
        try:
            from old_rag.storage.settings import load_settings
            loaded = load_settings().get("aurarouter", {})
            if isinstance(loaded, dict):
                merged = dict(defaults)
                for k, v in loaded.items():
                    if v != "" or defaults.get(k) == "":
                        merged[k] = v
                return merged
        except Exception:
            pass
        return defaults

    @staticmethod
    def _build_cache_key(payload: Dict[str, Any], settings: Dict[str, Any]) -> str:
        tier1 = payload.get("tier1") or {}
        doc = tier1.get("document") or {}
        scoring = tier1.get("scoring") or {}
        src_ctx = payload.get("source_context") or tier1.get("source_context") or {}
        passage_raw = (
            payload.get("passage_text")
            or (tier1.get("passage") or {}).get("raw_text")
            or (payload.get("score_snapshot") or {}).get("content")
            or ""
        )
        key_obj = {
            "item_kind": payload.get("item_kind") or tier1.get("item_kind") or "search_hit",
            "query_text": payload.get("query_text") or (tier1.get("query_analysis") or {}).get("query_text") or "",
            "doc_id": payload.get("doc_id") or doc.get("doc_id") or "",
            "chunk_id": payload.get("chunk_id") or doc.get("chunk_id") or "",
            "event_id": payload.get("event_id") or doc.get("event_id") or "",
            "origin_doc_id": src_ctx.get("origin_doc_id") or "",
            "surface": src_ctx.get("surface") or "",
            "passage_head": str(passage_raw)[:500],
            "conf": scoring.get("confidence_score"),
            "l_rank": scoring.get("lexical_rank"),
            "v_rank": scoring.get("vector_rank"),
            "exact_id": scoring.get("exact_identifier_match"),
            "mode": settings.get("mode"),
            "endpoint_url": settings.get("endpoint_url"),
            "config_path": settings.get("config_path"),
            "role": settings.get("role"),
            "local_only": bool(settings.get("local_only", True)),
        }
        return hashlib.sha256(safe_json_dumps(key_obj, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _build_tier2_prompt(payload: Dict[str, Any]) -> str:
        tier1 = payload.get("tier1") or {}
        snapshot = payload.get("score_snapshot") or {}
        doc = tier1.get("document") or snapshot
        scoring = tier1.get("scoring") or _compute_scoring_breakdown(snapshot, rrf_k=60.0)
        lexical = tier1.get("lexical_evidence") or {}
        q_analysis = tier1.get("query_analysis") or {}
        src_ctx = payload.get("source_context") or tier1.get("source_context") or {}

        query_text = (
            payload.get("query_text")
            or q_analysis.get("query_text")
            or ""
        )
        passage = (
            payload.get("passage_text")
            or (tier1.get("passage") or {}).get("raw_text")
            or snapshot.get("content")
            or snapshot.get("body_text")
            or ""
        )
        # Strip <mark> wrappers cleanly if present in fallback text
        passage = re.sub(r"</?mark>", "", str(passage)).strip()
        if len(passage) > 1500:
            passage = passage[:1500] + "\n...[truncated]"

        context_line = f'User query: "{query_text}"'
        if src_ctx.get("origin_subject"):
            context_line = (
                f'Ambient related lookup while reading "{src_ctx.get("origin_subject")}". '
                f'Extracted search terms: "{query_text}"'
            )

        channels = ", ".join(tier1.get("channels_matched") or []) or "Unknown"
        matched_toks = ", ".join(lexical.get("matched_tokens") or []) or "None"
        unmatched_toks = ", ".join(lexical.get("unmatched_tokens") or []) or "None"

        cos_val = scoring.get("cosine_similarity")
        cos_str = f"{cos_val:.3f}" if cos_val is not None else "N/A"
        l_rank_str = f"#{scoring['lexical_rank']}" if scoring.get("lexical_rank") is not None else "None"
        v_rank_str = f"#{scoring['vector_rank']}" if scoring.get("vector_rank") is not None else "None"
        conf_pct = scoring.get("confidence_pct", 0)
        tier_str = scoring.get("relevance_tier", "LOW")

        title = doc.get("title") or doc.get("subject") or doc.get("doc_id") or "Retrieved Item"
        src_type = doc.get("source_type") or "EMAIL"
        sender = doc.get("sender") or ""
        meta_parts = [f"Type: {src_type}", f"Title: {title}"]
        if sender:
            meta_parts.append(f"From: {sender}")
        if doc.get("folder"):
            meta_parts.append(f"Folder: {doc['folder']}")
        if doc.get("location"):
            meta_parts.append(f"Location: {doc['location']}")

        return (
            "You are explaining search relevance inside a private personal archive.\n"
            "Write 2 to 3 clear, concise, factual sentences explaining WHY this item matched "
            "the query and WHY it received its confidence tier. Ground your explanation strictly "
            "in the retrieval metrics and excerpt below. Do not invent facts or use markdown bullet lists.\n\n"
            f"RETRIEVAL CONTEXT:\n{context_line}\n\n"
            "SCORING SIGNALS:\n"
            f"- Channels: {channels}\n"
            f"- Confidence: {conf_pct}% ({tier_str}) via {scoring.get('calibration_rule', 'RRF')}\n"
            f"- Lexical Rank: {l_rank_str} | Vector Rank: {v_rank_str} | Cosine Similarity: {cos_str}\n"
            f"- Exact ID Pin: {bool(scoring.get('exact_identifier_match'))}\n"
            f"- Matched Stemmed Tokens: {matched_toks} | Unmatched Tokens: {unmatched_toks}\n\n"
            f"ITEM METADATA:\n" + " | ".join(meta_parts) + "\n\n"
            f"EXCERPT:\n{passage or '(No excerpt text available)'}"
        )

    @staticmethod
    def _clean_response_text(text: str) -> str:
        cleaned = _META_COMMENT_RE.sub("", text or "").strip()
        return cleaned

    @staticmethod
    def _check_provider_reachable(model_cfg: Dict[str, Any], timeout_sec: float = 1.5) -> Tuple[bool, str]:
        """Lightweight endpoint health check (<1.5s) to fail fast when local servers are down."""
        provider = str(model_cfg.get("provider") or "").lower()
        t = min(1.5, max(0.3, float(timeout_sec)))

        def _http_get_status(url: str) -> int:
            try:
                import httpx
                resp = httpx.get(url, timeout=t)
                return int(resp.status_code)
            except ImportError:
                import urllib.request
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=t) as resp:
                    return int(resp.status)

        try:
            if provider == "ollama":
                ollama_port = os.environ.get("OLLAMA_PORT", "11434")
                default_ep = os.environ.get("OLLAMA_ENDPOINT_DEFAULT", f"http://127.0.0.1:{ollama_port}/api/generate")
                endpoint = str(model_cfg.get("endpoint") or default_ep)
                base = endpoint.split("/api/")[0] if "/api/" in endpoint else endpoint.rstrip("/")
                status = _http_get_status(f"{base}/api/tags")
                if status == 200:
                    return True, ""
                return False, f"Ollama returned HTTP {status} at {base}/api/tags"

            if provider == "llamacpp-server":
                llama_port = os.environ.get("LLAMACPP_PORT", "8080")
                default_ep = os.environ.get("LLAMACPP_ENDPOINT_DEFAULT", f"http://127.0.0.1:{llama_port}")
                endpoint = str(model_cfg.get("endpoint") or default_ep).rstrip("/")
                status = _http_get_status(f"{endpoint}/health")
                if status == 200:
                    return True, ""
                return False, f"llama.cpp server returned HTTP {status} at {endpoint}/health"

            if provider == "llamacpp":
                path = str(model_cfg.get("model_path") or "")
                if path and Path(path).is_file():
                    return True, ""
                return False, f"GGUF model file not found: {path or '(unset)'}"

            if provider == "openapi":
                openapi_port = os.environ.get("OPENAPI_PORT", "8000")
                default_ep = os.environ.get("OPENAPI_ENDPOINT_DEFAULT", f"http://127.0.0.1:{openapi_port}/v1")
                endpoint = str(model_cfg.get("endpoint") or default_ep).rstrip("/")
                status = _http_get_status(f"{endpoint}/models")
                if status < 500:
                    return True, ""
                return False, f"OpenAPI endpoint returned HTTP {status}"

            # Unknown / mock provider: let ComputeFabric execute directly
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _get_or_create_fabric(self, config_path: str = ""):
        """Return (ConfigLoader, ComputeFabric), caching by resolved config path and mtime."""
        if self._injected_fabric is not None:
            return self._injected_fabric.config, self._injected_fabric

        from aurarouter.config import ConfigLoader
        from aurarouter.fabric import ComputeFabric

        explicit = config_path.strip() if config_path else None
        # Peek at mtime cleanly
        with self._fabric_lock:
            loader = ConfigLoader(config_path=explicit)
            resolved = str(loader.config_path.resolve()) if loader.config_path else ""
            mtime = loader.config_path.stat().st_mtime if (loader.config_path and loader.config_path.exists()) else 0.0
            sig = (resolved, mtime)
            if self._cached_fabric is not None and self._cached_fabric_sig == sig:
                return self._cached_fabric.config, self._cached_fabric
            fabric = ComputeFabric(loader)
            self._cached_fabric = fabric
            self._cached_fabric_sig = sig
            return loader, fabric

    def _try_mcp_http(
        self,
        endpoint_url: str,
        prompt: str,
        local_only: bool,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        """Call a running AuraRouter HTTP MCP endpoint via JSON-RPC 2.0 tools/call."""
        base = endpoint_url.strip().rstrip("/")
        if not base.endswith("/mcp/message") and not base.endswith("/message"):
            rpc_url = f"{base}/mcp/message"
        else:
            rpc_url = base

        tool_sequence = (
            [("local_inference", {"prompt": prompt}), ("route_task", {"task": prompt, "format": "text"})]
            if local_only
            else [("route_task", {"task": prompt, "format": "text"}), ("local_inference", {"prompt": prompt})]
        )

        def _post_json(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
            try:
                import httpx
                with httpx.Client(timeout=timeout_sec) as client:
                    resp = client.post(url, json=body)
                    resp.raise_for_status()
                    return resp.json()
            except ImportError:
                import json
                import urllib.request
                data_bytes = safe_json_dumps(body).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data_bytes,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    return json.loads(resp.read().decode("utf-8"))

        last_err = ""
        for idx, (tool_name, args) in enumerate(tool_sequence):
            rpc_req = {
                "jsonrpc": "2.0",
                "id": idx + 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
            try:
                data = _post_json(rpc_url, rpc_req)
            except Exception as exc:
                msg = str(exc)
                code = "TIMEOUT" if "timed out" in msg.lower() or "timeout" in msg.lower() else "CONNECTION_ERROR"
                return {
                    "success": False,
                    "error_code": code,
                    "error": f"AuraRouter MCP HTTP unreachable at {rpc_url}: {msg}",
                    "transport": "mcp_http",
                }

            if isinstance(data, dict) and data.get("error"):
                last_err = str(data["error"])
                continue

            result_obj = data.get("result") if isinstance(data, dict) else None
            text_out = ""
            if isinstance(result_obj, str):
                text_out = result_obj
            elif isinstance(result_obj, dict):
                content = result_obj.get("content")
                if isinstance(content, list):
                    text_parts = [
                        str(c.get("text", ""))
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    text_out = "\n".join(text_parts)
                elif "text" in result_obj:
                    text_out = str(result_obj["text"])

            clean_text = self._clean_response_text(text_out)
            if not clean_text or clean_text.startswith(("ERROR:", "Error:", "BUDGET_EXCEEDED:")):
                return {
                    "success": False,
                    "error_code": "INFERENCE_ERROR",
                    "error": clean_text or "AuraRouter MCP server returned an empty response.",
                    "transport": "mcp_http",
                }

            return {
                "success": True,
                "explanation": clean_text,
                "model_id": f"aurarouter:{tool_name}",
                "role": "local" if tool_name == "local_inference" else "routed",
                "provider": "aurarouter-mcp",
                "transport": "mcp_http",
            }

        return {
            "success": False,
            "error_code": "CONNECTION_ERROR",
            "error": f"AuraRouter MCP call failed at {rpc_url}: {last_err or 'No compatible tool available'}",
            "transport": "mcp_http",
        }

    def _try_embedded(
        self,
        prompt: str,
        settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run grounded Tier 2 synthesis through embedded AuraRouter ConfigLoader + ComputeFabric."""
        try:
            loader, fabric = self._get_or_create_fabric(str(settings.get("config_path") or ""))
        except ImportError:
            return {
                "success": False,
                "error_code": "NOT_INSTALLED",
                "error": "AuraRouter package is not installed in the active Python environment.",
                "transport": "embedded",
            }
        except FileNotFoundError as exc:
            return {
                "success": False,
                "error_code": "NOT_CONFIGURED",
                "error": f"AuraRouter configuration (auraconfig.yaml) not found: {exc}",
                "transport": "embedded",
            }
        except Exception as exc:
            return {
                "success": False,
                "error_code": "CONFIG_ERROR",
                "error": f"Failed to initialize AuraRouter: {exc}",
                "transport": "embedded",
            }

        preferred_role = str(settings.get("role") or "assist").strip()
        local_only = bool(settings.get("local_only", True))
        timeout_sec = max(1.0, float(settings.get("timeout_sec") or 30.0))

        candidate_roles: List[str] = []
        for r in (preferred_role, *_TIER2_DEFAULT_ROLES):
            if r and r not in candidate_roles:
                candidate_roles.append(r)

        selected_role: Optional[str] = None
        selected_chain: List[str] = []
        had_any_configured = False

        for r in candidate_roles:
            raw_chain = loader.get_role_chain(r)
            if raw_chain:
                had_any_configured = True
            chain = fabric.get_local_chain(r) if (local_only and hasattr(fabric, "get_local_chain")) else raw_chain
            if chain:
                selected_role = r
                selected_chain = list(chain)
                break

        if not selected_role or not selected_chain:
            msg = (
                "No local AuraRouter models are configured in any candidate role "
                f"({', '.join(candidate_roles)}) with local_only=True."
                if had_any_configured and local_only
                else f"No AuraRouter models configured for roles ({', '.join(candidate_roles)})."
            )
            return {
                "success": False,
                "error_code": "NO_LOCAL_MODELS" if (had_any_configured and local_only) else "NOT_CONFIGURED",
                "error": msg,
                "transport": "embedded",
            }

        # Lightweight reachability check before calling ComputeFabric.execute()
        # (unless an injected mock/test fabric is supplying in-memory providers)
        if self._injected_fabric is None:
            reachable_chain: List[str] = []
            probe_failures: List[str] = []
            for mid in selected_chain:
                mcfg = loader.get_model_config(mid)
                ok, reason = self._check_provider_reachable(mcfg, timeout_sec=min(1.5, timeout_sec))
                if ok:
                    reachable_chain.append(mid)
                else:
                    probe_failures.append(f"{mid}: {reason}")
            if not reachable_chain:
                return {
                    "success": False,
                    "error_code": "CONNECTION_ERROR",
                    "error": (
                        f"AuraRouter local provider unreachable for role '{selected_role}' "
                        f"({'; '.join(probe_failures)})."
                    ),
                    "transport": "embedded",
                }
        else:
            reachable_chain = selected_chain

        # Execute with hard timeout guard (never wait on stuck worker pool on timeout)
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(
            fabric.execute,
            selected_role,
            prompt,
            chain_override=reachable_chain,
        )
        try:
            gen_res = fut.result(timeout=timeout_sec)
            pool.shutdown(wait=False)
        except FuturesTimeoutError:
            pool.shutdown(wait=False, cancel_futures=True)
            return {
                "success": False,
                "error_code": "TIMEOUT",
                "error": f"AuraRouter inference timed out after {timeout_sec:.1f}s on role '{selected_role}'.",
                "transport": "embedded",
            }
        except Exception as exc:
            pool.shutdown(wait=False)
            return {
                "success": False,
                "error_code": "INFERENCE_ERROR",
                "error": f"AuraRouter execution failed: {exc}",
                "transport": "embedded",
            }

        if gen_res is None:
            return {
                "success": False,
                "error_code": "CONNECTION_ERROR",
                "error": f"All AuraRouter providers failed for role '{selected_role}'.",
                "transport": "embedded",
            }

        raw_text = str(getattr(gen_res, "text", gen_res) or "")
        clean_text = self._clean_response_text(raw_text)
        if not clean_text or clean_text.startswith(("ERROR:", "Error:", "BUDGET_EXCEEDED:")):
            return {
                "success": False,
                "error_code": "INFERENCE_ERROR",
                "error": clean_text or "AuraRouter returned an empty explanation.",
                "transport": "embedded",
            }

        model_used = getattr(gen_res, "model_id", "") or (reachable_chain[0] if reachable_chain else "")
        provider_used = getattr(gen_res, "provider", "") or str(
            loader.get_model_config(model_used).get("provider", "")
        )
        return {
            "success": True,
            "explanation": clean_text,
            "model_id": model_used,
            "role": selected_role,
            "provider": provider_used or "local",
            "transport": "embedded",
            "input_tokens": int(getattr(gen_res, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(gen_res, "output_tokens", 0) or 0),
        }

    def explain_tier2(
        self,
        payload: Dict[str, Any],
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Lazily synthesize a grounded Tier 2 explanation via AuraRouter with graceful degradation."""
        req = dict(payload or {})
        force = bool(force_refresh or req.get("force_refresh", False))
        settings = self._get_settings()

        if not bool(settings.get("enabled", True)):
            return {
                "success": False,
                "error_code": "DISABLED",
                "error": "AuraRouter AI explanations are disabled in settings.",
                "cached": False,
            }

        cache_key = self._build_cache_key(req, settings)
        if not force:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    hit = dict(cached)
                    hit["cached"] = True
                    hit["latency_ms"] = 0
                    return hit

        prompt = self._build_tier2_prompt(req)
        mode = str(settings.get("mode") or "auto").lower()
        endpoint_url = str(settings.get("endpoint_url") or "").strip()
        timeout_sec = max(1.0, float(settings.get("timeout_sec") or 30.0))
        local_only = bool(settings.get("local_only", True))

        t0 = time.monotonic()
        if mode == "mcp_http" or (mode == "auto" and endpoint_url):
            if not endpoint_url:
                return {
                    "success": False,
                    "error_code": "NOT_CONFIGURED",
                    "error": "AuraRouter mode is 'mcp_http' but no endpoint_url is configured.",
                    "cached": False,
                }
            res = self._try_mcp_http(
                endpoint_url=endpoint_url,
                prompt=prompt,
                local_only=local_only,
                timeout_sec=timeout_sec,
            )
            # In auto mode, if HTTP MCP server isn't listening, fallback cleanly to embedded AuraRouter?
            # Only if endpoint_url wasn't explicitly demanding mcp_http — actually if user set endpoint_url,
            # reporting the unreachable endpoint_url cleanly is clearer than silently switching paths.
        else:
            res = self._try_embedded(prompt=prompt, settings=settings)

        latency_ms = int(round((time.monotonic() - t0) * 1000.0))
        res["latency_ms"] = latency_ms
        res["cached"] = False
        res["local_only"] = local_only

        if res.get("success"):
            with self._cache_lock:
                self._cache[cache_key] = dict(res)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._max_cache_size:
                    self._cache.popitem(last=False)

        return res
