"""settings.py - Persistent Configuration, Storage Location Auditing & Disk Usage Metrics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.storage.cas import get_default_app_data_dir


DEFAULT_SETTINGS: Dict[str, Any] = {
    "storage_location": "",
    "accounts": [
        {
            "id": "default_outlook",
            "name": "Classic Outlook (MAPI)",
            "account_type": "outlook_mapi",
            "email_address": "Default Outlook Profile",
            "folders": ["ALL"],
            "date_range": "1yr",
            "enabled": True,
        }
    ],
    "connectors": {
        "outlook": {
            "enabled": True,
            "folders": ["ALL"],
            "date_range": "1yr",
        },
        "exchange_owa": {
            "enabled": False,
            "server_url": "",
            "username": "",
            "folders": ["ALL"],
            "date_range": "1yr",
        },
        "imap": {
            "enabled": False,
            "provider": "gmail",
            "host": "imap.gmail.com",
            "port": 993,
            "ssl": True,
            "username": "",
            "password": "",
            "folders": ["ALL"],
            "date_range": "1yr",
        },
        "filesystem": {
            "enabled": True,
            "paths": [],
            "recursive": True,
        },
    },
    "aurarouter": {
        "enabled": True,
        "mode": "auto",        # "auto" | "mcp_http" | "embedded"
        "endpoint_url": "",    # Optional HTTP MCP endpoint, e.g. "http://127.0.0.1:8000"
        "config_path": "",     # Optional explicit path to auraconfig.yaml
        "role": "assist",      # Preferred role; fallback chain ["assist", "summarization", "reasoning", "coding"]
        "local_only": True,    # Enforce sovereign local inference
        "timeout_sec": 30.0,
    },
}


def get_settings_file_path() -> Path:
    """Return path to settings.json in the default application data directory."""
    return get_default_app_data_dir() / "settings.json"


def load_settings() -> Dict[str, Any]:
    """Load settings from settings.json or return default configuration."""
    path = get_settings_file_path()
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        merged = json.loads(json.dumps(DEFAULT_SETTINGS))
        if "storage_location" in data:
            merged["storage_location"] = data["storage_location"]
        if "accounts" in data and isinstance(data["accounts"], list):
            merged["accounts"] = data["accounts"]
        elif "connectors" in data and isinstance(data["connectors"], dict):
            # Migrate legacy single-account connectors into accounts array
            migrated_accounts = []
            out_cfg = data["connectors"].get("outlook", {})
            if out_cfg.get("enabled", True):
                migrated_accounts.append({
                    "id": "default_outlook",
                    "name": "Classic Outlook (MAPI)",
                    "account_type": "outlook_mapi",
                    "email_address": "Default Outlook Profile",
                    "folders": out_cfg.get("folders", ["ALL"]),
                    "date_range": out_cfg.get("date_range", "1yr"),
                    "enabled": True,
                })
            imap_cfg = data["connectors"].get("imap", {})
            if imap_cfg.get("username"):
                migrated_accounts.append({
                    "id": "default_imap",
                    "name": f"IMAP ({imap_cfg.get('provider', 'gmail').capitalize()})",
                    "account_type": "imap_smtp",
                    "email_address": imap_cfg.get("username", ""),
                    "imap_host": imap_cfg.get("host", "imap.gmail.com"),
                    "imap_port": imap_cfg.get("port", 993),
                    "imap_ssl": imap_cfg.get("ssl", True),
                    "smtp_host": imap_cfg.get("host", "smtp.gmail.com").replace("imap", "smtp"),
                    "smtp_port": 587,
                    "smtp_ssl": False,
                    "smtp_starttls": True,
                    "username": imap_cfg.get("username", ""),
                    "password": imap_cfg.get("password", ""),
                    "folders": imap_cfg.get("folders", ["ALL"]),
                    "date_range": imap_cfg.get("date_range", "1yr"),
                    "enabled": bool(imap_cfg.get("enabled", False)),
                })
            if migrated_accounts:
                merged["accounts"] = migrated_accounts

        if "connectors" in data and isinstance(data["connectors"], dict):
            for k, v in data["connectors"].items():
                if k in merged["connectors"] and isinstance(v, dict):
                    merged["connectors"][k].update(v)
                else:
                    merged["connectors"][k] = v

        if "aurarouter" in data and isinstance(data["aurarouter"], dict):
            merged["aurarouter"].update(data["aurarouter"])
        return merged
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings(settings: Dict[str, Any]) -> None:
    """Save settings dictionary to settings.json."""
    path = get_settings_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def get_accounts() -> List[Dict[str, Any]]:
    """Return all configured email accounts."""
    settings = load_settings()
    return settings.get("accounts", [])


def save_account(account: Dict[str, Any]) -> Dict[str, Any]:
    """Add or update an email account in settings."""
    settings = load_settings()
    accounts = settings.get("accounts", [])
    acc_id = account.get("id")
    if not acc_id:
        import uuid
        acc_id = f"acc_{uuid.uuid4().hex[:8]}"
        account["id"] = acc_id

    existing_idx = next((i for i, a in enumerate(accounts) if a.get("id") == acc_id), -1)
    if existing_idx >= 0:
        accounts[existing_idx] = account
    else:
        accounts.append(account)

    settings["accounts"] = accounts
    save_settings(settings)
    return account


def delete_account(account_id: str) -> bool:
    """Delete an email account by ID."""
    settings = load_settings()
    accounts = settings.get("accounts", [])
    orig_len = len(accounts)
    accounts = [a for a in accounts if a.get("id") != account_id]
    if len(accounts) != orig_len:
        settings["accounts"] = accounts
        save_settings(settings)
        return True
    return False


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve account configuration by ID."""
    accounts = get_accounts()
    for a in accounts:
        if a.get("id") == account_id:
            return a
    return None


