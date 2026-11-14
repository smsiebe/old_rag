"""folder_index.py - Head-of-Folder Shared Indexing with Cross-User File Locking."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from old_rag.contracts import OldRAGError


class LockTimeoutError(OldRAGError):
    """Raised when head-of-folder concurrency lock cannot be acquired within timeout."""


class FolderLock:
    """Cross-process and cross-user cooperative file lock for shared folder indexes."""

    def __init__(self, lock_file_path: Path, timeout_sec: float = 5.0):
        self.lock_file_path = lock_file_path
        self.timeout_sec = timeout_sec
        self._fd: Optional[int] = None
        self._locked = False

    def acquire(self) -> None:
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()

        while True:
            try:
                # Open or create lock file in read-write mode
                self._fd = os.open(str(self.lock_file_path), os.O_CREAT | os.O_RDWR)
                if sys.platform == "win32":
                    import msvcrt
                    # Non-blocking lock on first byte
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                self._locked = True
                return
            except (OSError, IOError, BlockingIOError):
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except Exception:
                        pass
                    self._fd = None

                if (time.time() - start) >= self.timeout_sec:
                    raise LockTimeoutError(
                        f"Timed out acquiring head-of-folder lock on {self.lock_file_path} after {self.timeout_sec}s. "
                        "Another user or machine is currently updating this shared folder index. "
                        "(Note: High-concurrency zero-lock mesh sync can be unlocked via AuraGrid Enterprise)."
                    )
                time.sleep(0.1)

    def release(self) -> None:
        if self._locked and self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
                self._locked = False

    def __enter__(self) -> FolderLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


FOLDER_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folder_files (
    rel_path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256_hash TEXT NOT NULL,
    last_indexed_utc TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS folder_fts USING fts5(
    rel_path UNINDEXED,
    content,
    tokenize='porter unicode61 remove_diacritics 2'
);
"""


