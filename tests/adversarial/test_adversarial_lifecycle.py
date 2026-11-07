"""test_adversarial_lifecycle.py - Adversarial testing for Mutex, Outbox Crash Recovery & Matryoshka."""

import numpy as np
import pytest

from old_rag.contracts import DimensionMismatchError, OutboxOp, OutboxStatus
from old_rag.engine.indexer import matryoshka_reduce
from old_rag.pipeline.lifecycle import SingleInstanceMutex


def test_adversarial_matryoshka_dimension_mismatch():
    """Attempting to slice a vector shorter than matryoshka_dim must raise DimensionMismatchError."""
    short_vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    with pytest.raises(DimensionMismatchError):
        matryoshka_reduce(short_vec, target_dim=768)


def test_adversarial_mutex_session_collision():
    """Simulating two processes attempting to acquire the same session mutex."""
    test_mutex_name = "Local\\OldRAG_Adversarial_Collision_Test"
    m1 = SingleInstanceMutex(test_mutex_name)
    m2 = SingleInstanceMutex(test_mutex_name)

    assert m1.acquire() is True
    # Second acquisition on Windows returns False
    import sys
    if sys.platform == "win32":
        assert m2.acquire() is False
    m1.release()


def test_adversarial_outbox_unclean_crash_replay(catalog_repo):
    """Simulate a crash where multiple outbox operations were left PENDING."""
    catalog_repo.enqueue_outbox(OutboxOp.INGEST, "crash_doc_1", ["c1", "c2"])
    catalog_repo.enqueue_outbox(OutboxOp.DELETE, "crash_doc_2", ["c3"])

    pending = catalog_repo.get_pending_outbox()
    assert len(pending) == 2

    # Simulate crash recovery replaying them
    for rec in pending:
        catalog_repo.mark_outbox_committed(rec.outbox_id)

    assert len(catalog_repo.get_pending_outbox()) == 0
