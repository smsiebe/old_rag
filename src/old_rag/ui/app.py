"""app.py - pywebview Companion Overlay & Zero-Socket Native Bridge."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from old_rag.pipeline.lifecycle import NamedPipeClient, get_scoped_pipe_name


class BridgeAPI:
    """Exposes window.pywebview.api methods to Javascript over local Named Pipe IPC."""

    def __init__(self, pipe_name: Optional[str] = None):
        self.client = NamedPipeClient(pipe_name)

    def search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.call({"action": "search", "query": request})

    def get_status(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.client.call({"action": "status"})

    def open_cached_msg(self, request: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = request.get("doc_id", "")
        return self.client.call({"action": "open_cached_msg", "doc_id": doc_id})

    def reindex(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.client.call({"action": "reindex"})


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
