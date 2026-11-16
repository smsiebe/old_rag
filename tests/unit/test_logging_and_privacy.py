"""test_logging_and_privacy.py - Unit tests for sanitized, privacy-safe logging."""

import logging
from pathlib import Path
from old_rag.logger import (
    SanitizingFormatter,
    get_logger,
    sanitize_log_text,
    setup_logging,
)


def test_sanitize_log_text_redactions():
    """Verify secrets, tokens, passwords, and keys are automatically sanitized."""
    raw = (
        "User login failed with password='MySecretPassword123' "
        "using bearer eyJhbGciOiJIUzI1Ni.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "and api_key='sk-1234567890abcdef' and token=ghp_abcdef1234567890 "
        "and master_key=0123456789abcdef0123456789abcdef"
    )
    cleaned = sanitize_log_text(raw)

    assert "MySecretPassword123" not in cleaned
    assert "[REDACTED_PASSWORD]" in cleaned
    assert "sflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in cleaned
    assert "[REDACTED_JWT]" in cleaned or "[REDACTED_TOKEN]" in cleaned
    assert "sk-1234567890abcdef" not in cleaned
    assert "[REDACTED_APIKEY]" in cleaned or "[REDACTED_TOKEN]" in cleaned
    assert "0123456789abcdef0123456789abcdef" not in cleaned
    assert "[REDACTED_KEY_HEX]" in cleaned


def test_sanitizing_formatter():
    """Verify SanitizingFormatter correctly scrubs formatted LogRecords."""
    formatter = SanitizingFormatter(fmt="%(levelname)s: %(message)s")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Connecting to IMAP host=%s with password=%s",
        args=("imap.example.com", "SuperSecretPwd!"),
        exc_info=None,
    )
    output = formatter.format(record)
    assert "imap.example.com" in output
    assert "SuperSecretPwd!" not in output
    assert "[REDACTED_PASSWORD]" in output


def test_file_logging_privacy_safeguard(tmp_path):
    """Verify rotating file handler writes sanitized logs to disk."""
    log_dir = tmp_path / "logs"
    setup_logging(log_level=logging.DEBUG, to_console=False, custom_log_dir=log_dir, force=True)
    logger = get_logger("privacy_test")

    logger.info("Ingesting doc doc_id=doc_1001 with token=secret_auth_token_999")
    log_file = log_dir / "old_rag.log"
    assert log_file.exists()

    content = log_file.read_text(encoding="utf-8")
    assert "doc_1001" in content
    assert "secret_auth_token_999" not in content
    assert "[REDACTED_TOKEN]" in content

