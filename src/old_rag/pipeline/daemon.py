"""daemon.py - Multi-Process Daemon Orchestrator & VLM Subprocess Supervisor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from old_rag.contracts import ModelConfig, StorageConfig
from old_rag.engine.chunker import ParentAwareChunker
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
        self.indexer = IndexEngine(self.app_data_dir / "lexical_fts.db", config=self.model_cfg)
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
            config=self.model_cfg,
        )

        self.pipe_server = NamedPipeServer(get_scoped_pipe_name(), self.handle_client_request)
        self.vlm_process: Optional[subprocess.Popen] = None
        self._running = False
        self.watcher: Optional[DirectoryWatcher] = None
        self._mapi_connector = ClassicMAPIConnector(allow_background_launch=False)

    def start_vlm_subprocess(self) -> None:
        """Spawn isolated Process 2 at BELOW_NORMAL_PRIORITY_CLASS."""
        creation_flags = 0
        if sys.platform == "win32":
            import psutil
            creation_flags = psutil.BELOW_NORMAL_PRIORITY_CLASS

        cmd = [sys.executable, "-m", "old_rag.cli", "vlm-worker"]
        try:
            self.vlm_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except Exception:
            self.vlm_process = None

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
        """Handle JSON IPC request from UI or CLI."""
        try:
            req = json.loads(raw_req)
            action = req.get("action")
            if action == "status":
                res = {
                    "daemon_running": True,
                    "vlm_active": self.vlm_process is not None and self.vlm_process.poll() is None,
                    "items_indexed": 0,
                    "pending_outbox_count": len(self.catalog.get_pending_outbox()),
                    "classic_outlook_active": not is_new_outlook_active(),
                    "on_battery": self.is_on_battery(),
                }
                return json.dumps(res)
            elif action == "reindex":
                count = self.coordinator.reindex_all()
                return json.dumps({"success": True, "count": count})
            elif action == "purge":
                orphans = self.catalog.prune_orphans(purge_source_deleted=True)
                for h in orphans:
                    self.cas.delete_blob(h)
                return json.dumps({"success": True, "pruned_count": len(orphans)})
            elif action == "search":
                from old_rag.contracts import SearchQuery
                q = SearchQuery.model_validate(req.get("query", {}))
                results = self.search_engine.search(q)
                return json.dumps({"results": [r.model_dump() for r in results]})
            elif action == "open_cached_msg":
                doc_id = req.get("doc_id")
                temp_path = self.cas.create_temp_view_file(doc_id)
                if temp_path:
                    return json.dumps({"success": True, "temp_view_path": str(temp_path)})
                return json.dumps({"success": False, "error": "Not found in CAS"})
            return json.dumps({"error": f"Unknown action {action}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

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

        # 3. Start VLM subprocess
        self.start_vlm_subprocess()

        # 4. Start Named Pipe IPC Server
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
        if self.vlm_process and self.vlm_process.poll() is None:
            self.vlm_process.terminate()
        self.cas.sweep_tmp()
        self.mutex.release()
