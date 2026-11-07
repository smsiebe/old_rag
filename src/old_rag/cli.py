"""cli.py - Console entrypoints for old_rag commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from old_rag.contracts import StorageConfig
from old_rag.engine.vlm_worker import run_vlm_worker_stdio_loop
from old_rag.ingest.outlook import is_new_outlook_active, probe_object_model_guard
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.pipeline.lifecycle import get_scoped_pipe_name
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


def cmd_vlm_worker(args: argparse.Namespace) -> None:
    """Run VLM worker subprocess loop (Process 2)."""
    run_vlm_worker_stdio_loop()


def cmd_ui(args: argparse.Namespace) -> None:
    """Run pywebview companion overlay (Process 3)."""
    from old_rag.ui.app import run_ui_app
    run_ui_app()


def cmd_status(args: argparse.Namespace) -> None:
    """Check running daemon status over named pipe."""
    from old_rag.ui.app import BridgeAPI
    api = BridgeAPI()
    res = api.get_status()
    print(res)


def cmd_reindex(args: argparse.Namespace) -> None:
    """Trigger offline re-index from CAS Tier 1."""
    from old_rag.ui.app import BridgeAPI
    api = BridgeAPI()
    res = api.reindex()
    print(res)


def main() -> None:
    parser = argparse.ArgumentParser(prog="old-rag", description="Sovereign local Outlook & file RAG engine")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Initialize keys, storage, and verify MAPI")
    subparsers.add_parser("daemon", help="Run background coordinator daemon")
    subparsers.add_parser("vlm-worker", help="Run VLM worker subprocess (internal)")
    subparsers.add_parser("ui", help="Launch native UI overlay")
    subparsers.add_parser("status", help="Query daemon status")
    subparsers.add_parser("reindex", help="Trigger offline re-indexing from text CAS")

    exp_parser = subparsers.add_parser("export-key", help="Export encrypted DEK backup")
    exp_parser.add_argument("--output", "-o", help="Target output file path")

    args = parser.parse_args()
    if args.command == "init":
        cmd_init(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    elif args.command == "vlm-worker":
        cmd_vlm_worker(args)
    elif args.command == "ui":
        cmd_ui(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "export-key":
        cmd_export_key(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
