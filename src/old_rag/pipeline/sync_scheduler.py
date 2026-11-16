"""sync_scheduler.py - Real-Time Multi-Source Sync Engine, EventBus & Watermark Scheduler."""

from __future__ import annotations

import collections
import concurrent.futures
from datetime import datetime, timedelta, timezone
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from old_rag.contracts import (
    CalendarEvent,
    ChatAccountConfig,
    ChatMessageItem,
    RawIngestPayload,
    SourceType,
    SyncCursorRecord,
    SyncPolicyConfig,
    UIEventDTO,
)
from old_rag.ingest.calendar import CalDAVConnector, event_to_ingest_payload
from old_rag.ingest.chat.base import BaseChatAdapter
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.ingest.imap import IMAPConnector
from old_rag.protocols import (
    CatalogProtocol,
    CoordinatorProtocol,
    QuotaGovernorProtocol,
)
from old_rag.storage.settings import (
    get_accounts,
    get_chat_accounts,
    get_sync_policies,
    load_settings,
)


class EventBus:
    """Thread-safe in-memory ring buffer for streaming UI events."""

    def __init__(self, maxlen: int = 500):
        self._events: collections.deque[UIEventDTO] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def publish_event(
        self,
        event_type: str,
        source_type: Optional[SourceType] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> UIEventDTO:
        with self._lock:
            self._seq += 1
            evt = UIEventDTO(
                seq=self._seq,
                event_type=event_type,
                source_type=source_type,
                payload=payload or {},
                created_at_utc=datetime.now(timezone.utc),
            )
            self._events.append(evt)
            return evt

    def get_events_since(self, since_seq: int = 0) -> List[UIEventDTO]:
        with self._lock:
            return [e for e in self._events if e.seq > since_seq]


class MAPIWorkerQueue:
    """Dedicated Single-Threaded Apartment (STA) worker for Classic Outlook MAPI COM calls."""

    def __init__(self):
        self._queue: queue.Queue[Tuple[Callable, tuple, dict, concurrent.futures.Future]] = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="MAPI-STA-Worker")
        self._thread.start()

    def _worker_loop(self) -> None:
        co_init = False
        if sys.platform == "win32":
            try:
                import pythoncom
                pythoncom.CoInitialize()
                co_init = True
            except Exception:
                pass

        try:
            while self._running:
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                fn, args, kwargs, future = item
                try:
                    res = fn(*args, **kwargs)
                    future.set_result(res)
                except Exception as ex:
                    future.set_exception(ex)
                finally:
                    self._queue.task_done()
        finally:
            if co_init:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def submit(self, fn: Callable, *args, timeout: Optional[float] = 300.0, **kwargs) -> Any:
        """Execute a function synchronously on the STA thread."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((fn, args, kwargs, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


class RealTimeSyncScheduler:
    """Coordinates two-sided watermark real-time sync across Email, Chat, Calendar, and Files."""

    def __init__(
        self,
        coordinator: CoordinatorProtocol,
        catalog: CatalogProtocol,
        quota_governor: Optional[QuotaGovernorProtocol] = None,
        chat_registry: Optional[ChatPluginRegistry] = None,
    ):
        self.coordinator = coordinator
        self.catalog = catalog
        self.quota_governor = quota_governor
        self.chat_registry = chat_registry or ChatPluginRegistry()
        self.event_bus = EventBus()
        self.mapi_queue = MAPIWorkerQueue()

        self._ingest_queue: queue.Queue[RawIngestPayload] = queue.Queue(maxsize=1000)
        self._running = False
        self._ingest_thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start background ingest and synchronization loops."""
        self._running = True
        self._ingest_thread = threading.Thread(target=self._ingest_loop, daemon=True, name="Sync-Ingest-Worker")
        self._ingest_thread.start()
        self._sync_thread = threading.Thread(target=self._periodic_sync_loop, daemon=True, name="Sync-Scheduler-Loop")
        self._sync_thread.start()

    def stop(self) -> None:
        """Stop background loops."""
        self._running = False
        self.mapi_queue.stop()
        if self._ingest_thread and self._ingest_thread.is_alive():
            self._ingest_thread.join(timeout=1.0)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=1.0)

    def _ingest_loop(self) -> None:
        """Drain bounded ingest queue through coordinator and quota governor."""
        while self._running:
            try:
                payload = self._ingest_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.coordinator.ingest_payload(payload)
                self.event_bus.publish_event(
                    event_type="INGEST_COMMITTED",
                    source_type=payload.source_type,
                    payload={"doc_id": payload.doc_id, "subject": payload.subject},
                )
                if self.quota_governor:
                    try:
                        self.quota_governor.enforce_quota(payload.source_type)
                    except Exception:
                        pass
            except Exception as e:
                self.event_bus.publish_event(
                    event_type="INGEST_ERROR",
                    source_type=payload.source_type,
                    payload={"doc_id": payload.doc_id, "error": str(e)},
                )
            finally:
                self._ingest_queue.task_done()

    def _periodic_sync_loop(self) -> None:
        """Execute periodic sync across configured sources."""
        while self._running:
            try:
                self.sync_all_sources()
            except Exception:
                pass
            time.sleep(15.0)

    def sync_all_sources(self) -> None:
        """Run a full sync cycle across all configured connectors."""
        # 1. Sync Chat Accounts
        chat_accounts = get_chat_accounts(decrypt=True)
        for acc_dict in chat_accounts:
            if not acc_dict.get("enabled", True):
                continue
            try:
                acc_cfg = ChatAccountConfig.model_validate(acc_dict)
                self.sync_chat_account(acc_cfg)
            except Exception:
                pass

        # 2. Sync Email Accounts
        email_accounts = get_accounts(decrypt=True)
        for acc in email_accounts:
            if not acc.get("enabled", True):
                continue
            acc_type = acc.get("account_type", "")
            if acc_type == "outlook_mapi":
                self.sync_outlook_mail(acc)
            elif acc_type == "imap_smtp":
                self.sync_imap_mail(acc)

        # 3. Sync Calendar
        self.sync_calendar_sources()

    def sync_chat_account(self, account: ChatAccountConfig) -> int:
        """Synchronize DMs and subscribed rooms for a chat account, handling lookback expansion."""
        adapter = self.chat_registry.create_adapter(account)
        source_key = f"chat_{account.id}"
        now = datetime.now(timezone.utc)

        # Retrieve existing watermark cursor
        cursors = {c.source_key: c for c in (self.catalog.get_sync_cursors() if hasattr(self.catalog, "get_sync_cursors") else [])}
        cur_record = cursors.get(source_key)

        target_lookback = account.lookback_days or 30
        target_low_utc = now - timedelta(days=target_lookback)

        low_watermark = cur_record.low_watermark_utc if cur_record else None
        high_cursor = cur_record.high_watermark_cursor if cur_record else None

        # Check for lookback expansion (e.g. from 7 days back to 30 days back)
        needs_backfill = False
        if low_watermark and low_watermark > target_low_utc:
            needs_backfill = True

        # Update cursor to SYNCING
        status_rec = SyncCursorRecord(
            source_key=source_key,
            source_type=SourceType.CHAT,
            account_id=account.id,
            high_watermark_cursor=high_cursor,
            low_watermark_utc=min(low_watermark or target_low_utc, target_low_utc),
            configured_lookback_days=target_lookback,
            last_sync_utc=now,
            status="SYNCING",
        )
        if hasattr(self.catalog, "upsert_sync_cursor"):
            self.catalog.upsert_sync_cursor(status_rec)

        ingested_count = 0
        try:
            rooms = adapter.list_rooms(include_dms=account.include_dms)
            for room in rooms:
                if not room.subscribed:
                    continue

                # 1. Backfill gap if lookback was expanded
                if needs_backfill and low_watermark:
                    gap_msgs, _ = adapter.fetch_messages(room.room_id, since_utc=target_low_utc, limit=100)
                    gap_filtered = [m for m in gap_msgs if m.sent_ts_utc < low_watermark]
                    payloads = BaseChatAdapter.group_messages_into_ingest_payloads(gap_filtered, account)
                    for p in payloads:
                        self.coordinator.ingest_payload(p)
                        ingested_count += 1

                # 2. Forward incremental sync
                since_time = datetime.fromisoformat(high_cursor) if high_cursor else target_low_utc
                msgs, next_cursor = adapter.fetch_messages(room.room_id, since_utc=since_time, limit=50)
                payloads = BaseChatAdapter.group_messages_into_ingest_payloads(msgs, account)
                for p in payloads:
                    self.coordinator.ingest_payload(p)
                    ingested_count += 1

                if msgs:
                    latest_ts = max(m.sent_ts_utc for m in msgs)
                    high_cursor = latest_ts.isoformat()

            status_rec = status_rec.model_copy(update={
                "status": "IDLE",
                "high_watermark_cursor": high_cursor,
                "low_watermark_utc": target_low_utc,
            })
            if hasattr(self.catalog, "upsert_sync_cursor"):
                self.catalog.upsert_sync_cursor(status_rec)

            self.event_bus.publish_event(
                event_type="SYNC_CURSOR_UPDATED",
                source_type=SourceType.CHAT,
                payload={"source_key": source_key, "ingested": ingested_count},
            )
        except Exception as e:
            status_rec = status_rec.model_copy(update={
                "status": "ERROR",
                "last_error": str(e),
            })
            if hasattr(self.catalog, "upsert_sync_cursor"):
                self.catalog.upsert_sync_cursor(status_rec)

        return ingested_count

    def sync_outlook_mail(self, account: Dict[str, Any], limit: Optional[int] = None) -> int:
        """Sync Classic Outlook mail through STA queue."""
        def _poll():
            from old_rag.ingest.outlook import ClassicMAPIConnector
            from old_rag.logger import get_logger
            logger = get_logger("pipeline.sync.outlook")
            connector = ClassicMAPIConnector()
            count = 0
            folders = account.get("folders") or ["ALL"]
            date_range = account.get("date_range", "1yr")
            as_of_date_utc = None
            if date_range == "30d":
                as_of_date_utc = datetime.now(timezone.utc) - timedelta(days=30)
            elif date_range == "90d":
                as_of_date_utc = datetime.now(timezone.utc) - timedelta(days=90)
            elif date_range == "1yr":
                as_of_date_utc = datetime.now(timezone.utc) - timedelta(days=365)
            elif date_range == "2yr":
                as_of_date_utc = datetime.now(timezone.utc) - timedelta(days=730)

            logger.info("Starting Outlook mail sync (newest first): folders=%s, date_range=%s, limit=%s", folders, date_range, limit)
            for payload in connector.poll_messages(folder_names=folders, as_of_date_utc=as_of_date_utc, limit=limit):
                self.coordinator.ingest_payload(payload)
                count += 1
            logger.info("Outlook mail sync complete: ingested/updated %d messages", count)
            return count

        try:
            return self.mapi_queue.submit(_poll, timeout=300.0)
        except Exception as e:
            from old_rag.logger import get_logger
            get_logger("pipeline.sync.outlook").warning("Outlook mail sync exception: %s (%s)", type(e).__name__, e)
            return 0

    def sync_imap_mail(self, account: Dict[str, Any]) -> int:
        """Sync IMAP mailbox with lookback support."""
        try:
            connector = IMAPConnector(
                host=account.get("imap_host", ""),
                port=account.get("imap_port", 993),
                use_ssl=account.get("imap_ssl", True),
                username=account.get("username", ""),
                password=account.get("password", ""),
            )
            count = 0
            for payload in connector.fetch_messages(
                folders=account.get("folders", ["INBOX"]),
                date_range=account.get("date_range", "30d"),
                limit_per_folder=20,
            ):
                self.coordinator.ingest_payload(payload)
                count += 1
            return count
        except Exception:
            return 0

    def sync_calendar_sources(self) -> int:
        """Sync all configured Calendar connectors (Outlook MAPI, Google, Exchange, Confluence, CalDAV)."""
        from old_rag.storage.settings import get_calendar_accounts
        from old_rag.ingest.calendar import CalendarManager
        db_path = getattr(self.catalog, "db_path", None)
        if not db_path and hasattr(self.coordinator, "cas"):
            db_path = self.coordinator.cas.app_data_dir / "catalog.db"
        if not db_path:
            return 0
        cal_mgr = CalendarManager(db_path)
        accounts = get_calendar_accounts(decrypt=True)
        count = 0
        for acc in accounts:
            if not acc.get("enabled", True):
                continue
            if acc.get("connector_type") == "outlook_mapi":
                def _poll_mapi(a=acc):
                    return cal_mgr.sync_calendar_account(a, coordinator=self.coordinator)
                try:
                    count += self.mapi_queue.submit(_poll_mapi)
                except Exception:
                    pass
            else:
                try:
                    count += cal_mgr.sync_calendar_account(acc, coordinator=self.coordinator)
                except Exception:
                    pass
        return count

    def trigger_sync(self, source_type: Optional[SourceType] = None, source_key: Optional[str] = None) -> Dict[str, Any]:
        """Manually trigger an immediate sync cycle."""
        if source_type == SourceType.CHAT or (source_key and source_key.startswith("chat_")):
            chat_accounts = get_chat_accounts(decrypt=True)
            for acc in chat_accounts:
                cfg = ChatAccountConfig.model_validate(acc)
                if not source_key or source_key == f"chat_{cfg.id}":
                    self.sync_chat_account(cfg)
        elif source_type == SourceType.EMAIL:
            email_accounts = get_accounts(decrypt=True)
            for acc in email_accounts:
                if not acc.get("enabled", True):
                    continue
                acc_type = acc.get("account_type", "")
                if acc_type == "outlook_mapi":
                    self.sync_outlook_mail(acc)
                elif acc_type == "imap_smtp":
                    self.sync_imap_mail(acc)
        elif source_type == SourceType.CALENDAR or (source_key and source_key.startswith("cal_")):
            self.sync_calendar_sources()
        else:
            self.sync_all_sources()

        return {"status": "triggered", "source_type": source_type.value if source_type else "ALL"}
