"""app.py - pywebview Companion Overlay & Zero-Socket Native Bridge."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from old_rag.pipeline.lifecycle import NamedPipeClient, get_scoped_pipe_name


class BridgeAPI:
    """Exposes window.pywebview.api methods to Javascript over local Named Pipe IPC with in-process fallback."""

    def __init__(self, pipe_name: Optional[str] = None):
        self.client = NamedPipeClient(pipe_name)
        self._cas = None
        self._catalog = None
        self._search_engine = None
        self._explainer = None

    @staticmethod
    def _is_ipc_transport_error(res: Any) -> bool:
        if not isinstance(res, dict) or "error" not in res:
            return False
        err = str(res.get("error") or "")
        return err.startswith("Named Pipe") or err.startswith("Unknown action")

    def _get_explainer(self):
        if self._explainer is None:
            from old_rag.engine.explainer import MatchExplainer
            self._explainer = MatchExplainer()
        return self._explainer

    def _get_in_process_engine(self):
        if self._search_engine is None:
            from old_rag.engine.search import HybridSearchEngine
            from old_rag.storage.cas import CASEngine, get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            self._cas = CASEngine()
            lancedb_table = None
            try:
                import lancedb
                ldb = lancedb.connect(str(app_dir / "lancedb"))
                tables = ldb.list_tables() if hasattr(ldb, "list_tables") else ldb.table_names()
                if "chunks_vec" in tables:
                    lancedb_table = ldb.open_table("chunks_vec")
            except Exception:
                pass
            from old_rag.storage.catalog import CatalogRepository
            self._catalog = CatalogRepository(app_dir / "catalog.db")
            self._search_engine = HybridSearchEngine(
                fts_db_path=app_dir / "lexical_fts.db",
                lancedb_table=lancedb_table,
                catalog=self._catalog,
            )
        return self._search_engine, self._cas

    def search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "search", "query": request})
        if "error" in res:
            # Fallback to direct in-process search
            try:
                engine, _ = self._get_in_process_engine()
                from old_rag.contracts import SearchQuery
                q = SearchQuery.model_validate(request)
                hits = engine.search(q)
                return {"results": [h.model_dump(mode="json") for h in hits]}
            except Exception as e:
                return {"error": str(e), "results": []}
        return res

    def get_status(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "status"})
        if "error" in res:
            # In-process status fallback
            from old_rag.ingest.outlook import is_new_outlook_active
            from old_rag.storage.cas import get_default_app_data_dir
            from old_rag.storage.catalog import CatalogRepository
            app_dir = get_default_app_data_dir()
            cat = CatalogRepository(app_dir / "catalog.db")
            doc_count = 0
            try:
                row = cat._get_connection().execute("SELECT COUNT(*) FROM documents").fetchone()
                if row:
                    doc_count = row[0]
            except Exception:
                pass
            return {
                "daemon_running": False,
                "vlm_active": False,
                "items_indexed": doc_count,
                "pending_outbox_count": len(cat.get_pending_outbox()),
                "classic_outlook_active": not is_new_outlook_active(),
                "on_battery": False,
            }
        return res

    def open_cached_msg(self, request: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = request.get("doc_id", "")
        res = self.client.call({"action": "open_cached_msg", "doc_id": doc_id})
        temp_path = None
        if "temp_view_path" in res and res.get("success"):
            temp_path = res["temp_view_path"]
        elif "error" in res:
            _, cas = self._get_in_process_engine()
            p = cas.create_temp_view_file(doc_id)
            if p:
                temp_path = str(p)
                res = {"success": True, "temp_view_path": temp_path}

        if temp_path and sys.platform == "win32":
            try:
                import os
                os.startfile(temp_path)
            except Exception:
                pass
        return res

    def reindex(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "reindex"})
        if "error" in res:
            from old_rag.engine.chunker import ParentAwareChunker
            from old_rag.engine.extractor import DocumentExtractor
            from old_rag.engine.indexer import IndexEngine, open_lancedb_table
            from old_rag.pipeline.coordinator import PipelineCoordinator
            from old_rag.storage.cas import CASEngine, get_default_app_data_dir
            from old_rag.storage.catalog import CatalogRepository
            app_dir = get_default_app_data_dir()
            cas = CASEngine()
            cat = CatalogRepository(app_dir / "catalog.db")
            coord = PipelineCoordinator(
                cas=cas,
                catalog=cat,
                extractor=DocumentExtractor(catalog=cat),
                chunker=ParentAwareChunker(catalog=cat),
                indexer=IndexEngine(app_dir / "lexical_fts.db", lancedb_table=open_lancedb_table(app_dir)),
            )
            count = coord.reindex_all()
            return {"success": True, "count": count}
        return res

    def get_admin_overview(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "get_admin_overview"})
        if "error" in res:
            from old_rag.ingest.outlook import ClassicMAPIConnector
            from old_rag.storage.cas import get_default_app_data_dir
            from old_rag.storage.catalog import CatalogRepository
            from old_rag.storage.settings import get_storage_breakdown, load_settings, validate_storage_location
            app_dir = get_default_app_data_dir()
            settings = load_settings()
            storage_breakdown = get_storage_breakdown(app_dir)
            privacy_check = validate_storage_location(str(app_dir))

            outlook_ok = False
            try:
                ClassicMAPIConnector().check_availability()
                outlook_ok = True
            except Exception:
                pass

            lance_count = 0
            try:
                import lancedb
                ldb = lancedb.connect(str(app_dir / "lancedb"))
                tables = ldb.list_tables() if hasattr(ldb, "list_tables") else ldb.table_names()
                if "chunks_vec" in tables:
                    tbl = ldb.open_table("chunks_vec")
                    lance_count = tbl.count_rows()
            except Exception:
                pass

            doc_count = 0
            try:
                cat = CatalogRepository(app_dir / "catalog.db")
                conn = cat._get_connection()
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
                    "last_sync": "Live STA Ingestion",
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
                    "status": "ACTIVE" if settings.get("connectors", {}).get("filesystem", {}).get("enabled", True) else "IDLE",
                    "details": f"{len(settings.get('connectors', {}).get('filesystem', {}).get('paths', []))} directories configured",
                    "last_sync": "Recursive Monitoring",
                    "enabled": settings.get("connectors", {}).get("filesystem", {}).get("enabled", True),
                },
                {
                    "id": "lancedb",
                    "name": "LanceDB Vector Engine",
                    "type": "index",
                    "status": "ONLINE",
                    "details": f"{lance_count} chunks indexed (Matryoshka 768-dim)",
                    "last_sync": "Real-time",
                    "enabled": True,
                },
                {
                    "id": "fts5",
                    "name": "SQLite FTS5 Porter & Trigram",
                    "type": "index",
                    "status": "ONLINE",
                    "details": "Dual-table stemmed lexical index active",
                    "last_sync": "Real-time",
                    "enabled": True,
                },
            ]

            return {
                "connections": connections,
                "storage": storage_breakdown,
                "privacy": privacy_check,
                "items_indexed": doc_count,
                "chunks_indexed": lance_count,
                "daemon_running": False,
                "is_sovereign": True,
            }
        return res

    def get_statistics(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "get_statistics"})
        if "error" in res:
            from old_rag.storage.cas import get_default_app_data_dir
            from old_rag.storage.catalog import CatalogRepository
            app_dir = get_default_app_data_dir()
            cat = CatalogRepository(app_dir / "catalog.db")
            stats = cat.get_statistics()
            try:
                import lancedb
                ldb = lancedb.connect(str(app_dir / "lancedb"))
                tables = ldb.list_tables() if hasattr(ldb, "list_tables") else ldb.table_names()
                if "chunks_vec" in tables:
                    stats["total_chunks"] = ldb.open_table("chunks_vec").count_rows()
            except Exception:
                stats["total_chunks"] = 0
            return stats
        return res

    def get_settings(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "get_settings"})
        if "error" in res:
            from old_rag.storage.settings import load_settings
            return {"settings": load_settings()}
        return res

    def save_settings(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "save_settings", "settings": request.get("settings", {})})
        if "error" in res:
            from old_rag.storage.settings import save_settings, validate_storage_location
            new_settings = request.get("settings", {})
            validation = validate_storage_location(new_settings.get("storage_location", ""))
            save_settings(new_settings)
            return {"success": True, "privacy": validation}
        return res

    def test_imap_connection(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "test_imap_connection", "config": request.get("config", {})})
        if "error" in res:
            from old_rag.ingest.imap import IMAPConnector
            imap_cfg = request.get("config", {})
            conn = IMAPConnector(
                host=imap_cfg.get("host", "imap.gmail.com"),
                port=int(imap_cfg.get("port", 993)),
                use_ssl=bool(imap_cfg.get("ssl", True)),
                username=imap_cfg.get("username", ""),
                password=imap_cfg.get("password", ""),
            )
            return conn.test_connection()
        return res

    def sweep_temp_views(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "sweep_temp_views"})
        if "error" in res:
            from old_rag.storage.cas import get_default_app_data_dir
            from old_rag.storage.settings import sweep_temp_views
            return sweep_temp_views(get_default_app_data_dir())
        return res

    def list_outlook_folders(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from old_rag.ingest.outlook import ClassicMAPIConnector
        connector = ClassicMAPIConnector()
        folders = connector.list_folders()
        return {"folders": folders}

    def list_accounts(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "list_accounts"})
        if "error" in res:
            from old_rag.storage.settings import get_accounts
            return {"accounts": get_accounts()}
        return res

    def save_account(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "save_account", "account": request.get("account", {})})
        if "error" in res:
            from old_rag.storage.settings import save_account
            acc = save_account(request.get("account", {}))
            return {"success": True, "account": acc}
        return res

    def delete_account(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "delete_account", "account_id": request.get("account_id", "")})
        if "error" in res:
            from old_rag.storage.settings import delete_account
            ok = delete_account(request.get("account_id", ""))
            return {"success": ok}
        return res

    def list_mailbox_folders(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "list_mailbox_folders", "account_id": request.get("account_id", "")})
        if "error" in res:
            acc_id = request.get("account_id", "")
            from old_rag.storage.settings import get_account
            acc = get_account(acc_id) if acc_id else None
            folders = ["Inbox", "Sent Items", "Archive", "Trash"]
            if acc and acc.get("account_type") == "outlook_mapi":
                from old_rag.ingest.outlook import ClassicMAPIConnector
                folders = ClassicMAPIConnector().list_folders()
            elif acc and acc.get("imap_host"):
                from old_rag.ingest.imap import IMAPConnector
                c = IMAPConnector(acc["imap_host"], acc.get("imap_port", 993), acc.get("imap_ssl", True), acc.get("username", ""), acc.get("password", ""))
                t = c.test_connection()
                if t.get("folders"):
                    folders = t["folders"]
            return {"folders": folders}
        return res

    def list_messages(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({
            "action": "list_messages",
            "account_id": request.get("account_id"),
            "folder": request.get("folder"),
            "limit": request.get("limit", 50),
            "offset": request.get("offset", 0),
            "search": request.get("search", ""),
        })
        if "error" in res:
            from old_rag.storage.cas import get_default_app_data_dir
            from old_rag.storage.catalog import CatalogRepository
            app_dir = get_default_app_data_dir()
            cat = CatalogRepository(app_dir / "catalog.db")
            conn = cat._get_connection()

            folder = request.get("folder")
            limit = int(request.get("limit", 50))
            offset = int(request.get("offset", 0))
            search_term = str(request.get("search", "")).strip()

            q = "SELECT doc_id, sender, subject, folder, received_ts_utc FROM documents WHERE is_deleted = 0"
            params = []
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
            return {"messages": rows}
        return res

    def get_message_detail(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "get_message_detail", "doc_id": request.get("doc_id", "")})
        if "error" in res:
            _, cas = self._get_in_process_engine()
            doc_id = request.get("doc_id", "")
            norm_doc = cas.get_text(doc_id)
            if not norm_doc:
                return {"error": "Message not found in archive"}
            doc_dict = norm_doc.model_dump(mode="json")
            doc_dict["has_archived_msg"] = cas.has_msg(doc_id)
            return {"message": doc_dict}
        return res

    def send_email(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({
            "action": "send_email",
            "account_id": request.get("account_id", ""),
            "to": request.get("to", []),
            "cc": request.get("cc", []),
            "bcc": request.get("bcc", []),
            "subject": request.get("subject", ""),
            "body_html": request.get("body_html", ""),
            "body_text": request.get("body_text", ""),
            "attachments": request.get("attachments", []),
        })
        if "error" in res:
            acc_id = request.get("account_id", "")
            from old_rag.storage.settings import get_account
            acc = get_account(acc_id) if acc_id else None
            to_addrs = request.get("to", [])
            cc_addrs = request.get("cc", [])
            bcc_addrs = request.get("bcc", [])
            subject = request.get("subject", "")
            body_html = request.get("body_html", "")
            body_text = request.get("body_text", "")
            attachments = request.get("attachments", [])

            if acc and acc.get("account_type") == "outlook_mapi":
                from old_rag.ingest.outlook import ClassicMAPIConnector
                return ClassicMAPIConnector().send_mail_mapi(to_addrs, cc_addrs, bcc_addrs, subject, body_html, body_text, attachments)
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
                dispatch_res = sender.send_message(
                    from_addr=acc.get("email_address") or acc.get("username", ""),
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    bcc_addrs=bcc_addrs,
                    subject=subject,
                    body_html=body_html,
                    body_text=body_text,
                    attachments=attachments,
                )
                if dispatch_res.get("success") and acc.get("imap_host") and "raw_message" in dispatch_res:
                    from old_rag.ingest.imap import IMAPConnector
                    ic = IMAPConnector(acc["imap_host"], acc.get("imap_port", 993), acc.get("imap_ssl", True), acc.get("username", ""), acc.get("password", ""))
                    ic.append_sent_message(dispatch_res["raw_message"].encode("utf-8"))
                return dispatch_res
            else:
                from old_rag.ingest.outlook import ClassicMAPIConnector
                return ClassicMAPIConnector().send_mail_mapi(to_addrs, cc_addrs, bcc_addrs, subject, body_html, body_text, attachments)
        return res

    def list_calendar_events(self, request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.client.call({"action": "list_calendar_events"})
        if "error" in res:
            from old_rag.ingest.calendar import CalendarManager
            from old_rag.storage.cas import get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            cm = CalendarManager(app_dir / "catalog.db")
            cm.sync_outlook_calendar()
            events = cm.list_events()
            return {"events": [e.model_dump(mode="json") for e in events]}
        return res

    def create_calendar_event(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "create_calendar_event", "event": request.get("event", {})})
        if "error" in res:
            import uuid
            from datetime import datetime
            from old_rag.contracts import CalendarEvent, SourceType
            from old_rag.ingest.calendar import CalendarManager
            from old_rag.storage.cas import get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            cm = CalendarManager(app_dir / "catalog.db")
            evt_dict = request.get("event", {})
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
                from old_rag.ingest.outlook import ClassicMAPIConnector
                ClassicMAPIConnector().create_calendar_event(
                    subject=evt.subject,
                    start_dt=evt.start_ts_utc,
                    end_dt=evt.end_ts_utc,
                    location=evt.location or "",
                    body=evt.body_text or "",
                    all_day=evt.is_all_day,
                )
            except Exception:
                pass
            return {"success": True, "event": evt.model_dump(mode="json")}
        return res

    def scan_head_of_folder(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({
            "action": "scan_head_of_folder",
            "folder_path": request.get("folder_path", ""),
            "recursive": request.get("recursive", True),
        })
        if "error" in res:
            from old_rag.engine.folder_index import HeadOfFolderIndex
            folder_path = request.get("folder_path", "")
            recursive = bool(request.get("recursive", True))
            idx = HeadOfFolderIndex(Path(folder_path))
            return idx.scan_and_index(recursive=recursive)
        return res

    def get_head_of_folder_status(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({"action": "get_head_of_folder_status", "folder_path": request.get("folder_path", "")})
        if "error" in res:
            from old_rag.engine.folder_index import HeadOfFolderIndex
            folder_path = request.get("folder_path", "")
            idx = HeadOfFolderIndex(Path(folder_path))
            return idx.get_status()
        return res

    def get_related_context(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({
            "action": "get_related_context",
            "doc_id": request.get("doc_id"),
            "subject": request.get("subject", ""),
            "text": request.get("text", ""),
            "top_k": request.get("top_k", 6),
        })
        if "error" in res:
            engine, cas = self._get_in_process_engine()
            from old_rag.engine.search import resolve_related_context
            from old_rag.ingest.calendar import CalendarManager
            from old_rag.storage.cas import get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            cm = CalendarManager(app_dir / "catalog.db")
            return resolve_related_context(
                search_engine=engine,
                catalog=self._catalog,
                calendar_manager=cm,
                doc_id=request.get("doc_id"),
                subject=request.get("subject", ""),
                text=request.get("text", ""),
                top_k=int(request.get("top_k", 6)),
            )
        return res

    def open_document(self, request: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.call({
            "action": "open_document",
            "doc_id": request.get("doc_id"),
            "original_path": request.get("original_path"),
        })
        if "error" in res or not res.get("success"):
            _, cas = self._get_in_process_engine()
            from old_rag.engine.search import resolve_open_document
            return resolve_open_document(
                cas=cas,
                catalog=self._catalog,
                doc_id=request.get("doc_id"),
                original_path=request.get("original_path"),
            )
        return res

    def explain_match_tier1(self, request: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"action": "explain_match_tier1", **(request or {})}
        res = self.client.call(payload, timeout_ms=5000)
        if not self._is_ipc_transport_error(res):
            return res
        try:
            engine, cas = self._get_in_process_engine()
            from old_rag.engine.explainer import explain_match_tier1
            from old_rag.ingest.calendar import CalendarManager
            from old_rag.storage.cas import get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            cm = CalendarManager(app_dir / "catalog.db")
            return explain_match_tier1(
                fts_db_path=app_dir / "lexical_fts.db",
                catalog=self._catalog,
                cas=cas,
                calendar_manager=cm,
                rrf_k=float(engine.config.rrf_k),
                payload=request or {},
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    def explain_match_tier2(self, request: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"action": "explain_match_tier2", **(request or {})}
        res = self.client.call(payload, timeout_ms=45000)
        if not self._is_ipc_transport_error(res):
            return res
        try:
            explainer = self._get_explainer()
            req_dict = request or {}
            return explainer.explain_tier2(
                payload=req_dict,
                force_refresh=bool(req_dict.get("force_refresh", False)),
            )
        except Exception as e:
            return {"success": False, "error_code": "INTERNAL_ERROR", "error": str(e)}


def ensure_default_desktop() -> None:
    """Ensure current thread is attached to the user's interactive 'Default' desktop."""
    if sys.platform != "win32":
        return
    try:
        import win32api
        import win32con
        import win32service

        h_default = win32service.OpenDesktop("default", 0, False, win32con.GENERIC_ALL)
        if h_default:
            h_default.SetThreadDesktop()
    except Exception:
        pass


