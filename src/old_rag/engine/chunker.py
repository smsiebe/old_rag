"""chunker.py - Parent-Safe Localized Reply Stripper & Lineage Chunker."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from tokenizers import Tokenizer

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
        tokenizer: Optional[Tokenizer] = None,
        tokenizer_path: Optional[Path] = None,
    ):
        self.catalog = catalog
        self.max_chunk_tokens = max_chunk_tokens
        self.chunk_overlap = chunk_overlap
        self.tokenizer = tokenizer

        if self.tokenizer is None:
            if tokenizer_path and Path(tokenizer_path).exists():
                try:
                    self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
                except Exception:
                    self.tokenizer = None
            else:
                try:
                    from old_rag.storage.models import ModelResolver
                    _, resolved_tok = ModelResolver().resolve_embedder_model()
                    if Path(resolved_tok).exists():
                        self.tokenizer = Tokenizer.from_file(str(resolved_tok))
                except Exception:
                    self.tokenizer = None

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
        if doc.structured_context and "<!-- Scryer Structured Context -->" not in raw_text:
            from old_rag.engine.extractor import format_structured_context_text

            extra_ctx = format_structured_context_text(
                doc.structured_context, doc.document_metadata or None, doc.unstructured_annotations or None
            )
            if extra_ctx:
                raw_text = f"{raw_text}\n\n{extra_ctx}".strip()
        if not raw_text.strip():
            return []

        chunks: List[DocumentChunk] = []

        # 1. Use real WordPiece tokenizer when available
        if self.tokenizer is not None:
            enc = self.tokenizer.encode(raw_text)
            total_tokens = len(enc.ids)

            if total_tokens <= self.max_chunk_tokens:
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
                        content=raw_text,
                        token_count=total_tokens,
                    )
                )
                return chunks

            step = max(1, self.max_chunk_tokens - self.chunk_overlap)
            idx = 0
            chunk_idx = 0

            while idx < total_tokens:
                end_idx = min(idx + self.max_chunk_tokens, total_tokens)
                offsets = [o for o in enc.offsets[idx:end_idx] if o != (0, 0)]
                if offsets:
                    char_start = offsets[0][0]
                    char_end = offsets[-1][1]
                    window_content = raw_text[char_start:char_end].strip()
                else:
                    window_content = raw_text

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
                        content=window_content,
                        token_count=end_idx - idx,
                    )
                )
                chunk_idx += 1
                idx += step

            return chunks

        # 2. Fallback whitespace word budgeting
        words = self._estimate_tokens(raw_text)
        total_words = len(words)

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

