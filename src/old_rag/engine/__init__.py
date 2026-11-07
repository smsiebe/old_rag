"""engine package for old_rag."""

from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, derive_fts_rowid
from old_rag.engine.search import HybridSearchEngine, escape_fts5_query, is_identifier_like
from old_rag.engine.vlm_worker import VLMWorker, run_vlm_worker_stdio_loop

__all__ = [
    "DocumentExtractor",
    "ParentAwareChunker",
    "IndexEngine",
    "derive_fts_rowid",
    "HybridSearchEngine",
    "escape_fts5_query",
    "is_identifier_like",
    "VLMWorker",
    "run_vlm_worker_stdio_loop",
]