def bring_window_to_front(title: str = "OldRAG Search Companion") -> None:
    """Bring named window to the foreground on the active desktop."""
    if sys.platform != "win32":
        return
    try:
        import win32con
        import win32gui

        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
    except Exception:
        pass


def start_hotkey_listener(toggle_window_func):
    """Background Win32 message loop listening for Ctrl+Alt+Space."""
    if sys.platform != "win32":
        return

    def _loop():
        try:
            ensure_default_desktop()
            import win32con
            import win32gui
            # Register HotKey: Ctrl + Alt + Space (ID: 100)
            MOD_ALT = win32con.MOD_ALT
            MOD_CONTROL = win32con.MOD_CONTROL
            VK_SPACE = win32con.VK_SPACE
            hwnd = None  # thread-specific hotkey
            win32gui.RegisterHotKey(hwnd, 100, MOD_ALT | MOD_CONTROL, VK_SPACE)
            try:
                msg = win32gui.GetMessage(None, 0, 0)
                while msg[0] != 0:
                    if msg[1][1] == win32con.WM_HOTKEY and msg[1][2] == 100:
                        toggle_window_func()
                    msg = win32gui.GetMessage(None, 0, 0)
            finally:
                win32gui.UnregisterHotKey(hwnd, 100)
        except Exception:
            pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def setup_tray_icon(toggle_window_func, exit_func):
    """Run detached pystray system tray icon."""
    try:
        from PIL import Image, ImageDraw
        import pystray

        # Generate 32x32 search icon in memory
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, 20, 20), outline="#339af0", width=3)
        draw.line((18, 18, 28, 28), fill="#339af0", width=4)

        menu = pystray.Menu(
            pystray.MenuItem("Toggle Search (Ctrl+Alt+Space)", toggle_window_func, default=True),
            pystray.MenuItem("Exit", exit_func),
        )
        icon = pystray.Icon("OldRAG", img, "OldRAG Search Companion", menu)
        icon.run_detached()
        return icon
    except Exception:
        return None


