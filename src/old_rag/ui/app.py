"""app.py - pywebview Companion Overlay & Zero-Socket Native Bridge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from old_rag.pipeline.lifecycle import get_scoped_pipe_name


class BridgeAPI:
    """Exposes window.pywebview.api methods to Javascript over local Named Pipe IPC."""

    def __init__(self, pipe_name: Optional[str] = None):
        self.pipe_name = pipe_name or get_scoped_pipe_name()

    def _call_daemon(self, action: str, **kwargs) -> Dict[str, Any]:
        """Dispatch IPC call to Daemon process over Named Pipe."""
        payload = {"action": action, **kwargs}
        req_bytes = (json.dumps(payload) + "\n").encode("utf-8")

        # Open named pipe as client
        try:
            with open(self.pipe_name, "r+b", buffering=0) as pipe:
                pipe.write(req_bytes)
                line = pipe.readline()
                return json.loads(line.decode("utf-8"))
        except Exception as e:
            return {"error": f"Failed to connect to OldRAG Daemon: {str(e)}"}

    def search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._call_daemon("search", query=request)

    def get_status(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call_daemon("status")

    def open_cached_msg(self, request: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = request.get("doc_id", "")
        return self._call_daemon("open_cached_msg", doc_id=doc_id)

    def reindex(self, _dummy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call_daemon("reindex")


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
    # Start on main thread with http_server=False to guarantee zero open network ports!
    webview.start(http_server=False)
