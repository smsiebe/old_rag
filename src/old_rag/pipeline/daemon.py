"""daemon.py - Multi-Process Daemon Orchestrator & Named Pipe Server."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from old_rag.contracts import ModelConfig, StorageConfig, safe_json_dumps, safe_json_loads
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.explainer import MatchExplainer
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine
from old_rag.engine.search import HybridSearchEngine
from old_rag.ingest.outlook import ClassicMAPIConnector, is_new_outlook_active
from old_rag.ingest.watcher import DirectoryWatcher
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.lifecycle import (
    NamedPipeServer,
    SingleInstanceMutex,
    get_scoped_pipe_name,
)
from old_rag.storage.cas import CASEngine, get_default_app_data_dir
from old_rag.storage.catalog import CatalogRepository


class DaemonProcess:
    """Core background daemon process (Process 1)."""

    def __init__(self, storage_cfg: Optional[StorageConfig] = None, model_cfg: Optional[ModelConfig] = None):
        self.storage_cfg = storage_cfg or StorageConfig()
        self.model_cfg = model_cfg or ModelConfig()
        self.app_data_dir = Path(self.storage_cfg.app_data_dir) if self.storage_cfg.app_data_dir else get_default_app_data_dir()
        self.app_data_dir.mkdir(parents=True, exist_ok=True)

        self.mutex = SingleInstanceMutex()
        self.cas = CASEngine(self.storage_cfg)
        self.catalog = CatalogRepository(self.app_data_dir / "catalog.db")

        # Initialize LanceDB table if possible
        self.lancedb_table = None
        try:
            import lancedb
            from old_rag.schema import get_lancedb_pyarrow_schema
            lancedb_dir = self.app_data_dir / "lancedb"
            lancedb_dir.mkdir(parents=True, exist_ok=True)
            db = lancedb.connect(str(lancedb_dir))
            schema = get_lancedb_pyarrow_schema(dim=self.model_cfg.matryoshka_dim)
            self.lancedb_table = db.create_table("chunks_vec", schema=schema, exist_ok=True)
        except Exception:
            self.lancedb_table = None

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
            if action == "status":
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
                ok = delete_account(req.get("account_id", ""))
                return safe_json_dumps({"success": ok})
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
            elif action == "list_messages":
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
            elif action == "get_message_detail":
                doc_id = req.get("doc_id", "")
                norm_doc = self.cas.get_text(doc_id)
                if not norm_doc:
                    return safe_json_dumps({"error": "Message not found in archive"})
                doc_dict = norm_doc.model_dump(mode="json")
                doc_dict["has_archived_msg"] = self.cas.has_msg(doc_id)
                return safe_json_dumps({"message": doc_dict})
            elif action == "send_email":
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

        # 3. Start Named Pipe IPC Server
        self.pipe_server.start()
        print(f"OldRAG Daemon running. IPC Pipe: {get_scoped_pipe_name()}")

        try:
            while self._running:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._running = False
        self.pipe_server.stop()
        self.cas.sweep_tmp()
        self.mutex.release()