def validate_storage_location(raw_path: str) -> Dict[str, Any]:
    """Validate a candidate storage directory and perform security/privacy auditing."""
    if not raw_path or not raw_path.strip():
        return {
            "is_safe": True,
            "is_critical": False,
            "warning": None,
            "path": str(get_default_app_data_dir()),
        }

    norm = os.path.abspath(raw_path.strip())
    norm_lower = norm.lower()

    # 1. UNC Network Share Check
    if norm.startswith(r"\\") or norm.startswith("//"):
        return {
            "is_safe": False,
            "is_critical": True,
            "warning": (
                "CRITICAL WARNING: Storage path is a UNC network share. "
                "Local encryption keys and archive contents will be transmitted across the network, "
                "risking eavesdropping and external tampering."
            ),
            "path": norm,
        }

    # 2. Cloud Sync Directory Check (OneDrive, Dropbox, Google Drive, iCloud, Box)
    cloud_keywords = {
        "onedrive": "Microsoft OneDrive",
        "dropbox": "Dropbox",
        "google drive": "Google Drive",
        "icloud": "Apple iCloud Drive",
        "box sync": "Box",
        "box": "Box",
        "owncloud": "ownCloud",
        "nextcloud": "Nextcloud",
    }
    for kw, service in cloud_keywords.items():
        if f"\\{kw}\\" in norm_lower or f"/{kw}/" in norm_lower or norm_lower.endswith(f"\\{kw}") or norm_lower.endswith(f"/{kw}"):
            return {
                "is_safe": False,
                "is_critical": True,
                "warning": (
                    f"CRITICAL PRIVACY WARNING: Storage path is inside a cloud-synchronized folder ({service}). "
                    "Your private local email archive, decrypted chunks, and indexes will be uploaded "
                    "to external third-party cloud servers!"
                ),
                "path": norm,
            }

    # 3. Removable Media Check (Windows GetDriveTypeW)
    if sys.platform == "win32":
        try:
            import ctypes
            drive = os.path.splitdrive(norm)[0]
            if drive:
                drive_root = drive + "\\"
                dtype = ctypes.windll.kernel32.GetDriveTypeW(drive_root)
                # DRIVE_REMOVABLE = 2, DRIVE_REMOTE = 4, DRIVE_CDROM = 5, DRIVE_RAMDISK = 6
                if dtype == 2:
                    return {
                        "is_safe": False,
                        "is_critical": False,
                        "warning": (
                            "CAUTION: Storage path is on removable media (USB/SD drive). "
                            "Performance may be reduced and the archive will become inaccessible if unplugged."
                        ),
                        "path": norm,
                    }
                elif dtype == 4:
                    return {
                        "is_safe": False,
                        "is_critical": True,
                        "warning": (
                            "CRITICAL WARNING: Storage path is on a mapped network drive. "
                            "Data is transmitted across the local network."
                        ),
                        "path": norm,
                    }
        except Exception:
            pass

    return {
        "is_safe": True,
        "is_critical": False,
        "warning": None,
        "path": norm,
    }


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def _dir_size(path: Path) -> int:
    """Calculate total size of all files under path."""
    if not path.exists():
        return 0
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except Exception:
        pass
    return total


