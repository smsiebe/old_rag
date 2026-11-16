"""quota.py - Per-Data-Type Hard-Drive Quota Governor and Eviction Engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Dict, List, Optional, Set

from old_rag.contracts import (
    DataTypeQuotaConfig,
    DataTypeQuotaStatus,
    EvictionCandidateSummary,
    QuotaPolicyType,
    QuotaPurgeResult,
    SourceType,
)
from old_rag.protocols import (
    CatalogProtocol,
    ContentStoreProtocol,
    CoordinatorProtocol,
    IndexerProtocol,
    QuotaGovernorProtocol,
)
from old_rag.storage.settings import get_quota_configs


class QuotaGovernor(QuotaGovernorProtocol):
    """Monitors storage consumption per data type and executes index-driven evictions."""

    def __init__(
        self,
        catalog: CatalogProtocol,
        coordinator: Optional[CoordinatorProtocol] = None,
        indexer: Optional[IndexerProtocol] = None,
        cas: Optional[ContentStoreProtocol] = None,
    ):
        self.catalog = catalog
        self.coordinator = coordinator
        self.indexer = indexer
        self.cas = cas

    def _get_config(self, source_type: SourceType) -> DataTypeQuotaConfig:
        configs = get_quota_configs()
        cfg_dict = configs.get(source_type.value)
        if cfg_dict:
            try:
                return DataTypeQuotaConfig.model_validate(cfg_dict)
            except Exception:
                pass
        return DataTypeQuotaConfig(source_type=source_type)

    def get_quota_status(self, source_type: Optional[SourceType] = None) -> List[DataTypeQuotaStatus]:
        """Compute exact byte usage and limits across all or specified data types."""
        types_to_check = [source_type] if source_type else list(SourceType)
        usage_data = self.catalog.get_usage_by_source_type() if hasattr(self.catalog, "get_usage_by_source_type") else {}

        statuses: List[DataTypeQuotaStatus] = []
        for st in types_to_check:
            cfg = self._get_config(st)
            st_usage = usage_data.get(st.value, {})
            doc_bytes = st_usage.get("used_bytes", 0)
            excl_blobs = st_usage.get("exclusive_blob_bytes", 0)
            shared_blobs = st_usage.get("shared_blob_bytes", 0)
            total_used = doc_bytes + excl_blobs + shared_blobs

            max_b = cfg.max_bytes
            ratio = (total_used / max_b) if max_b > 0 else 0.0
            over_soft = (total_used >= max_b * cfg.soft_warning_ratio) if max_b > 0 else False
            over_hard = (total_used > max_b) if max_b > 0 else False

            statuses.append(
                DataTypeQuotaStatus(
                    source_type=st,
                    used_bytes=total_used,
                    max_bytes=max_b,
                    usage_ratio=round(ratio, 4),
                    document_count=st_usage.get("document_count", 0),
                    chunk_count=st_usage.get("chunk_count", 0),
                    shared_blob_bytes=shared_blobs,
                    exclusive_blob_bytes=excl_blobs,
                    policy=cfg.policy,
                    over_soft_limit=over_soft,
                    over_hard_limit=over_hard,
                )
            )
        return statuses

    def get_status(self, source_type: Optional[SourceType] = None) -> Any:
        """Convenience method returning a single status if source_type is specified."""
        statuses = self.get_quota_status(source_type)
        if source_type and statuses:
            return statuses[0]
        return statuses

    def rank_eviction_candidates(
        self,
        source_type: SourceType,
        bytes_to_free: int,
        config: Optional[DataTypeQuotaConfig] = None,
    ) -> List[EvictionCandidateSummary]:
        """Select and rank non-pinned, non-starred documents until bytes_to_free target is met."""
        if bytes_to_free <= 0:
            return []

        cfg = config or self._get_config(source_type)
        now_utc = datetime.now(timezone.utc)
        recent_cutoff = now_utc - timedelta(days=cfg.preserve_recent_days) if cfg.preserve_recent_days > 0 else None

        conn = getattr(self.catalog, "_get_connection", lambda: None)()
        if not conn:
            return []

        with getattr(self.catalog, "_lock", sqlite3.connect(":memory:")):
            rows = conn.execute(
                """
                SELECT doc_id, subject, sender, received_ts_utc, byte_size, access_count,
                       last_accessed_utc, is_pinned, is_starred, chunk_ids_json, conversation_id
                FROM documents
                WHERE source_type = ? AND is_deleted = 0
                """,
                (source_type.value,),
            ).fetchall()

            # Find active reply-parent doc_ids (documents that have other documents referencing them)
            parent_ids: Set[str] = set()
            try:
                child_rows = conn.execute(
                    "SELECT DISTINCT reply_to_doc_id FROM documents WHERE reply_to_doc_id IS NOT NULL AND is_deleted = 0"
                ).fetchall()
                for cr in child_rows:
                    if cr[0]:
                        parent_ids.add(cr[0])
            except Exception:
                pass

            conv_rows = conn.execute(
                """
                SELECT conversation_id FROM documents
                WHERE conversation_id IS NOT NULL AND is_deleted = 0
                GROUP BY conversation_id HAVING COUNT(*) > 1
                """
            ).fetchall()
            for cr in conv_rows:
                if cr[0]:
                    parent_ids.add(cr[0])

            # Blob reference tracking: blob_hash -> set of referencing doc_ids
            blob_doc_map: Dict[str, Set[str]] = {}
            blob_size_map: Dict[str, int] = {}
            doc_blob_map: Dict[str, List[str]] = {}

            b_rows = conn.execute(
                """
                SELECT br.blob_hash, br.doc_id, b.size_bytes
                FROM blob_references br
                JOIN blobs b ON br.blob_hash = b.blob_hash
                """
            ).fetchall()
            for br in b_rows:
                bh = br["blob_hash"]
                did = br["doc_id"]
                sz = int(br["size_bytes"])
                if bh not in blob_doc_map:
                    blob_doc_map[bh] = set()
                blob_doc_map[bh].add(did)
                blob_size_map[bh] = sz
                if did not in doc_blob_map:
                    doc_blob_map[did] = []
                doc_blob_map[did].append(bh)

            # Filter candidates against preservation rules
            valid_candidates = []
            for r in rows:
                did = r["doc_id"]
                # 1. Pinned rule
                if r["is_pinned"]:
                    continue
                # 2. Starred rule
                if cfg.preserve_starred and r["is_starred"]:
                    continue
                # 3. Recent window rule
                ts_str = r["received_ts_utc"]
                rec_ts = datetime.fromisoformat(ts_str) if ts_str else None
                if recent_cutoff and rec_ts and rec_ts >= recent_cutoff:
                    continue
                # 4. Reply-parent protection (if active reply parent of other documents)
                if did in parent_ids:
                    continue

                valid_candidates.append(dict(r))

            if not valid_candidates:
                return []

            # Scoring based on policy
            scored_candidates = []
            max_size = max((c["byte_size"] for c in valid_candidates), default=1) or 1
            max_access = max((c["access_count"] for c in valid_candidates), default=1) or 1

            for c in valid_candidates:
                did = c["doc_id"]
                ts_str = c["received_ts_utc"]
                rec_ts = datetime.fromisoformat(ts_str) if ts_str else now_utc
                last_acc_str = c["last_accessed_utc"]
                last_acc = datetime.fromisoformat(last_acc_str) if last_acc_str else rec_ts

                # LRU factor: [0, 1] higher means more frequently/recently accessed
                age_days = max(0.0, (now_utc - rec_ts).total_seconds() / 86400.0)
                idle_days = max(0.0, (now_utc - last_acc).total_seconds() / 86400.0)
                i_lru = 1.0 / (1.0 + idle_days)
                i_age = min(1.0, age_days / 365.0)
                i_size = min(1.0, c["byte_size"] / max_size)

                # Centrality calculation
                # Term density & entity count
                entity_count = conn.execute(
                    "SELECT COUNT(*) FROM document_entities WHERE doc_id = ?", (did,)
                ).fetchone()[0]
                i_graph = min(1.0, entity_count / 10.0)
                i_lex = min(1.0, max(1, c["byte_size"]) / 50000.0)
                i_vec = 0.5  # Neutral prior for intra-thread cosine redundancy
                centrality = 0.35 * i_vec + 0.35 * i_lex + 0.30 * i_graph

                if cfg.policy == QuotaPolicyType.OLDEST_FIRST:
                    retention_score = rec_ts.timestamp()
                elif cfg.policy == QuotaPolicyType.LRU_ACCESS:
                    retention_score = c["access_count"] * 1000000.0 + last_acc.timestamp()
                elif cfg.policy == QuotaPolicyType.LOWEST_CENTRALITY:
                    retention_score = centrality
                else:  # COMPOSITE_SCORE
                    retention_score = 0.40 * centrality + 0.30 * i_lru + 0.20 * (1.0 - i_age) - 0.10 * i_size

                c["_retention_score"] = retention_score
                c["_rec_ts"] = rec_ts
                scored_candidates.append(c)

            # Sort ascending by retention score (lowest score evicts first)
            scored_candidates.sort(key=lambda x: x["_retention_score"])

            # Accumulate until bytes_to_free with shared blob accounting
            selected_candidates: List[EvictionCandidateSummary] = []
            selected_doc_ids: Set[str] = set()
            cumulative_freed = 0

            # First pass: collect candidates up to target
            for cand in scored_candidates:
                selected_doc_ids.add(cand["doc_id"])

                # Direct document bytes freed (Tier 1 text, Tier 3 msg, chunks)
                freed_doc_bytes = cand["byte_size"]

                # Blobs freed: only add blob bytes if all referencing docs are in selected_doc_ids
                freed_blob_bytes = 0
                for bh in doc_blob_map.get(cand["doc_id"], []):
                    ref_docs = blob_doc_map.get(bh, set())
                    if ref_docs.issubset(selected_doc_ids):
                        freed_blob_bytes += blob_size_map.get(bh, 0)

                total_freed_for_cand = freed_doc_bytes + freed_blob_bytes
                cumulative_freed += total_freed_for_cand

                reason = (
                    f"Policy {cfg.policy.value}: retention score {cand['_retention_score']:.3f} "
                    f"(size={cand['byte_size']}B, idle={idle_days:.1f}d)"
                )

                selected_candidates.append(
                    EvictionCandidateSummary(
                        doc_id=cand["doc_id"],
                        source_type=source_type,
                        subject=cand["subject"],
                        sender=cand["sender"],
                        received_ts_utc=cand["_rec_ts"],
                        byte_size=cand["byte_size"],
                        estimated_freed_bytes=total_freed_for_cand,
                        retention_score=round(cand["_retention_score"], 4),
                        reason=reason,
                    )
                )

                if cumulative_freed >= bytes_to_free:
                    break

            return selected_candidates

    def enforce_quota(
        self,
        source_type: SourceType,
        dry_run: bool = False,
        config: Optional[DataTypeQuotaConfig] = None,
    ) -> QuotaPurgeResult:
        """Enforce quota limits by purging ranked candidates across all internal stores."""
        statuses = self.get_quota_status(source_type)
        if not statuses:
            return QuotaPurgeResult(
                source_type=source_type,
                dry_run=dry_run,
                evicted_doc_ids=[],
                freed_bytes=0,
                used_bytes_before=0,
                used_bytes_after=0,
            )

        status = statuses[0]
        used_before = status.used_bytes
        cfg = config or self._get_config(source_type)
        max_bytes = cfg.max_bytes if config is not None else status.max_bytes

        if max_bytes <= 0 or used_before <= max_bytes:
            return QuotaPurgeResult(
                source_type=source_type,
                dry_run=dry_run,
                evicted_doc_ids=[],
                freed_bytes=0,
                used_bytes_before=used_before,
                used_bytes_after=used_before,
            )

        bytes_to_free = used_before - max_bytes
        candidates = self.rank_eviction_candidates(source_type, bytes_to_free, config=cfg)
        evicted_ids = [c.doc_id for c in candidates]
        freed_bytes = sum(c.estimated_freed_bytes for c in candidates)

        if not dry_run and self.coordinator:
            for did in evicted_ids:
                if hasattr(self.coordinator, "delete_document"):
                    self.coordinator.delete_document(did)

            # Compact indexes and checkpoint catalog
            if self.indexer and hasattr(self.indexer, "compact_stores"):
                self.indexer.compact_stores()
            if hasattr(self.catalog, "checkpoint_and_vacuum"):
                self.catalog.checkpoint_and_vacuum()

            post_status = self.get_quota_status(source_type)
            used_after = post_status[0].used_bytes if post_status else max(0, used_before - freed_bytes)
        else:
            used_after = max(0, used_before - freed_bytes)

        return QuotaPurgeResult(
            source_type=source_type,
            dry_run=dry_run,
            evicted_doc_ids=evicted_ids,
            freed_bytes=freed_bytes,
            used_bytes_before=used_before,
            used_bytes_after=used_after,
            candidates=candidates,
        )
