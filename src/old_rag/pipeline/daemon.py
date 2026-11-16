"""daemon.py - Multi-Process Daemon Orchestrator & Named Pipe Server."""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import (
    CalendarEvent,
    CalendarIngestMeta,
    ChatAccountConfig,
    DataTypeQuotaConfig,
    ModelConfig,
    RawIngestPayload,
    SourceType,
    StorageConfig,
    SyncPolicyConfig,
    safe_json_dumps,
    safe_json_loads,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.explainer import MatchExplainer
from old_rag.engine.extractor import DocumentExtractor, build_scryer_options_from_settings
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.engine.search import HybridSearchEngine
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.ingest.outlook import ClassicMAPIConnector, is_new_outlook_active
from old_rag.ingest.watcher import DirectoryWatcher, ingest_filesystem_path
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.lifecycle import (
    NamedPipeServer,
    SingleInstanceMutex,
    get_scoped_pipe_name,
)
from old_rag.pipeline.sync_scheduler import EventBus, RealTimeSyncScheduler
from old_rag.storage.cas import CASEngine, get_default_app_data_dir
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.quota import QuotaGovernor


class DaemonProcess:
    """Core background daemon process (Process 1)."""

    def __init__(self, storage_cfg: Optional[StorageConfig] = None, model_cfg: Optional[ModelConfig] = None):
        from old_rag.logger import get_logger, setup_logging
        setup_logging()
        self.logger = get_logger("daemon")
        self.storage_cfg = storage_cfg or StorageConfig()
        self.model_cfg = model_cfg or ModelConfig()
        self.app_data_dir = Path(self.storage_cfg.app_data_dir) if self.storage_cfg.app_data_dir else get_default_app_data_dir()
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("Initializing DaemonProcess at %s", self.app_data_dir)

        self.mutex = SingleInstanceMutex()
        self.cas = CASEngine(self.storage_cfg)
        self.catalog = CatalogRepository(self.app_data_dir / "catalog.db")

        # Initialize LanceDB table if possible
        self.lancedb_table = open_lancedb_table(
            self.app_data_dir,
            dim=self.model_cfg.matryoshka_dim,
        )

        self.indexer = IndexEngine(
            fts_db_path=self.app_data_dir / "lexical_fts.db",
            lancedb_table=self.lancedb_table,
            config=self.model_cfg,
        )
        self.extractor = DocumentExtractor(catalog=self.catalog)
        self.chunker = ParentAwareChunker(catalog=self.catalog)
        self.coordinator = PipelineCoordinator(
            cas=self.cas,
            catalog=self.catalog,
            extractor=self.extractor,
            chunker=self.chunker,
            indexer=self.indexer,
        )
        self.search_engine = HybridSearchEngine(
            fts_db_path=self.app_data_dir / "lexical_fts.db",
            lancedb_table=self.lancedb_table,
            config=self.model_cfg,
            catalog=self.catalog,
        )
        self.explainer = MatchExplainer()

        self.chat_registry = ChatPluginRegistry()
        self.quota_governor = QuotaGovernor(
            catalog=self.catalog,
            cas=self.cas,
            indexer=self.indexer,
            coordinator=self.coordinator,
        )
        self.sync_scheduler = RealTimeSyncScheduler(
            coordinator=self.coordinator,
            catalog=self.catalog,
            quota_governor=self.quota_governor,
            chat_registry=self.chat_registry,
        )
        self.event_bus = self.sync_scheduler.event_bus

        self.pipe_server = NamedPipeServer(get_scoped_pipe_name(), self.handle_client_request)
        self._running = False
        self.watcher: Optional[DirectoryWatcher] = None
        self._mapi_connector = ClassicMAPIConnector(allow_background_launch=False)

    def is_on_battery(self) -> bool:
        """Check whether system is running on battery."""
        try:
            import psutil
            b = psutil.sensors_battery()
            if b is not None and not b.power_plugged:
                return True
        except Exception:
            pass
        return False

    def handle_client_request(self, raw_req: str) -> str:
        """Handle JSON IPC request from UI, MCP server, or CLI."""
        try:
            req = safe_json_loads(raw_req)
            action = req.get("action")
            if action in ("status", "get_status"):
                lance_count = 0
                if self.lancedb_table:
                    try:
                        lance_count = self.lancedb_table.count_rows()
                    except Exception:
                        pass
                res = {
                    "daemon_running": True,
                    "vlm_active": False,
                    "items_indexed": lance_count,
                    "pending_outbox_count": len(self.catalog.get_pending_outbox()),
                    "classic_outlook_active": not is_new_outlook_active(),
                    "on_battery": self.is_on_battery(),
                    "lancedb_count": lance_count,
                    "sync_progress_pct": 100.0,
                }
                return safe_json_dumps(res)
            elif action in ("search", "query_personal_archive"):
                from old_rag.contracts import SearchQuery
                q_dict = req.get("query", {})
                if not q_dict and "query_text" in req:
                    q_dict = {
                        "query_text": req.get("query_text", ""),
                        "top_k": req.get("top_k", 20),
                        "folder": req.get("folder"),
                        "sender": req.get("sender"),
                    }
                q = SearchQuery.model_validate(q_dict)
                results = self.search_engine.search(q)
                return safe_json_dumps({"results": [r.model_dump(mode="json") for r in results]})
            elif action == "get_archived_document":
                doc_id = req.get("doc_id")
                if not doc_id:
                    return safe_json_dumps({"error": "doc_id required"})
                norm_doc = self.cas.get_text(doc_id)
                if norm_doc:
                    return safe_json_dumps({"document": norm_doc.model_dump()})
                return safe_json_dumps({"error": f"Document {doc_id} not found in archive"})
            elif action == "list_recent_threads":
                limit = int(req.get("limit", 20))
                conn = self.catalog._get_connection()
                cur = conn.execute(
                    """
                    SELECT doc_id, subject, sender, folder, received_ts_utc, conversation_id
                    FROM documents
                    WHERE is_deleted = 0
                    ORDER BY received_ts_utc DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cur.fetchall()]
                return safe_json_dumps({"threads": rows})
            elif action == "open_cached_msg":
                doc_id = req.get("doc_id")
                temp_path = self.cas.create_temp_view_file(doc_id)
                if temp_path:
                    return safe_json_dumps({"success": True, "temp_view_path": str(temp_path)})
                return safe_json_dumps({"success": False, "error": "Not found in CAS"})
            elif action == "reindex":
                count = self.coordinator.reindex_all()
                return safe_json_dumps({"success": True, "count": count})
            elif action == "purge":
                orphans = self.catalog.prune_orphans(purge_source_deleted=True)
                for h in orphans:
                    self.cas.delete_blob(h)
                return safe_json_dumps({"success": True, "pruned_count": len(orphans)})
            elif action == "get_admin_overview":
                from old_rag.storage.settings import get_storage_breakdown, load_settings, validate_storage_location
                settings = load_settings()
                storage_breakdown = get_storage_breakdown(self.app_data_dir)
                privacy_check = validate_storage_location(str(self.app_data_dir))

                outlook_ok = False
                try:
                    self._mapi_connector.check_availability()
                    outlook_ok = True
                except Exception:
                    pass

                lance_count = 0
                if self.lancedb_table is not None:
                    try:
                        lance_count = self.lancedb_table.count_rows()
                    except Exception:
                        pass

                doc_count = 0
                conn = self.catalog._get_connection()
                try:
                    cur = conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted = 0")
                    doc_count = cur.fetchone()[0]
                except Exception:
                    pass

                connections = [
                    {
                        "id": "outlook",
                        "name": "Classic Outlook (MAPI)",
                        "type": "email",
                        "status": "CONNECTED" if outlook_ok else "DISCONNECTED",
                        "details": f"{doc_count} items ingested" if outlook_ok else "Outlook not detected or closed",
                        "last_sync": "Live MAPI Poller",
                        "enabled": settings.get("connectors", {}).get("outlook", {}).get("enabled", True),
                    },
                    {
                        "id": "exchange_owa",
                        "name": "Remote Exchange / OWA",
                        "type": "email",
                        "status": "CONFIGURED" if settings.get("connectors", {}).get("exchange_owa", {}).get("server_url") else "NOT CONFIGURED",
                        "details": settings.get("connectors", {}).get("exchange_owa", {}).get("server_url") or "No remote server specified",
                        "last_sync": "N/A",
                        "enabled": settings.get("connectors", {}).get("exchange_owa", {}).get("enabled", False),
                    },
                    {
                        "id": "imap",
                        "name": f"IMAP ({settings.get('connectors', {}).get('imap', {}).get('provider', 'gmail').capitalize()})",
                        "type": "email",
                        "status": "CONFIGURED" if settings.get("connectors", {}).get("imap", {}).get("username") else "NOT CONFIGURED",
                        "details": settings.get("connectors", {}).get("imap", {}).get("host") or "imap.gmail.com",
                        "last_sync": "On-demand",
                        "enabled": settings.get("connectors", {}).get("imap", {}).get("enabled", False),
                    },
                    {
                        "id": "filesystem",
                        "name": "Filesystem Monitor",
                        "type": "file",
                        "status": "ACTIVE" if (self.watcher is not None or settings.get("connectors", {}).get("filesystem", {}).get("enabled", True)) else "IDLE",
                        "details": f"{len(settings.get('connectors', {}).get('filesystem', {}).get('paths', []))} directories configured",
                        "last_sync": "Recursive Watcher Active",
                        "enabled": settings.get("connectors", {}).get("filesystem", {}).get("enabled", True),
                    },
                    {
                        "id": "lancedb",
                        "name": "LanceDB Vector Engine",
                        "type": "index",
                        "status": "ONLINE",
                        "details": f"{lance_count} chunks indexed (Matryoshka 768-dim)",
                        "last_sync": "Real-time WAL",
                        "enabled": True,
                    },
                    {
                        "id": "fts5",
                        "name": "SQLite FTS5 Porter & Trigram",
                        "type": "index",
                        "status": "ONLINE",
                        "details": "Dual-table stemmed lexical index active",
                        "last_sync": "Real-time WAL",
                        "enabled": True,
                    },
                ]

                return safe_json_dumps({
                    "connections": connections,
                    "storage": storage_breakdown,
                    "privacy": privacy_check,
                    "items_indexed": doc_count,
                    "chunks_indexed": lance_count,
                    "daemon_running": True,
                    "is_sovereign": True,
                })
            elif action == "get_statistics":
                stats = self.catalog.get_statistics()
                lance_count = 0
                if self.lancedb_table is not None:
                    try:
                        lance_count = self.lancedb_table.count_rows()
                    except Exception:
                        pass
                stats["total_chunks"] = lance_count
                return safe_json_dumps(stats)
            elif action == "get_settings":
                from old_rag.storage.settings import load_settings
                return safe_json_dumps({"settings": load_settings()})
            elif action == "save_settings":
                from old_rag.storage.settings import save_settings, validate_storage_location
                new_settings = req.get("settings", {})
                validation = validate_storage_location(new_settings.get("storage_location", ""))
                save_settings(new_settings)
                self.extractor.default_options = build_scryer_options_from_settings(new_settings)
                return safe_json_dumps({"success": True, "privacy": validation})
            elif action == "test_imap_connection":
                from old_rag.ingest.imap import IMAPConnector
                imap_cfg = req.get("config", {})
                conn = IMAPConnector(
                    host=imap_cfg.get("host", "imap.gmail.com"),
                    port=int(imap_cfg.get("port", 993)),
                    use_ssl=bool(imap_cfg.get("ssl", True)),
                    username=imap_cfg.get("username", ""),
                    password=imap_cfg.get("password", ""),
                )
                res = conn.test_connection()
                return safe_json_dumps(res)
            elif action == "sweep_temp_views":
                from old_rag.storage.settings import sweep_temp_views
                res = sweep_temp_views(self.app_data_dir)
                return safe_json_dumps(res)
            elif action == "list_accounts":
                from old_rag.storage.settings import get_accounts
                return safe_json_dumps({"accounts": get_accounts()})
            elif action == "save_account":
                from old_rag.storage.settings import save_account
                acc = save_account(req.get("account", {}))
                return safe_json_dumps({"success": True, "account": acc})
            elif action == "delete_account":
                from old_rag.storage.settings import delete_account
                acc_id = req.get("account_id", "")
                purge_data = bool(req.get("purge_data", False))
                coord = self.coordinator if purge_data else None
                ok = delete_account(acc_id, purge_data=purge_data, coordinator=coord)
                return safe_json_dumps({"success": ok})
            elif action == "purge_account_data":
                acc_id = req.get("account_id", "")
                source_type = req.get("source_type")
                res = self.coordinator.purge_account_data(acc_id, source_type=source_type)
                return safe_json_dumps(res)
            elif action == "list_calendar_accounts":
                from old_rag.storage.settings import get_calendar_accounts
                return safe_json_dumps({"accounts": get_calendar_accounts()})
            elif action == "save_calendar_account":
                from old_rag.storage.settings import save_calendar_account
                acc = save_calendar_account(req.get("account", {}))
                return safe_json_dumps({"success": True, "account": acc})
            elif action == "delete_calendar_account":
                from old_rag.storage.settings import delete_calendar_account
                acc_id = req.get("account_id", "")
                purge_data = bool(req.get("purge_data", False))
                coord = self.coordinator if purge_data else None
                ok = delete_calendar_account(acc_id, purge_data=purge_data, coordinator=coord)
                return safe_json_dumps({"success": ok})
            elif action == "save_dashboard_layout":
                from old_rag.storage.settings import save_dashboard_layout
                layout = req.get("layout", {})
                return safe_json_dumps({"success": True, "layout": save_dashboard_layout(layout)})
            elif action == "get_dashboard_layout":
                from old_rag.storage.settings import get_dashboard_layout
                return safe_json_dumps({"layout": get_dashboard_layout()})
            elif action == "list_mailbox_folders":
                acc_id = req.get("account_id", "")
                from old_rag.storage.settings import get_account
                acc = get_account(acc_id) if acc_id else None
                folders = ["Inbox", "Sent Items", "Archive", "Trash"]
                if acc and acc.get("account_type") == "outlook_mapi":
                    folders = self._mapi_connector.list_folders()
                elif acc and acc.get("imap_host"):
                    from old_rag.ingest.imap import IMAPConnector
                    c = IMAPConnector(acc["imap_host"], acc.get("imap_port", 993), acc.get("imap_ssl", True), acc.get("username", ""), acc.get("password", ""))
                    t = c.test_connection()
                    if t.get("folders"):
                        folders = t["folders"]
                return safe_json_dumps({"folders": folders})
            elif action in ("list_messages", "list_mail"):
                account_id = req.get("account_id")
                folder = req.get("folder")
                limit = int(req.get("limit", 50))
                offset = int(req.get("offset", 0))
                search_term = req.get("search", "").strip()

                conn = self.catalog._get_connection()
                q = "SELECT doc_id, sender, subject, folder, received_ts_utc FROM documents WHERE is_deleted = 0"
                params: List[Any] = []
                if folder and folder != "ALL":
                    q += " AND (folder = ? OR folder LIKE ?)"
                    params.extend([folder, f"%{folder}%"])
                if search_term:
                    q += " AND (subject LIKE ? OR sender LIKE ?)"
                    params.extend([f"%{search_term}%", f"%{search_term}%"])
                q += " ORDER BY received_ts_utc DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                cur = conn.execute(q, params)
                rows = [dict(r) for r in cur.fetchall()]
                return safe_json_dumps({"messages": rows})
            elif action in ("get_message_detail", "get_mail_detail"):
                doc_id = req.get("doc_id", "")
                norm_doc = self.cas.get_text(doc_id)
                if not norm_doc:
                    return safe_json_dumps({"error": "Message not found in archive"})
                doc_dict = norm_doc.model_dump(mode="json")
                doc_dict["has_archived_msg"] = self.cas.has_msg(doc_id)
                return safe_json_dumps({"message": doc_dict})
            elif action in ("send_email", "send_mail"):
                acc_id = req.get("account_id", "")
                from old_rag.storage.settings import get_account
                acc = get_account(acc_id) if acc_id else None
                to_addrs = req.get("to", [])
                cc_addrs = req.get("cc", [])
                bcc_addrs = req.get("bcc", [])
                subject = req.get("subject", "")
                body_html = req.get("body_html", "")
                body_text = req.get("body_text", "")
                attachments = req.get("attachments", [])

                if acc and acc.get("account_type") == "outlook_mapi":
                    res = self._mapi_connector.send_mail_mapi(to_addrs, cc_addrs, bcc_addrs, subject, body_html, body_text, attachments)
                    return safe_json_dumps(res)
                elif acc and acc.get("smtp_host"):
                    from old_rag.ingest.smtp import SMTPSender
                    sender = SMTPSender(
                        host=acc["smtp_host"],
                        port=acc.get("smtp_port", 587),
                        use_ssl=acc.get("smtp_ssl", False),
                        use_starttls=acc.get("smtp_starttls", True),
                        username=acc.get("username", ""),
                        password=acc.get("password", ""),
                    )
                    res = sender.send_message(
                        from_addr=acc.get("email_address") or acc.get("username", ""),
                        to_addrs=to_addrs,
                        cc_addrs=cc_addrs,
                        bcc_addrs=bcc_addrs,
                        subject=subject,
                        body_html=body_html,
                        body_text=body_text,
                        attachments=attachments,
                    )
                    if res.get("success") and acc.get("imap_host") and "raw_message" in res:
                        from old_rag.ingest.imap import IMAPConnector
                        ic = IMAPConnector(acc["imap_host"], acc.get("imap_port", 993), acc.get("imap_ssl", True), acc.get("username", ""), acc.get("password", ""))
                        ic.append_sent_message(res["raw_message"].encode("utf-8"))
                    return safe_json_dumps(res)
                else:
                    res = self._mapi_connector.send_mail_mapi(to_addrs, cc_addrs, bcc_addrs, subject, body_html, body_text, attachments)
                    return safe_json_dumps(res)
            elif action == "list_calendar_events":
                from old_rag.ingest.calendar import CalendarManager
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                cm.sync_outlook_calendar()
                events = cm.list_events()
                return safe_json_dumps({"events": [e.model_dump(mode="json") for e in events]})
            elif action == "create_calendar_event":
                import uuid
                from datetime import datetime
                from old_rag.contracts import CalendarEvent, SourceType
                from old_rag.ingest.calendar import CalendarManager
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                evt_dict = req.get("event", {})
                evt_id = f"evt_{uuid.uuid4().hex[:10]}"
                evt = CalendarEvent(
                    event_id=evt_id,
                    account_id=evt_dict.get("account_id"),
                    source_type=SourceType.EMAIL,
                    subject=evt_dict.get("subject", "Meeting"),
                    location=evt_dict.get("location"),
                    start_ts_utc=datetime.fromisoformat(evt_dict["start_ts_utc"]),
                    end_ts_utc=datetime.fromisoformat(evt_dict["end_ts_utc"]),
                    is_all_day=bool(evt_dict.get("is_all_day", False)),
                    organizer=evt_dict.get("organizer"),
                    attendees=evt_dict.get("attendees", []),
                    body_text=evt_dict.get("body_text", ""),
                )
                cm.upsert_event(evt)
                try:
                    self._mapi_connector.create_calendar_event(
                        subject=evt.subject,
                        start_dt=evt.start_ts_utc,
                        end_dt=evt.end_ts_utc,
                        location=evt.location or "",
                        body=evt.body_text or "",
                        all_day=evt.is_all_day,
                    )
                except Exception:
                    pass
                return safe_json_dumps({"success": True, "event": evt.model_dump(mode="json")})
            elif action == "ingest_path":
                path_str = req.get("path", "")
                recursive = bool(req.get("recursive", True))
                stats = ingest_filesystem_path(
                    Path(path_str),
                    self.coordinator,
                    recursive=recursive,
                )
                return safe_json_dumps({"success": True, **stats})
            elif action == "scan_head_of_folder":
                from old_rag.engine.folder_index import HeadOfFolderIndex
                folder_path = req.get("folder_path", "")
                recursive = bool(req.get("recursive", True))
                idx = HeadOfFolderIndex(Path(folder_path))
                res = idx.scan_and_index(recursive=recursive)
                return safe_json_dumps(res)
            elif action == "get_head_of_folder_status":
                from old_rag.engine.folder_index import HeadOfFolderIndex
                folder_path = req.get("folder_path", "")
                idx = HeadOfFolderIndex(Path(folder_path))
                return safe_json_dumps(idx.get_status())
            elif action == "get_related_context":
                from old_rag.engine.search import resolve_related_context
                from old_rag.ingest.calendar import CalendarManager
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                res = resolve_related_context(
                    search_engine=self.search_engine,
                    catalog=self.catalog,
                    calendar_manager=cm,
                    doc_id=req.get("doc_id"),
                    subject=req.get("subject", ""),
                    text=req.get("text", ""),
                    top_k=int(req.get("top_k", 6)),
                )
                return safe_json_dumps(res)
            elif action == "open_document":
                from old_rag.engine.search import resolve_open_document
                res = resolve_open_document(
                    cas=self.cas,
                    catalog=self.catalog,
                    doc_id=req.get("doc_id"),
                    original_path=req.get("original_path"),
                )
                return safe_json_dumps(res)
            elif action == "explain_match_tier1":
                from old_rag.engine.explainer import explain_match_tier1
                from old_rag.ingest.calendar import CalendarManager
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                res = explain_match_tier1(
                    fts_db_path=self.app_data_dir / "lexical_fts.db",
                    catalog=self.catalog,
                    cas=self.cas,
                    calendar_manager=cm,
                    rrf_k=float(self.model_cfg.rrf_k),
                    payload=req,
                )
                return safe_json_dumps(res)
            elif action == "explain_match_tier2":
                res = self.explainer.explain_tier2(
                    payload=req,
                    force_refresh=bool(req.get("force_refresh", False)),
                )
                return safe_json_dumps(res)
            elif action == "import_ics":
                from old_rag.ingest.calendar import CalendarManager, event_to_ingest_payload, parse_ics_content
                ics_text = req.get("ics_content", "")
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                events = parse_ics_content(ics_text)
                for evt in events:
                    cm.upsert_event(evt)
                    p = event_to_ingest_payload(evt)
                    self.coordinator.ingest_payload(p)
                return safe_json_dumps({"success": True, "count": len(events)})
            elif action == "export_ics":
                from old_rag.ingest.calendar import CalendarManager, export_events_to_ics
                cm = CalendarManager(self.app_data_dir / "catalog.db")
                event_ids = req.get("event_ids")
                events = cm.list_events()
                if event_ids:
                    events = [e for e in events if e.event_id in event_ids]
                ics_text = export_events_to_ics(events)
                return safe_json_dumps({"success": True, "ics_content": ics_text})
            elif action == "record_access":
                doc_id = req.get("doc_id")
                if doc_id:
                    self.catalog.record_document_access(doc_id)
                    return safe_json_dumps({"success": True, "doc_id": doc_id})
                return safe_json_dumps({"error": "doc_id required"})
            elif action == "list_chat_plugins":
                plugins = self.chat_registry.list_plugins()
                return safe_json_dumps({"plugins": [p.model_dump(mode="json") for p in plugins]})
            elif action == "list_chat_accounts":
                from old_rag.storage.settings import get_chat_accounts
                accounts = get_chat_accounts(decrypt=False)
                return safe_json_dumps({"accounts": accounts})
            elif action == "save_chat_account":
                from old_rag.storage.settings import save_chat_account
                acc = save_chat_account(req.get("account", {}))
                return safe_json_dumps({"success": True, "account": acc})
            elif action == "delete_chat_account":
                from old_rag.storage.settings import delete_chat_account
                acc_id = req.get("account_id", "")
                purge_data = bool(req.get("purge_data", False))
                coord = self.coordinator if purge_data else None
                ok = delete_chat_account(acc_id, purge_data=purge_data, coordinator=coord)
                return safe_json_dumps({"success": ok})
            elif action == "test_chat_connection":
                acc_dict = req.get("account", {})
                acc_cfg = ChatAccountConfig.model_validate(acc_dict)
                adapter = self.chat_registry.create_adapter(acc_cfg)
                ok = adapter.test_connection()
                return safe_json_dumps({"success": ok})
            elif action == "list_chat_rooms":
                account_id = req.get("account_id")
                rooms = self.catalog.list_chat_rooms(account_id=account_id)
                return safe_json_dumps({"rooms": [r.model_dump(mode="json") for r in rooms]})
            elif action == "update_room_subscription":
                room_id = req.get("room_id")
                subscribed = bool(req.get("subscribed", True))
                conn = self.catalog._get_connection()
                conn.execute("UPDATE chat_rooms SET subscribed = ? WHERE room_id = ?", (1 if subscribed else 0, room_id))
                conn.commit()
                return safe_json_dumps({"success": True, "room_id": room_id, "subscribed": subscribed})
            elif action == "list_chat_messages":
                room_id = req.get("room_id")
                limit = int(req.get("limit", 50))
                since_utc = req.get("since_utc")
                since_dt = datetime.fromisoformat(since_utc) if since_utc else None
                msgs = self.catalog.list_chat_messages(room_id=room_id, since_utc=since_dt, limit=limit)
                return safe_json_dumps({"messages": [m.model_dump(mode="json") for m in msgs]})
            elif action == "send_chat_message":
                account_id = req.get("account_id", "")
                room_id = req.get("room_id", "")
                body_text = req.get("body_text", "")
                thread_root_id = req.get("thread_root_id")
                from old_rag.storage.settings import get_chat_accounts
                accounts = get_chat_accounts(decrypt=True)
                target_acc = next((a for a in accounts if a.get("id") == account_id), None)
                if not target_acc:
                    return safe_json_dumps({"success": False, "error": f"Chat account {account_id} not found"})
                acc_cfg = ChatAccountConfig.model_validate(target_acc)
                adapter = self.chat_registry.create_adapter(acc_cfg)
                msg_item = adapter.send_message(room_id=room_id, body_text=body_text, thread_root_id=thread_root_id)
                self.catalog.upsert_chat_messages([msg_item])
                return safe_json_dumps({"success": True, "message": msg_item.model_dump(mode="json")})
            elif action == "get_sync_status":
                cursors = self.catalog.get_sync_cursors()
                return safe_json_dumps({
                    "daemon_running": self._running,
                    "cursors": [c.model_dump(mode="json") for c in cursors],
                    "last_sync_utc": datetime.now(timezone.utc).isoformat(),
                })
            elif action == "trigger_sync":
                st_val = req.get("source_type")
                st = SourceType(st_val) if st_val else None
                sk = req.get("source_key")
                res = self.sync_scheduler.trigger_sync(source_type=st, source_key=sk)
                return safe_json_dumps({"success": True, **res})
            elif action == "update_sync_policy":
                from old_rag.storage.settings import save_sync_policy
                pol_dict = req.get("policy", {})
                pol = SyncPolicyConfig.model_validate(pol_dict)
                save_sync_policy(pol)
                return safe_json_dumps({"success": True, "policy": pol.model_dump(mode="json")})
            elif action == "get_quota_status":
                st_val = req.get("source_type")
                if st_val:
                    st = SourceType(st_val)
                    status = self.quota_governor.get_status(st)
                    return safe_json_dumps(status.model_dump(mode="json"))
                else:
                    statuses = [self.quota_governor.get_status(st).model_dump(mode="json") for st in SourceType]
                    return safe_json_dumps({"statuses": statuses})
            elif action == "update_quota_policy":
                from old_rag.storage.settings import save_quota_config
                cfg_dict = req.get("config", {})
                cfg = DataTypeQuotaConfig.model_validate(cfg_dict)
                save_quota_config(cfg)
                return safe_json_dumps({"success": True, "config": cfg.model_dump(mode="json")})
            elif action == "preview_quota_eviction":
                st_val = req.get("source_type")
                st = SourceType(st_val)
                res = self.quota_governor.enforce_quota(st, dry_run=True)
                return safe_json_dumps(res.model_dump(mode="json"))
            elif action == "run_quota_purge":
                st_val = req.get("source_type")
                dry_run = bool(req.get("dry_run", False))
                st = SourceType(st_val)
                res = self.quota_governor.enforce_quota(st, dry_run=dry_run)
                return safe_json_dumps(res.model_dump(mode="json"))
            elif action == "poll_events":
                since_seq = int(req.get("since_seq", 0))
                events = self.event_bus.get_events_since(since_seq)
                latest_seq = events[-1].seq if events else since_seq
                return safe_json_dumps({
                    "events": [e.model_dump(mode="json") for e in events],
                    "latest_seq": latest_seq,
                })
            return safe_json_dumps({"error": f"Unknown action {action}"})
        except Exception as e:
            return safe_json_dumps({"error": str(e)})

    def run(self) -> None:
        if not self.mutex.acquire():
            print("Another instance of OldRAG daemon is already running in this session.")
            sys.exit(1)

        self._running = True
        # 1. Clean decrypted temp view files from previous sessions
        self.cas.sweep_tmp()

        # 2. Crash recovery: process uncommitted outbox items
        recovered = self.coordinator.process_outbox()
        if recovered > 0:
            print(f"Recovered {recovered} uncommitted outbox items on startup.")

        # 3. Start Filesystem Watcher if configured
        try:
            from old_rag.storage.settings import load_settings
            settings = load_settings()
            fs_cfg = settings.get("connectors", {}).get("filesystem", {})
            watch_paths = [Path(p) for p in fs_cfg.get("paths", []) if p and Path(p).exists()]
            if fs_cfg.get("enabled", True) and watch_paths:
                self.watcher = DirectoryWatcher(
                    watch_paths,
                    on_payload=self.coordinator.ingest_payload,
                )
                self.watcher.start()
        except Exception:
            pass

        # 4. Start RealTimeSyncScheduler
        try:
            self.sync_scheduler.start()
        except Exception:
            pass

        # 5. Start Named Pipe IPC Server
        self.pipe_server.start()
        print(f"OldRAG Daemon running. IPC Pipe: {get_scoped_pipe_name()}")

        try:
            while self._running:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._running = False
        try:
            self.sync_scheduler.stop()
        except Exception:
            pass
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                pass
        self.pipe_server.stop()
        self.cas.sweep_tmp()
        self.mutex.release()
