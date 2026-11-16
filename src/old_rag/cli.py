"""cli.py - Console entrypoints for old_rag commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from old_rag.contracts import StorageConfig
from old_rag.ingest.outlook import is_new_outlook_active, probe_object_model_guard
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.pipeline.lifecycle import NamedPipeClient, get_scoped_pipe_name
from old_rag.storage.cas import CASEngine, KeyManager, get_default_app_data_dir


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize app directories, generate DPAPI DEK, and probe MAPI guard."""
    app_dir = get_default_app_data_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    print(f"Initializing OldRAG at {app_dir}...")

    # 1. Initialize master key
    km = KeyManager(app_dir / "master_key.dpapi")
    key = km.get_master_key()
    print("Master Data Encryption Key (DEK) initialized with Windows DPAPI.")

    # 2. Outlook probe
    if is_new_outlook_active():
        print("Note: New Outlook (olk.exe) detected. MAPI COM requires Classic Outlook.")
    else:
        print("Probing Classic Outlook MAPI Object Model Guard...")
        guard_ok = probe_object_model_guard(timeout_sec=2.0)
        if guard_ok:
            print("Classic Outlook MAPI is available and unprompted.")
        else:
            print("Warning: Outlook Object Model Guard triggered or Outlook not running.")

    print("Initialization complete.")


def cmd_export_key(args: argparse.Namespace) -> None:
    """Export master key wrapped by passphrase via Scrypt."""
    app_dir = get_default_app_data_dir()
    km = KeyManager(app_dir / "master_key.dpapi")
    passphrase = input("Enter passphrase for key backup: ")
    if not passphrase:
        print("Passphrase cannot be empty.")
        sys.exit(1)
    backup_data = km.export_key(passphrase)
    out_file = Path(args.output) if args.output else Path("old_rag_key_backup.json")
    out_file.write_bytes(backup_data)
    print(f"Key backup written to {out_file.resolve()}. Keep this safe and off cloud sync!")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Run core background daemon process (Process 1)."""
    daemon = DaemonProcess()
    daemon.run()


def cmd_ui(args: argparse.Namespace) -> None:
    """Run pywebview companion overlay (Process 3)."""
    from old_rag.ui.app import run_ui_app
    run_ui_app()


def cmd_status(args: argparse.Namespace) -> None:
    """Query running daemon status over named pipe."""
    client = NamedPipeClient()
    res = client.call({"action": "status"})
    print(res)


def cmd_reindex(args: argparse.Namespace) -> None:
    """Trigger offline re-index from CAS Tier 1."""
    client = NamedPipeClient()
    res = client.call({"action": "reindex"})
    print(res)


def cmd_purge(args: argparse.Namespace) -> None:
    """Purge zero-ref blobs and dead-letter records."""
    client = NamedPipeClient()
    res = client.call({"action": "purge"})
    print(res)


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest local files/folders (--path) or Outlook emails (--days/--folder)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from datetime import datetime, timedelta, timezone
    from old_rag.contracts import ModelConfig
    from old_rag.storage.cas import CASEngine, get_default_app_data_dir
    from old_rag.storage.catalog import CatalogRepository
    from old_rag.engine.extractor import DocumentExtractor
    from old_rag.engine.chunker import ParentAwareChunker
    from old_rag.engine.indexer import IndexEngine, open_lancedb_table
    from old_rag.pipeline.coordinator import PipelineCoordinator

    app_dir = get_default_app_data_dir()
    model_cfg = ModelConfig()
    cas = CASEngine()
    catalog = CatalogRepository(app_dir / "catalog.db")
    extractor = DocumentExtractor(catalog=catalog)
    chunker = ParentAwareChunker(catalog=catalog)
    lancedb_table = open_lancedb_table(app_dir, dim=model_cfg.matryoshka_dim)
    indexer = IndexEngine(app_dir / "lexical_fts.db", lancedb_table=lancedb_table, config=model_cfg)

    coord = PipelineCoordinator(
        cas=cas,
        catalog=catalog,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    target_path = getattr(args, "path", None)
    if target_path:
        from old_rag.ingest.watcher import ingest_filesystem_path

        stats = ingest_filesystem_path(
            Path(target_path),
            coord,
            recursive=not getattr(args, "no_recursive", False),
        )
        print(
            f"Ingested {stats['ingested']} files "
            f"(skipped {stats['skipped']}, errors {stats['errors']}) from {Path(target_path).resolve()}"
        )
        return

    from old_rag.ingest.outlook import ClassicMAPIConnector

    days = args.days or 30
    since = datetime.now(timezone.utc) - timedelta(days=days)
    print(f"Connecting to Classic Outlook MAPI to ingest emails since {since.strftime('%Y-%m-%d %H:%M:%S')} UTC ({days} days)...")

    connector = ClassicMAPIConnector(allow_background_launch=False)
    folder_list = [args.folder] if args.folder else None

    count = 0
    try:
        for payload in connector.poll_messages(folder_names=folder_list, as_of_date_utc=since):
            count += 1
            subj = (payload.subject or "(No subject)")[:50]
            print(f"[{count}] [{payload.folder}] {payload.sender}: {subj}")
            coord.ingest_payload(payload)

        print(f"\nSuccessfully ingested {count} emails into OldRAG!")
        print(f"CAS Archive: {cas.archive_dir}")
        print(f"Index: {app_dir / 'lexical_fts.db'}")
    except Exception as e:
        print(f"\nIngestion error: {e}")


def cmd_mcp(args: argparse.Namespace) -> None:
    """Run FastMCP server on stdio."""
    from old_rag.mcp_server import main as run_mcp
    run_mcp()


def main() -> None:
    from old_rag.logger import setup_logging
    setup_logging(to_console=False)
    parser = argparse.ArgumentParser(prog="old-rag", description="Sovereign local Outlook & file RAG engine")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Initialize keys, storage, and verify MAPI")
    subparsers.add_parser("daemon", help="Run background coordinator daemon")
    subparsers.add_parser("ui", help="Launch native UI overlay")
    subparsers.add_parser("status", help="Query daemon status")
    subparsers.add_parser("reindex", help="Trigger offline re-indexing from text CAS")
    subparsers.add_parser("purge", help="Purge orphaned blobs from storage")

    ing_parser = subparsers.add_parser("ingest", help="Ingest local files/folders (--path) or Outlook emails")
    ing_parser.add_argument("--path", "-p", type=str, default=None, help="Local file or folder path to ingest")
    ing_parser.add_argument("--no-recursive", action="store_true", help="Do not recurse into subdirectories when --path is a folder")
    ing_parser.add_argument("--days", "-d", type=int, default=30, help="Number of past days to ingest (default: 30)")
    ing_parser.add_argument("--folder", "-f", type=str, default=None, help="Specific folder to ingest")

    exp_parser = subparsers.add_parser("export-key", help="Export encrypted DEK backup")
    exp_parser.add_argument("--output", "-o", help="Target output file path")

    mcp_parser = subparsers.add_parser("mcp", help="Run FastMCP server on stdio for AuraRouter / AI integration")

    args = parser.parse_args()
    if args.command == "init":
        cmd_init(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    elif args.command == "ui":
        cmd_ui(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "purge":
        cmd_purge(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "export-key":
        cmd_export_key(args)
    elif args.command == "mcp":
        cmd_mcp(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
