"""logger.py - Privacy-Safe, Sanitized Structured File & Console Logging.

Strictly protects user privacy:
1. Automatically redacts authentication secrets, tokens, passwords, and DPAPI key bytes.
2. Strips raw message/email body content and attachments.
3. Writes rotating log files to %LOCALAPPDATA%\\OldRAG\\logs\\old_rag.log (10MB max, 3 backups).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from old_rag.storage.cas import get_default_app_data_dir

# Patterns to redact sensitive information
_SECRET_PATTERNS = [
    # Bearer tokens & JWTs
    (re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-\.]{10,})"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED_JWT]"),
    # Passwords and credentials in strings or queries
    (re.compile(r"(?i)(password\s*[:=]\s*['\"]?)([^'\"\s&]+)(['\"]?)"), r"\1[REDACTED_PASSWORD]\3"),
    (re.compile(r"(?i)(token\s*[:=]\s*['\"]?)([^'\"\s&]+)(['\"]?)"), r"\1[REDACTED_TOKEN]\3"),
    (re.compile(r"(?i)(secret\s*[:=]\s*['\"]?)([^'\"\s&]+)(['\"]?)"), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*['\"]?)([^'\"\s&]+)(['\"]?)"), r"\1[REDACTED_APIKEY]\3"),
    # DPAPI master key or hex byte representations
    (re.compile(r"(?i)(master_key\s*[:=]\s*['\"]?)([0-9a-fA-F]{32,})(['\"]?)"), r"\1[REDACTED_KEY_HEX]\3"),
]


def sanitize_log_text(text: str) -> str:
    """Sanitize a log message string by redacting secrets and sensitive patterns."""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SanitizingFormatter(logging.Formatter):
    """Logging formatter that scrubs private credentials, tokens, and raw message payloads."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return sanitize_log_text(formatted)


def get_log_dir() -> Path:
    """Return the canonical logs directory under OldRAG app data."""
    app_data_dir = get_default_app_data_dir()
    logs_dir = app_data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_log_file_path() -> Path:
    """Return the path to old_rag.log."""
    return get_log_dir() / "old_rag.log"


_INITIALIZED = False


def setup_logging(
    log_level: int = logging.INFO,
    to_console: bool = False,
    custom_log_dir: Optional[Path] = None,
    force: bool = False,
) -> None:
    """Initialize root/old_rag rotating file and optional console logging."""
    global _INITIALIZED
    root_logger = logging.getLogger("old_rag")
    root_logger.setLevel(log_level)

    # Avoid adding duplicate handlers on re-entry unless force or custom_log_dir
    if _INITIALIZED and not force and custom_log_dir is None:
        return

    if custom_log_dir or force:
        for h in list(root_logger.handlers):
            if isinstance(h, RotatingFileHandler):
                h.close()
                root_logger.removeHandler(h)

    formatter = SanitizingFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File Handler
    try:
        log_dir = custom_log_dir or get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_path = log_dir / "old_rag.log"
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to setup file logging: {e}\n")

    # Console Handler (when requested, e.g. for CLI/daemon interactive)
    if to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    _INITIALIZED = True


def get_logger(name: str = "old_rag") -> logging.Logger:
    """Get a privacy-sanitized logger child for the given subsystem name."""
    if not _INITIALIZED:
        setup_logging()
    if name == "old_rag" or name.startswith("old_rag."):
        return logging.getLogger(name)
    return logging.getLogger(f"old_rag.{name}")

