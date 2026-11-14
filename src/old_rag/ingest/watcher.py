"""watcher.py - Debounced Filesystem Watcher with Office Atomic Save Coalescing."""

from __future__ import annotations

import email
from email import policy
import hashlib
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from old_rag.contracts import RawAttachment, RawIngestPayload, SourceType

# Win32 File Attributes for Cloud/OneDrive Placeholders
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


def is_cloud_placeholder(file_path: Path) -> bool:
    """Check if file is a cloud-only placeholder without reading its contents."""
    if sys.platform != "win32":
        return False
    try:
        import win32file
        attrs = win32file.GetFileAttributesW(str(file_path))
        if attrs != -1:
            if attrs & FILE_ATTRIBUTE_OFFLINE or attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
                return True
    except Exception:
        pass
    return False


def is_temporary_office_file(filename: str) -> bool:
    """Return True if filename matches Office lock/temp patterns."""
    name = Path(filename).name
    if name.startswith("~$") or name.startswith("~*.tmp") or name.endswith(".tmp"):
        return True
    return False


def parse_eml_file(file_path: Path) -> RawIngestPayload:
    """Parse standard RFC822 .eml file using Python standard library email module."""
    with open(file_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    doc_id = hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()
    subject = msg.get("subject", "")
    sender = msg.get("from", "")
    message_id = msg.get("message-id", "")
    in_reply_to = msg.get("in-reply-to", "")

    body_text = ""
    body_html = ""
    attachments = []

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if disposition == "attachment" or part.get_filename():
            data = part.get_payload(decode=True) or b""
            attachments.append(
                RawAttachment(
                    filename=part.get_filename() or "unnamed_attachment",
                    content_type=content_type,
                    data=data,
                    size_bytes=len(data),
                )
            )
        elif content_type == "text/plain" and not body_text:
            body_text = part.get_content()
        elif content_type == "text/html" and not body_html:
            body_html = part.get_content()

    return RawIngestPayload(
        source_type=SourceType.FILE,
        doc_id=doc_id,
        original_path=str(file_path.resolve()),
        internet_message_id=message_id,
        sender=sender,
        subject=subject,
        folder=file_path.parent.name,
        received_ts_utc=datetime.now(timezone.utc),
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )


class CoalescingFileHandler(FileSystemEventHandler):
    """Debounces filesystem events and coalesces Word/Excel atomic save sequences."""

    def __init__(
        self,
        on_payload: Callable[[RawIngestPayload], None],
        on_deleted: Optional[Callable[[str], None]] = None,
        debounce_seconds: float = 1.0,
    ):
        super().__init__()
        self.on_payload = on_payload
        self.on_deleted = on_deleted
        self.debounce_seconds = debounce_seconds
        self._pending: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._running = True
        self._worker_thread = threading.Thread(target=self._debounce_loop, daemon=True)
        self._worker_thread.start()

    def _debounce_loop(self) -> None:
        while self._running:
            time.sleep(0.2)
            now = time.time()
            ready_paths: Set[str] = set()

            with self._lock:
                for path, timestamp in list(self._pending.items()):
                    if now - timestamp >= self.debounce_seconds:
                        ready_paths.add(path)
                        del self._pending[path]

            for path_str in ready_paths:
                p = Path(path_str)
                if not p.exists() or p.is_dir():
                    continue
                if is_temporary_office_file(p.name) or is_cloud_placeholder(p):
                    continue

                try:
                    payload = self._process_file(p)
                    if payload:
                        self.on_payload(payload)
                except Exception:
                    pass

    def _process_file(self, path: Path) -> Optional[RawIngestPayload]:
        ext = path.suffix.lower()
        if ext == ".eml":
            return parse_eml_file(path)

        # Standard file ingest
        doc_id = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        data = path.read_bytes()
        return RawIngestPayload(
            source_type=SourceType.FILE,
            doc_id=doc_id,
            original_path=str(path.resolve()),
            subject=path.name,
            folder=path.parent.name,
            received_ts_utc=datetime.now(timezone.utc),
            attachments=[
                RawAttachment(
                    filename=path.name,
                    content_type="application/octet-stream",
                    data=data,
                    size_bytes=len(data),
                )
            ],
        )

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            with self._lock:
                self._pending[event.src_path] = time.time()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            with self._lock:
                self._pending[event.src_path] = time.time()

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            with self._lock:
                dest = getattr(event, "dest_path", None)
                if dest:
                    self._pending[dest] = time.time()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            with self._lock:
                self._pending.pop(event.src_path, None)
            if self.on_deleted:
                self.on_deleted(event.src_path)

    def stop(self) -> None:
        self._running = False
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)


class DirectoryWatcher:
    """Wrapper around watchdog observer for user-configured paths."""

    def __init__(
        self,
        watch_paths: list[Path],
        on_payload: Callable[[RawIngestPayload], None],
        on_deleted: Optional[Callable[[str], None]] = None,
    ):
        self.watch_paths = watch_paths
        self.handler = CoalescingFileHandler(on_payload=on_payload, on_deleted=on_deleted)
        self.observer = Observer()

    def start(self) -> None:
        for p in self.watch_paths:
            if p.exists() and p.is_dir():
                self.observer.schedule(self.handler, str(p), recursive=True)
        self.observer.start()

    def stop(self) -> None:
        self.handler.stop()
        self.observer.stop()
        self.observer.join(timeout=2.0)


def ingest_filesystem_path(
    target_path: Path,
    coordinator: Any,
    recursive: bool = True,
) -> Dict[str, Any]:
    """Synchronously ingest a local file or directory tree through PipelineCoordinator."""
    target = Path(target_path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {target}")

    handler = CoalescingFileHandler(on_payload=coordinator.ingest_payload, debounce_seconds=60.0)
    handler.stop()

    if target.is_file():
        candidates = [target]
    else:
        candidates = sorted(target.rglob("*") if recursive else target.glob("*"))

    ingested = 0
    skipped = 0
    errors = 0
    doc_ids: List[str] = []

    extractor = getattr(coordinator, "extractor", None)
    for p in candidates:
        if not p.is_file():
            continue
        if target.is_dir():
            try:
                rel_parts = p.relative_to(target).parts
                if any(part.startswith(".") for part in rel_parts):
                    skipped += 1
                    continue
            except Exception:
                pass
        if is_temporary_office_file(p.name) or is_cloud_placeholder(p):
            skipped += 1
            continue
        if extractor is not None and hasattr(extractor, "can_extract") and not extractor.can_extract(p.name):
            skipped += 1
            continue

        try:
            payload = handler._process_file(p)
            if payload is not None:
                coordinator.ingest_payload(payload)
                ingested += 1
                doc_ids.append(payload.doc_id)
            else:
                skipped += 1
        except Exception:
            errors += 1

    return {
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "doc_ids": doc_ids,
    }

