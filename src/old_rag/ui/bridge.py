"""bridge.py - Sovereign Desktop Bridge & Programmatic GenAI SDK.

Implements OldRAGDesktopBridgeProtocol with zero HTTP servers and zero open TCP sockets:
1. Typed action dispatcher (dispatch / dispatch_json) wrapping IPC and in-process fallback into BridgeResponseEnvelope.
2. Self-contained desktop HTML renderer with path-traversal safety and script closing tag escaping.
3. describe_actions() and get_genai_tool_definitions() exporting OpenAPI/MCP-compatible tool schemas.
4. Programmatic domain methods for Search, Mail, Chat, Calendar, Sync, and Quota governance.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import (
    BridgeErrorCode,
    BridgeResponseEnvelope,
    CalendarEvent,
    ChatAccountConfig,
    DataTypeQuotaConfig,
    ModelConfig,
    SearchQuery,
    SourceType,
    StorageConfig,
    SyncPolicyConfig,
    safe_json_dumps,
    safe_json_loads,
)
from old_rag.pipeline.lifecycle import NamedPipeClient, get_scoped_pipe_name
from old_rag.protocols import OldRAGBridgeProtocol
from old_rag.storage.cas import CASEngine, get_default_app_data_dir
from old_rag.storage.catalog import CatalogRepository


class OldRAGDesktopBridge(OldRAGBridgeProtocol):
    """Pure-Python Desktop Bridge and programmatic GenAI SDK interface (zero HTTP server)."""

    def __init__(
        self,
        pipe_name: Optional[str] = None,
        storage_cfg: Optional[StorageConfig] = None,
        model_cfg: Optional[ModelConfig] = None,
    ):
        self.pipe_name = pipe_name or get_scoped_pipe_name()
        self.client = NamedPipeClient(self.pipe_name)
        self.storage_cfg = storage_cfg or StorageConfig()
        self.model_cfg = model_cfg or ModelConfig()

        self._app_data_dir = (
            Path(self.storage_cfg.app_data_dir)
            if self.storage_cfg.app_data_dir
            else get_default_app_data_dir()
        )

        # Lazy in-process fallback components
        self._cas: Optional[CASEngine] = None
        self._catalog: Optional[CatalogRepository] = None
        self._search_engine = None
        self._explainer = None
        self._chat_registry = None
        self._quota_governor = None
        self._coordinator = None
        self._sync_scheduler = None

    def _get_catalog(self) -> CatalogRepository:
        if self._catalog is None:
            self._catalog = CatalogRepository(self._app_data_dir / "catalog.db")
        return self._catalog

    def _get_cas(self) -> CASEngine:
        if self._cas is None:
            self._cas = CASEngine(self.storage_cfg)
        return self._cas

    def _get_search_engine(self):
        if self._search_engine is None:
            from old_rag.engine.indexer import open_lancedb_table
            from old_rag.engine.search import HybridSearchEngine
            cat = self._get_catalog()
            ldb_table = open_lancedb_table(self._app_data_dir, dim=self.model_cfg.matryoshka_dim)
            self._search_engine = HybridSearchEngine(
                fts_db_path=self._app_data_dir / "lexical_fts.db",
                lancedb_table=ldb_table,
                config=self.model_cfg,
                catalog=cat,
            )
        return self._search_engine

    def _get_chat_registry(self):
        if self._chat_registry is None:
            from old_rag.ingest.chat.registry import ChatPluginRegistry
            self._chat_registry = ChatPluginRegistry()
        return self._chat_registry

    def _get_coordinator(self):
        if self._coordinator is None:
            from old_rag.engine.chunker import ParentAwareChunker
            from old_rag.engine.extractor import DocumentExtractor
            from old_rag.engine.indexer import IndexEngine, open_lancedb_table
            from old_rag.pipeline.coordinator import PipelineCoordinator

            cat = self._get_catalog()
            cas = self._get_cas()
            ldb_table = open_lancedb_table(self._app_data_dir, dim=self.model_cfg.matryoshka_dim)
            indexer = IndexEngine(
                fts_db_path=self._app_data_dir / "lexical_fts.db",
                lancedb_table=ldb_table,
                config=self.model_cfg,
            )
            extractor = DocumentExtractor(catalog=cat)
            chunker = ParentAwareChunker(catalog=cat)
            self._coordinator = PipelineCoordinator(
                cas=cas,
                catalog=cat,
                extractor=extractor,
                chunker=chunker,
                indexer=indexer,
            )
        return self._coordinator

    def _get_quota_governor(self):
        if self._quota_governor is None:
            from old_rag.storage.quota import QuotaGovernor
            coord = self._get_coordinator()
            self._quota_governor = QuotaGovernor(
                catalog=self._get_catalog(),
                cas=self._get_cas(),
                indexer=coord.indexer,
                coordinator=coord,
            )
        return self._quota_governor

    def _get_sync_scheduler(self):
        if self._sync_scheduler is None:
            from old_rag.pipeline.sync_scheduler import RealTimeSyncScheduler
            self._sync_scheduler = RealTimeSyncScheduler(
                coordinator=self._get_coordinator(),
                catalog=self._get_catalog(),
                quota_governor=self._get_quota_governor(),
                chat_registry=self._get_chat_registry(),
            )
        return self._sync_scheduler

    def _is_ipc_error(self, res: Any) -> bool:
        if not isinstance(res, dict):
            return True
        if "error" in res:
            err = str(res.get("error", ""))
            return err.startswith("Named Pipe") or "Pipe not available" in err or "Unknown action" in err
        return False

    def _in_process_fallback(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute action in-process when background daemon is offline."""
        cat = self._get_catalog()
        cas = self._get_cas()

        if action in ("status", "get_status"):
            doc_count = 0
            try:
                row = cat._get_connection().execute("SELECT COUNT(*) FROM documents WHERE is_deleted = 0").fetchone()
                if row:
                    doc_count = row[0]
            except Exception:
                pass
            return {
                "daemon_running": False,
                "vlm_active": False,
                "items_indexed": doc_count,
                "pending_outbox_count": len(cat.get_pending_outbox()),
                "classic_outlook_active": False,
                "on_battery": False,
                "lancedb_count": 0,
                "sync_progress_pct": 100.0,
            }

        elif action in ("search", "query_personal_archive"):
            engine = self._get_search_engine()
            q_dict = payload.get("query", payload)
            q = SearchQuery.model_validate(q_dict)
            results = engine.search(q)
            return {"results": [r.model_dump(mode="json") for r in results]}

        elif action == "open_cached_msg":
            doc_id = payload.get("doc_id", "")
            temp_path = cas.create_temp_view_file(doc_id)
            if temp_path:
                return {"success": True, "temp_view_path": str(temp_path)}
            return {"success": False, "error": "Not found in CAS"}

        elif action == "get_admin_overview":
            from old_rag.storage.settings import get_storage_breakdown, load_settings, validate_storage_location
            settings = load_settings()
            storage_breakdown = get_storage_breakdown(self._app_data_dir)
            privacy_check = validate_storage_location(str(self._app_data_dir))
            doc_count = 0
            try:
                row = cat._get_connection().execute("SELECT COUNT(*) FROM documents WHERE is_deleted = 0").fetchone()
                if row:
                    doc_count = row[0]
            except Exception:
                pass
            return {
                "connections": [],
                "storage": storage_breakdown,
                "privacy": privacy_check,
                "items_indexed": doc_count,
                "chunks_indexed": 0,
                "daemon_running": False,
                "is_sovereign": True,
            }

        elif action == "get_statistics":
            stats = cat.get_statistics()
            stats["total_chunks"] = 0
            return stats

        elif action == "list_accounts":
            from old_rag.storage.settings import get_accounts
            return {"accounts": get_accounts()}

        elif action == "save_account":
            from old_rag.storage.settings import save_account
            acc = save_account(payload.get("account", payload))
            return {"success": True, "account": acc}

        elif action == "delete_account":
            from old_rag.storage.settings import delete_account
            acc_id = payload.get("account_id", "")
            purge_data = bool(payload.get("purge_data", False))
            coord = self._get_coordinator() if purge_data else None
            ok = delete_account(acc_id, purge_data=purge_data, coordinator=coord)
            return {"success": ok}

        elif action == "purge_account_data":
            acc_id = payload.get("account_id", "")
            source_type = payload.get("source_type")
            coord = self._get_coordinator()
            res = coord.purge_account_data(acc_id, source_type=source_type)
            return res

        elif action == "list_calendar_accounts":
            from old_rag.storage.settings import get_calendar_accounts
            return {"accounts": get_calendar_accounts(decrypt=False)}

        elif action == "save_calendar_account":
            from old_rag.storage.settings import save_calendar_account
            acc = save_calendar_account(payload.get("account", payload))
            return {"success": True, "account": acc}

        elif action == "delete_calendar_account":
            from old_rag.storage.settings import delete_calendar_account
            acc_id = payload.get("account_id", "")
            purge_data = bool(payload.get("purge_data", False))
            coord = self._get_coordinator() if purge_data else None
            ok = delete_calendar_account(acc_id, purge_data=purge_data, coordinator=coord)
            return {"success": ok}

        elif action == "save_dashboard_layout":
            from old_rag.storage.settings import save_dashboard_layout
            layout = payload.get("layout", payload)
            saved = save_dashboard_layout(layout)
            return {"success": True, "layout": saved}

        elif action == "get_dashboard_layout":
            from old_rag.storage.settings import get_dashboard_layout
            return {"layout": get_dashboard_layout()}

        elif action in ("list_messages", "list_mail"):
            limit = int(payload.get("limit", 50))
            offset = int(payload.get("offset", 0))
            folder = payload.get("folder")
            search_term = payload.get("search", "").strip()

            conn = cat._get_connection()
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
            return {"messages": [dict(r) for r in cur.fetchall()]}

        elif action in ("get_message_detail", "get_mail_detail"):
            doc_id = payload.get("doc_id", "")
            norm = cas.get_text(doc_id)
            if not norm:
                return {"error": f"Message {doc_id} not found in archive"}
            d = norm.model_dump(mode="json")
            d["has_archived_msg"] = cas.has_msg(doc_id)
            return {"message": d}

        elif action == "list_calendar_events":
            from old_rag.ingest.calendar import CalendarManager
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            events = cm.list_events(
                calendar_id=payload.get("calendar_id"),
                account_id=payload.get("account_id"),
            )
            return {"events": [e.model_dump(mode="json") for e in events[:int(payload.get("limit", 50))]]}

        elif action == "create_calendar_event":
            import uuid
            from old_rag.ingest.calendar import CalendarManager
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            evt_dict = payload.get("event", payload)
            evt_id = evt_dict.get("event_id") or f"evt_{uuid.uuid4().hex[:10]}"
            cal_id = evt_dict.get("calendar_id") or evt_dict.get("account_id") or "default"
            cal_color = evt_dict.get("calendar_color") or evt_dict.get("color") or "#339af0"
            source_sys = evt_dict.get("source_system") or "Outlook"
            evt = CalendarEvent(
                event_id=evt_id,
                account_id=evt_dict.get("account_id"),
                calendar_id=cal_id,
                calendar_color=cal_color,
                source_system=source_sys,
                source_type=SourceType.CALENDAR,
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
            return {"success": True, "event": evt.model_dump(mode="json")}

        elif action == "import_ics":
            from old_rag.ingest.calendar import CalendarManager, parse_ics_content
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            events = parse_ics_content(payload.get("ics_content", ""))
            for evt in events:
                cm.upsert_event(evt)
            return {"success": True, "count": len(events)}

        elif action == "export_ics":
            from old_rag.ingest.calendar import CalendarManager, export_events_to_ics
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            events = cm.list_events()
            event_ids = payload.get("event_ids")
            if event_ids:
                events = [e for e in events if e.event_id in event_ids]
            ics_text = export_events_to_ics(events)
            return {"success": True, "ics_content": ics_text}

        elif action == "get_related_context":
            from old_rag.engine.search import resolve_related_context
            from old_rag.ingest.calendar import CalendarManager
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            return resolve_related_context(
                search_engine=self._get_search_engine(),
                catalog=cat,
                calendar_manager=cm,
                doc_id=payload.get("doc_id"),
                subject=payload.get("subject", ""),
                text=payload.get("text", ""),
                top_k=int(payload.get("top_k", 6)),
            )

        elif action == "open_document":
            from old_rag.engine.search import resolve_open_document
            return resolve_open_document(
                cas=cas,
                catalog=cat,
                doc_id=payload.get("doc_id"),
                original_path=payload.get("original_path"),
            )

        elif action == "record_access":
            doc_id = payload.get("doc_id")
            if doc_id:
                cat.record_document_access(doc_id)
                return {"success": True, "doc_id": doc_id}
            return {"error": "doc_id required"}

        elif action == "explain_match_tier1":
            from old_rag.engine.explainer import explain_match_tier1
            from old_rag.ingest.calendar import CalendarManager
            cm = CalendarManager(self._app_data_dir / "catalog.db")
            return explain_match_tier1(
                fts_db_path=self._app_data_dir / "lexical_fts.db",
                catalog=cat,
                cas=cas,
                calendar_manager=cm,
                rrf_k=float(self.model_cfg.rrf_k),
                payload=payload,
            )

        elif action == "explain_match_tier2":
            if self._explainer is None:
                from old_rag.engine.explainer import MatchExplainer
                self._explainer = MatchExplainer()
            return self._explainer.explain_tier2(
                payload=payload,
                force_refresh=bool(payload.get("force_refresh", False)),
            )

        elif action == "list_chat_plugins":
            registry = self._get_chat_registry()
            return {"plugins": [p.model_dump(mode="json") for p in registry.list_plugins()]}

        elif action == "list_chat_accounts":
            from old_rag.storage.settings import get_chat_accounts
            return {"accounts": get_chat_accounts(decrypt=False)}

        elif action == "save_chat_account":
            from old_rag.storage.settings import save_chat_account
            acc = save_chat_account(payload.get("account", payload))
            return {"success": True, "account": acc}

        elif action == "delete_chat_account":
            from old_rag.storage.settings import delete_chat_account
            acc_id = payload.get("account_id", "")
            purge_data = bool(payload.get("purge_data", False))
            coord = self._get_coordinator() if purge_data else None
            ok = delete_chat_account(acc_id, purge_data=purge_data, coordinator=coord)
            return {"success": ok}

        elif action == "test_chat_connection":
            registry = self._get_chat_registry()
            acc_cfg = ChatAccountConfig.model_validate(payload.get("account", payload))
            adapter = registry.create_adapter(acc_cfg)
            ok = adapter.test_connection()
            return {"success": ok}

        elif action == "list_chat_rooms":
            rooms = cat.list_chat_rooms(account_id=payload.get("account_id"))
            return {"rooms": [r.model_dump(mode="json") for r in rooms]}

        elif action == "update_room_subscription":
            room_id = payload.get("room_id")
            subscribed = bool(payload.get("subscribed", True))
            conn = cat._get_connection()
            conn.execute("UPDATE chat_rooms SET subscribed = ? WHERE room_id = ?", (1 if subscribed else 0, room_id))
            conn.commit()
            return {"success": True, "room_id": room_id, "subscribed": subscribed}

        elif action == "list_chat_messages":
            since_str = payload.get("since_utc")
            since_dt = datetime.fromisoformat(since_str) if since_str else None
            msgs = cat.list_chat_messages(
                room_id=payload.get("room_id", ""),
                since_utc=since_dt,
                limit=int(payload.get("limit", 50)),
            )
            return {"messages": [m.model_dump(mode="json") for m in msgs]}

        elif action == "send_chat_message":
            registry = self._get_chat_registry()
            from old_rag.storage.settings import get_chat_accounts
            accounts = get_chat_accounts(decrypt=True)
            acc_id = payload.get("account_id", "")
            target_acc = next((a for a in accounts if a.get("id") == acc_id), None)
            if not target_acc:
                return {"success": False, "error": f"Chat account {acc_id} not found"}
            acc_cfg = ChatAccountConfig.model_validate(target_acc)
            adapter = registry.create_adapter(acc_cfg)
            msg_item = adapter.send_message(
                room_id=payload.get("room_id", ""),
                body_text=payload.get("body_text", ""),
                thread_root_id=payload.get("thread_root_id"),
            )
            cat.upsert_chat_messages([msg_item])
            return {"success": True, "message": msg_item.model_dump(mode="json")}

        elif action == "get_sync_status":
            cursors = cat.get_sync_cursors()
            cursor_list = [c.model_dump(mode="json") for c in cursors]
            return {
                "daemon_running": False,
                "cursors": cursor_list,
                "statuses": cursor_list,
                "last_sync_utc": datetime.now(timezone.utc).isoformat(),
            }

        elif action == "update_sync_policy":
            from old_rag.storage.settings import save_sync_policy
            pol = SyncPolicyConfig.model_validate(payload.get("policy", payload))
            save_sync_policy(pol)
            return {"success": True, "policy": pol.model_dump(mode="json")}

        elif action == "trigger_sync":
            sched = self._get_sync_scheduler()
            st_val = payload.get("source_type")
            st = None
            if st_val:
                try:
                    st = SourceType(st_val.value if hasattr(st_val, "value") else str(st_val).upper())
                except Exception:
                    pass
            source_key = payload.get("source_key")
            return sched.trigger_sync(source_type=st, source_key=source_key)

        elif action == "get_quota_status":
            gov = self._get_quota_governor()
            st_val = payload.get("source_type")
            if st_val:
                st = SourceType(st_val.value if hasattr(st_val, "value") else str(st_val).upper())
                status = gov.get_status(st)
                return status.model_dump(mode="json") if hasattr(status, "model_dump") else status
            statuses = [gov.get_status(st).model_dump(mode="json") for st in SourceType]
            return {"statuses": statuses}

        elif action == "update_quota_policy":
            from old_rag.storage.settings import save_quota_config
            cfg = DataTypeQuotaConfig.model_validate(payload.get("config", payload))
            save_quota_config(cfg)
            return {"success": True, "config": cfg.model_dump(mode="json")}

        elif action == "preview_quota_eviction":
            gov = self._get_quota_governor()
            st_val = payload.get("source_type", "EMAIL")
            st = SourceType(st_val.value if hasattr(st_val, "value") else str(st_val).upper())
            res = gov.enforce_quota(st, dry_run=True)
            return res.model_dump(mode="json")

        elif action == "run_quota_purge":
            gov = self._get_quota_governor()
            st_val = payload.get("source_type", "EMAIL")
            st = SourceType(st_val.value if hasattr(st_val, "value") else str(st_val).upper())
            dry_run = bool(payload.get("dry_run", False))
            res = gov.enforce_quota(st, dry_run=dry_run)
            return res.model_dump(mode="json")

        elif action == "get_settings":
            from old_rag.storage.settings import load_settings
            return {"settings": load_settings()}

        elif action == "save_settings":
            from old_rag.storage.settings import save_settings, validate_storage_location
            new_s = payload.get("settings", payload)
            save_settings(new_s)
            val = validate_storage_location(new_s.get("storage_location", ""))
            return {"success": True, "privacy": val}

        elif action == "poll_events":
            return {"events": [], "latest_seq": int(payload.get("since_seq", 0))}

        return {"error": f"Unknown action {action}"}

    def dispatch(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch a named bridge action with a payload dict, returning BridgeResponseEnvelope."""
        from old_rag.logger import get_logger
        logger = get_logger("ui.bridge")
        req_payload = payload or {}
        ipc_req = {"action": action, **req_payload}

        res: Any = None
        try:
            res = self.client.call(ipc_req, timeout_ms=5000)
        except Exception:
            res = None

        if self._is_ipc_error(res):
            try:
                res = self._in_process_fallback(action, req_payload)
            except Exception as e:
                res = {"error": str(e)}

        if isinstance(res, dict) and "error" in res and res.get("error"):
            err_msg = str(res["error"])
            logger.warning("Action '%s' error: %s", action, err_msg)
            envelope = BridgeResponseEnvelope(
                ok=False,
                status=404 if "not found" in err_msg.lower() else 500,
                error=err_msg,
                error_code=BridgeErrorCode.NOT_FOUND if "not found" in err_msg.lower() else BridgeErrorCode.INTERNAL_ERROR,
                data=None,
            )
        else:
            logger.debug("Action '%s' completed successfully", action)
            envelope = BridgeResponseEnvelope(
                ok=True,
                status=200,
                data=res,
            )
        return envelope.model_dump(mode="json")

    def dispatch_json(self, request_json: str) -> str:
        """JSON-string serialization wrapper around dispatch() for pywebview/IPC."""
        try:
            req = safe_json_loads(request_json)
            action = req.get("action", "")
            payload = req.get("payload") or {k: v for k, v in req.items() if k != "action"}
            res = self.dispatch(action, payload)
            return safe_json_dumps(res)
        except Exception as e:
            env = BridgeResponseEnvelope(
                ok=False,
                status=400,
                error=str(e),
                error_code=BridgeErrorCode.VALIDATION_ERROR,
            )
            return safe_json_dumps(env.model_dump(mode="json"))

    def render_desktop_html(self, inline_assets: bool = True) -> str:
        """Render self-contained single-page desktop workspace with zero external CDNs."""
        static_dir = (Path(__file__).parent / "static").resolve()
        index_html_path = static_dir / "index.html"

        if not index_html_path.exists():
            return "<html><body><h1>OldRAG Desktop: UI assets missing</h1></body></html>"

        html_content = index_html_path.read_text(encoding="utf-8")

        if not inline_assets:
            return html_content

        # Inject window.__OLDRAG_DESKTOP_MODE__ = true;
        desktop_flag = "<script>window.__OLDRAG_DESKTOP_MODE__ = true;</script>\n"
        if "<head>" in html_content:
            html_content = html_content.replace("<head>", f"<head>\n  {desktop_flag}", 1)
        elif "<head " in html_content:
            html_content = re.sub(r"(<head[^>]*>)", rf"\1\n  {desktop_flag}", html_content, count=1, flags=re.IGNORECASE)
        else:
            html_content = desktop_flag + html_content

        # 1. Inline CSS stylesheets with path-traversal safety check
        def _replace_css(match: re.Match) -> str:
            rel_href = match.group(1).strip()
            clean_href = rel_href
            if clean_href.startswith("/static/"):
                clean_href = clean_href[len("/static/"):]
            elif clean_href.startswith("static/"):
                clean_href = clean_href[len("static/"):]
            target_path = (static_dir / clean_href).resolve()
            if not target_path.is_relative_to(static_dir) or not target_path.exists():
                return ""
            css_text = target_path.read_text(encoding="utf-8")
            safe_css = re.sub(r"</style", r"<\\/style", css_text, flags=re.IGNORECASE)
            return f"<style>\n/* Inlined from {rel_href} */\n{safe_css}\n</style>"

        html_content = re.sub(
            r'<link\s+[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
            _replace_css,
            html_content,
            flags=re.IGNORECASE,
        )

        # 2. Inline JavaScript files with script escaping and path-traversal safety
        def _replace_js(match: re.Match) -> str:
            rel_src = match.group(1).strip()
            clean_src = rel_src
            if clean_src.startswith("/static/"):
                clean_src = clean_src[len("/static/"):]
            elif clean_src.startswith("static/"):
                clean_src = clean_src[len("static/"):]
            target_path = (static_dir / clean_src).resolve()
            if not target_path.is_relative_to(static_dir) or not target_path.exists():
                return ""
            js_text = target_path.read_text(encoding="utf-8")
            # Sanitize </script sequences to avoid early HTML closing tag attacks
            safe_js = re.sub(r"</script", r"<\\/script", js_text, flags=re.IGNORECASE)
            return f"<script>\n// Inlined from {rel_src}\n{safe_js}\n</script>"

        html_content = re.sub(
            r'<script\s+[^>]*src=["\']([^"\']+)["\'][^>]*>\s*</script>',
            _replace_js,
            html_content,
            flags=re.IGNORECASE,
        )

        return html_content

    def describe_actions(self) -> Dict[str, Any]:
        """Return schema descriptions of all bridge actions from bridge_schema.json."""
        schema_path = Path(__file__).parent.parent / "bridge_schema.json"
        if not schema_path.exists():
            return {}
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        return {
            "title": schema.get("title", "OldRAGBridgeSchema"),
            "actions": schema.get("actions", {}),
            "definitions": schema.get("definitions", {}),
        }

    def get_genai_tool_definitions(self) -> List[Dict[str, Any]]:
        """Export OpenAI/Anthropic/MCP tool calling function definitions for GenAI agents."""
        meta = self.describe_actions()
        actions = meta.get("actions", {})
        defs = meta.get("definitions", {})

        tools: List[Dict[str, Any]] = []
        for action_name, action_info in actions.items():
            req_ref = action_info.get("request")
            param_schema = {"type": "object", "properties": {}}
            desc = f"Execute OldRAG action: {action_name}"

            if req_ref and req_ref.startswith("#/definitions/"):
                def_key = req_ref.split("/")[-1]
                target_def = defs.get(def_key, {})
                param_schema = {
                    "type": "object",
                    "properties": target_def.get("properties", {}),
                    "required": target_def.get("required", []),
                }
                desc = target_def.get("description", desc)

            tools.append({
                "type": "function",
                "function": {
                    "name": action_name,
                    "description": desc,
                    "parameters": param_schema,
                },
            })
        return tools

    # --- Programmatic Domain Methods ---

    def search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("search", payload)

    def get_status(self) -> Dict[str, Any]:
        return self.dispatch("get_status")

    def open_cached_msg(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("open_cached_msg", payload)

    def get_admin_overview(self) -> Dict[str, Any]:
        return self.dispatch("get_admin_overview")

    def get_statistics(self) -> Dict[str, Any]:
        return self.dispatch("get_statistics")

    def list_accounts(self) -> Dict[str, Any]:
        return self.dispatch("list_accounts")

    def save_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("save_account", payload)

    def delete_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("delete_account", payload)

    def send_mail(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("send_mail", payload)

    def list_mailbox_folders(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("list_mailbox_folders", payload)

    def list_mail(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("list_mail", payload)

    def get_mail_detail(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("get_mail_detail", payload)

    def list_calendar_events(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("list_calendar_events", payload)

    def create_calendar_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("create_calendar_event", payload)

    def import_ics(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("import_ics", payload)

    def export_ics(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("export_ics", payload)

    def get_related_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("get_related_context", payload)

    def open_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("open_document", payload)

    def record_access(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("record_access", payload)

    def explain_match_tier1(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("explain_match_tier1", payload)

    def explain_match_tier2(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("explain_match_tier2", payload)

    def list_chat_plugins(self) -> Dict[str, Any]:
        return self.dispatch("list_chat_plugins")

    def list_chat_accounts(self) -> Dict[str, Any]:
        return self.dispatch("list_chat_accounts")

    def save_chat_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("save_chat_account", payload)

    def delete_chat_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("delete_chat_account", payload)

    def test_chat_connection(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("test_chat_connection", payload)

    def list_chat_rooms(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("list_chat_rooms", payload)

    def update_room_subscription(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("update_room_subscription", payload)

    def list_chat_messages(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("list_chat_messages", payload)

    def send_chat_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("send_chat_message", payload)

    def get_sync_status(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("get_sync_status", payload)

    def update_sync_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("update_sync_policy", payload)

    def trigger_sync(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("trigger_sync", payload)

    def get_quota_status(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.dispatch("get_quota_status", payload)

    def update_quota_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("update_quota_policy", payload)

    def preview_quota_eviction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("preview_quota_eviction", payload)

    def run_quota_purge(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("run_quota_purge", payload)

    def get_settings(self) -> Dict[str, Any]:
        return self.dispatch("get_settings")

    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("save_settings", payload)

    def scan_head_of_folder(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("scan_head_of_folder", payload)

    def get_head_of_folder_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.dispatch("get_head_of_folder_status", payload)

    def poll_events(self, since_seq: int = 0) -> Dict[str, Any]:
        return self.dispatch("poll_events", {"since_seq": since_seq})


# Programmatic Client alias matching Black Framework guidelines
OldRAGClient = OldRAGDesktopBridge