def get_storage_breakdown(app_data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Calculate disk usage breakdown across CAS tiers, vector indexes, databases, and views."""
    root = app_data_dir or get_default_app_data_dir()

    blob_size = _dir_size(root / "archive" / "blob")
    text_size = _dir_size(root / "archive" / "text")
    msg_size = _dir_size(root / "archive" / "msg")
    lancedb_size = _dir_size(root / "lancedb")
    models_size = _dir_size(root / "models")
    views_size = _dir_size(root / "views")

    sqlite_size = 0
    for name in ["catalog.db", "catalog.db-wal", "catalog.db-shm", "lexical_fts.db", "lexical_fts.db-wal", "lexical_fts.db-shm"]:
        db_file = root / name
        if db_file.exists():
            try:
                sqlite_size += db_file.stat().st_size
            except OSError:
                pass

    total_size = blob_size + text_size + msg_size + lancedb_size + models_size + views_size + sqlite_size

    return {
        "root_path": str(root),
        "total_bytes": total_size,
        "total_formatted": format_bytes(total_size),
        "breakdown": {
            "cas_blobs": {"bytes": blob_size, "formatted": format_bytes(blob_size), "label": "CAS Binary Blobs (Tier 2)"},
            "cas_text": {"bytes": text_size, "formatted": format_bytes(text_size), "label": "Encrypted Text Archive (Tier 1)"},
            "cas_msg": {"bytes": msg_size, "formatted": format_bytes(msg_size), "label": "Raw .msg Files (Tier 3)"},
            "lancedb": {"bytes": lancedb_size, "formatted": format_bytes(lancedb_size), "label": "LanceDB Vector Index"},
            "models": {"bytes": models_size, "formatted": format_bytes(models_size), "label": "Nomic ONNX Weights & Tokenizer"},
            "sqlite": {"bytes": sqlite_size, "formatted": format_bytes(sqlite_size), "label": "SQLite Catalogs & FTS5"},
            "views": {"bytes": views_size, "formatted": format_bytes(views_size), "label": "Decrypted Temporary Views"},
        },
    }


def sweep_temp_views(app_data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Securely delete decrypted temporary view files and return count and freed bytes."""
    root = app_data_dir or get_default_app_data_dir()
    views_dir = root / "views"
    if not views_dir.exists():
        return {"swept_files": 0, "freed_bytes": 0}

    count = 0
    freed = 0
    try:
        for p in views_dir.glob("*"):
            if p.is_file():
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    count += 1
                except OSError:
                    pass
    except Exception:
        pass

    return {
        "swept_files": count,
        "freed_bytes": freed,
        "freed_formatted": format_bytes(freed),
    }

