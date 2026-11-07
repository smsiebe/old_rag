"""chunker.py - Parent-Safe Localized Reply Stripper & Lineage Chunker."""

from __future__ import annotations

import re
from typing import List, Optional

from old_rag.contracts import DocumentChunk, NormalizedDocumentJSON
from old_rag.protocols import CatalogProtocol, ChunkerProtocol

# Common international Outlook quoted reply headers
REPLY_PATTERNS = [
    re.compile(r"^[-_\s]*Original Message[-_\s]*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(From|De|Von):\s*.+\n(Sent|Envoyé|Gesendet|Enviado):\s*.+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^On\s+.+wrote:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Le\s+.+a écrit\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Am\s+.+schrieb\s*.+:", re.IGNORECASE | re.MULTILINE),
]


class ParentAwareChunker(ChunkerProtocol):
    """Chunks documents into token-budgeted segments with parent-safe reply stripping."""

    def __init__(
        self,
        catalog: Optional[CatalogProtocol] = None,
        max_chunk_tokens: int = 496,
        chunk_overlap: int = 64,
    ):
        self.catalog = catalog
        self.max_chunk_tokens = max_chunk_tokens
        self.chunk_overlap = chunk_overlap

    def strip_reply_chain(self, text: str, doc: NormalizedDocumentJSON) -> str:
        """Strip quoted reply blocks ONLY if parent document is confirmed indexed."""
        # Find earliest match among patterns
        earliest_idx = -1
        for pat in REPLY_PATTERNS:
            m = pat.search(text)
            if m:
                if earliest_idx == -1 or m.start() < earliest_idx:
                    earliest_idx = m.start()

        if earliest_idx == -1:
            return text

        # Check if parent is present in catalog
        has_parent = False
        if self.catalog:
            parent_id = doc.in_reply_to or (doc.references[0] if doc.references else None)
            if parent_id and self.catalog.get_document(parent_id):
                has_parent = True

        # If parent is known, safe to strip. If unknown, retain to prevent context loss!
        if has_parent:
            return text[:earliest_idx].strip()
        return text

    def _estimate_tokens(self, text: str) -> List[str]:
        """Simple whitespace and punctuation tokenizer for word budgeting."""
        return text.split()

    def chunk(self, doc: NormalizedDocumentJSON) -> List[DocumentChunk]:
        raw_text = self.strip_reply_chain(doc.clean_body_text, doc)
        if not raw_text.strip():
            return []

        words = self._estimate_tokens(raw_text)
        total_words = len(words)
        chunks: List[DocumentChunk] = []

        if total_words <= self.max_chunk_tokens:
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{doc.doc_id}_0",
                    doc_id=doc.doc_id,
                    thread_id=doc.conversation_id,
                    chunk_index=0,
                    source_type=doc.source_type,
                    sender=doc.sender,
                    folder=doc.folder,
                    received_ts_utc=doc.received_ts_utc,
                    content=" ".join(words),
                    token_count=total_words,
                )
            )
            return chunks

        step = max(1, self.max_chunk_tokens - self.chunk_overlap)
        idx = 0
        chunk_idx = 0

        while idx < total_words:
            window_words = words[idx : idx + self.max_chunk_tokens]
            content = " ".join(window_words)
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{doc.doc_id}_{chunk_idx}",
                    doc_id=doc.doc_id,
                    thread_id=doc.conversation_id,
                    chunk_index=chunk_idx,
                    source_type=doc.source_type,
                    sender=doc.sender,
                    folder=doc.folder,
                    received_ts_utc=doc.received_ts_utc,
                    content=content,
                    token_count=len(window_words),
                )
            )
            chunk_idx += 1
            idx += step

        return chunks
