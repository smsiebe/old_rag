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
        self._search_engine = None

    def _get_in_process_engine(self):
        if self._search_engine is None:
            from old_rag.engine.search import HybridSearchEngine
            from old_rag.storage.cas import CASEngine, get_default_app_data_dir
            app_dir = get_default_app_data_dir()
            self._cas = CASEngine()
            self._search_engine = HybridSearchEngine(fts_db_path=app_dir / "lexical_fts.db")
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
                return {"results": [h.model_dump() for h in hits]}
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
            return {
                "daemon_running": False,
                "vlm_active": False,
                "items_indexed": 0,
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
            from old_rag.engine.indexer import IndexEngine
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
                indexer=IndexEngine(app_dir / "lexical_fts.db"),
            )
            count = coord.reindex_all()
            return {"success": True, "count": count}
        return res


def start_hotkey_listener(toggle_window_func):
    """Background Win32 message loop listening for Ctrl+Alt+Space."""
    if sys.platform != "win32":
        return

    def _loop():
        try:
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

    static_dir = Path(__file__).parent / "static"
    index_html = (static_dir / "index.html").resolve()
    file_uri = index_html.as_uri()

    api = BridgeAPI()
    window = webview.create_window(
        title="OldRAG Search Companion",
        url=file_uri,
        js_api=api,
        width=720,
        height=540,
        resizable=True,
    )

    def toggle_window():
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def exit_app():
        try:
            window.destroy()
        except Exception:
            pass

    # Start helper threads
    start_hotkey_listener(toggle_window)
    setup_tray_icon(toggle_window, exit_app)

    # Start on main thread with http_server=False to guarantee zero open network ports!
    webview.start(http_server=False)