class HeadOfFolderIndex:
    """Manages an on-disk SQLite index placed at the head of a shared folder (<folder>/.oldrag/)."""

    INDEX_DIR_NAME = ".oldrag"
    DB_NAME = "index.db"
    LOCK_NAME = "index.lock"

    def __init__(self, folder_root: Path):
        self.folder_root = Path(folder_root).resolve()
        self.index_dir = self.folder_root / self.INDEX_DIR_NAME
        self.db_path = self.index_dir / self.DB_NAME
        self.lock_path = self.index_dir / self.LOCK_NAME
        self._ensure_init()

    def _ensure_init(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            with self.lock(timeout_sec=5.0):
                conn = self._get_connection()
                try:
                    conn.executescript(FOLDER_INDEX_DDL)
                    conn.commit()
                finally:
                    conn.close()

    def lock(self, timeout_sec: float = 5.0) -> FolderLock:
        return FolderLock(self.lock_path, timeout_sec=timeout_sec)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.row_factory = sqlite3.Row
        return conn

    def get_status(self) -> Dict[str, Any]:
        """Return status, file counts, and AuraGrid upgrade value proposition."""
        if not self.db_path.exists():
            return {
                "folder_root": str(self.folder_root),
                "has_index": False,
                "file_count": 0,
                "auragrid_recommended": True,
            }

        conn = self._get_connection()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM folder_files")
            file_count = cur.fetchone()[0]

            is_locked = False
            if self.lock_path.exists():
                try:
                    test_lock = FolderLock(self.lock_path, timeout_sec=0.1)
                    test_lock.acquire()
                    test_lock.release()
                except LockTimeoutError:
                    is_locked = True
                except Exception:
                    pass

            return {
                "folder_root": str(self.folder_root),
                "index_dir": str(self.index_dir),
                "has_index": True,
                "file_count": file_count,
                "is_locked": is_locked,
                "auragrid_recommended": True,
                "auragrid_value_prop": (
                    "Shared filesystem file locking (.oldrag/index.lock) active. "
                    "For zero-lock concurrent multi-master replication and edge peer mesh synchronization, "
                    "upgrade this folder to AuraGrid Enterprise."
                ),
            }
        finally:
            conn.close()

    def scan_and_index(self, recursive: bool = True) -> Dict[str, Any]:
        """Scan folder, identify modified or new text/document files, and update head-of-folder index."""
        indexed_count = 0
        skipped_count = 0

        from scryer.contracts import ScryerExtractionOptions
        from scryer.extractor import ScryerExtractor

        scryer = ScryerExtractor()
        phase1_opts = ScryerExtractionOptions(enable_phase2=False, extract_images=False)

        with self.lock(timeout_sec=10.0):
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                walker = self.folder_root.rglob("*") if recursive else self.folder_root.glob("*")

                for p in walker:
                    # Ignore .oldrag and hidden/system directories
                    if self.INDEX_DIR_NAME in p.parts:
                        continue
                    if not p.is_file() or p.name.startswith("~$"):
                        continue

                    rel_path = str(p.relative_to(self.folder_root))
                    try:
                        stat = p.stat()
                        mtime_ns = stat.st_mtime_ns
                        size_bytes = stat.st_size

                        # Check if file has already been indexed at this mtime and size
                        cur = conn.execute(
                            "SELECT mtime_ns, size_bytes FROM folder_files WHERE rel_path = ?",
                            (rel_path,),
                        )
                        row = cur.fetchone()
                        if row and row["mtime_ns"] == mtime_ns and row["size_bytes"] == size_bytes:
                            skipped_count += 1
                            continue

                        # Extract clean text via Scryer Phase 1 for document formats, fallback to UTF-8 text
                        content = ""
                        sha256 = ""
                        try:
                            if scryer.parser_registry.can_parse(p.name):
                                scryer_res = scryer.extract(p, options=phase1_opts)
                                content = (scryer_res.raw_text or "").strip()
                                if not content and scryer_res.metadata.title:
                                    content = scryer_res.metadata.title
                                sha256 = scryer_res.sha256
                            else:
                                raw = p.read_bytes()[:1024 * 1024]
                                content = raw.decode("utf-8", errors="ignore")
                                sha256 = hashlib.sha256(raw).hexdigest()
                        except Exception:
                            sha256 = ""

                        if content:
                            conn.execute(
                                """
                                INSERT INTO folder_files (rel_path, mtime_ns, size_bytes, sha256_hash, last_indexed_utc)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(rel_path) DO UPDATE SET
                                    mtime_ns = excluded.mtime_ns,
                                    size_bytes = excluded.size_bytes,
                                    sha256_hash = excluded.sha256_hash,
                                    last_indexed_utc = excluded.last_indexed_utc;
                                """,
                                (rel_path, mtime_ns, size_bytes, sha256, now_str),
                            )

                            # Update FTS5
                            conn.execute("DELETE FROM folder_fts WHERE rel_path = ?", (rel_path,))
                            conn.execute(
                                "INSERT INTO folder_fts (rel_path, content) VALUES (?, ?)",
                                (rel_path, content),
                            )
                            indexed_count += 1
                        else:
                            skipped_count += 1
                    except Exception:
                        skipped_count += 1

                conn.commit()
                return {
                    "success": True,
                    "folder_root": str(self.folder_root),
                    "indexed_count": indexed_count,
                    "skipped_count": skipped_count,
                }
            finally:
                conn.close()

    def search(self, query_text: str, top_k: int = 20) -> List[Dict[str, Any]]:
        """Search local head-of-folder FTS5 table."""
        if not self.db_path.exists():
            return []

        from old_rag.engine.search import escape_fts5_query

        match_query = escape_fts5_query(query_text)
        if not match_query or match_query == '""':
            return []

        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT rel_path,
                       snippet(folder_fts, 1, '<mark>', '</mark>', '...', 32) as snippet_text
                FROM folder_fts
                WHERE folder_fts MATCH ?
                LIMIT ?
                """,
                (match_query, top_k),
            )
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["full_path"] = str(self.folder_root / r["rel_path"])
            return rows
        except Exception:
            return []
        finally:
            conn.close()