def run_ui_app() -> None:
    """Entry point for Process 3 (sovereign-rag ui)."""
    import webview

    ensure_default_desktop()

    static_dir = Path(__file__).parent / "static"
    index_html = (static_dir / "index.html").resolve()
    file_uri = index_html.as_uri()

    x, y = None, None
    if sys.platform == "win32":
        try:
            import win32api
            sw = win32api.GetSystemMetrics(0)
            sh = win32api.GetSystemMetrics(1)
            x = max(50, (sw - 740) // 2)
            y = max(50, (sh - 560) // 2)
        except Exception:
            pass

    api = BridgeAPI()
    window = webview.create_window(
        title="OldRAG Search Companion",
        url=file_uri,
        js_api=api,
        width=740,
        height=560,
        x=x,
        y=y,
        resizable=True,
    )

    icon = None

    def toggle_window():
        try:
            window.show()
            window.restore()
            bring_window_to_front("OldRAG Search Companion")
        except Exception:
            pass

    def exit_app():
        nonlocal icon
        try:
            if icon:
                icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    def on_window_closed():
        nonlocal icon
        try:
            if icon:
                icon.stop()
        except Exception:
            pass

    window.events.closed += on_window_closed

    def on_start():
        import time
        time.sleep(0.3)
        bring_window_to_front("OldRAG Search Companion")

    # Start helper threads
    start_hotkey_listener(toggle_window)
    icon = setup_tray_icon(toggle_window, exit_app)

    # Start on main thread with http_server=False to guarantee zero open network ports!
    webview.start(on_start, http_server=False)
