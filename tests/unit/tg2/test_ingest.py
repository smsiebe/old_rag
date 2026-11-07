"""test_ingest.py - Unit tests for Classic MAPI and Watcher Ingestors (TG2)."""

import sys
from datetime import datetime, timezone
from pathlib import Path
import pytest

from old_rag.contracts import ServiceUnavailableError
from old_rag.ingest.outlook import (
    ClassicMAPIConnector,
    is_new_outlook_active,
    probe_object_model_guard,
)
from old_rag.ingest.watcher import (
    is_cloud_placeholder,
    is_temporary_office_file,
    parse_eml_file,
)


# --- T2.1: Classic MAPI Ingestor Tests ---

def test_t2_1_dasl_utc_formatting():
    connector = ClassicMAPIConnector()
    dt = datetime(2026, 11, 6, 15, 30, 0, tzinfo=timezone.utc)
    dasl_inbox = connector.format_dasl_query(dt, is_sent_folder=False)
    assert dasl_inbox == '@SQL="urn:schemas:httpmail:datereceived" >= \'2026-11-06 15:30:00\''

    dasl_sent = connector.format_dasl_query(dt, is_sent_folder=True)
    assert dasl_sent == '@SQL="urn:schemas:httpmail:date" >= \'2026-11-06 15:30:00\''

    # Negative: None date returns None
    assert connector.format_dasl_query(None) is None


def test_t2_1_new_outlook_detector():
    # In headless/standard CI without olk.exe running, detector returns boolean
    res = is_new_outlook_active()
    assert isinstance(res, bool)


def test_t2_1_object_model_guard_probe():
    # Probe runs within bounded timeout
    res = probe_object_model_guard(timeout_sec=0.5)
    assert isinstance(res, bool)


def test_t2_1_check_availability_negative():
    connector = ClassicMAPIConnector()
    # When simulated on non-Windows or when Outlook is not running
    if sys.platform != "win32":
        with pytest.raises(ServiceUnavailableError):
            connector.check_availability()


# --- T2.2: Filesystem Watcher & EML Parser Tests ---

def test_t2_2_temporary_office_file_filter():
    assert is_temporary_office_file("~$Budget2026.xlsx") is True
    assert is_temporary_office_file("temp_file.tmp") is True
    assert is_temporary_office_file("~*.tmp") is True
    # Negative assertions: valid documents not filtered
    assert is_temporary_office_file("Budget2026.xlsx") is False
    assert is_temporary_office_file("report.docx") is False
    assert is_temporary_office_file("presentation.pptx") is False


def test_t2_2_cloud_placeholder_detection(temp_app_dir):
    # Standard local file on disk should not be marked as cloud placeholder
    local_file = temp_app_dir / "regular.pdf"
    local_file.write_bytes(b"DATA")
    assert is_cloud_placeholder(local_file) is False

    # Negative: non-existent file returns False
    assert is_cloud_placeholder(temp_app_dir / "does_not_exist.bin") is False


def test_t2_2_eml_file_parsing(temp_app_dir):
    eml_content = (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: Test Incident Report SN-4471-AB\r\n"
        b"Message-ID: <inc-4471@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"The server failed with status code 500.\r\n"
    )
    eml_path = temp_app_dir / "test.eml"
    eml_path.write_bytes(eml_content)

    payload = parse_eml_file(eml_path)
    assert payload.sender == "sender@example.com"
    assert payload.subject == "Test Incident Report SN-4471-AB"
    assert payload.internet_message_id == "<inc-4471@example.com>"
    assert "status code 500" in payload.body_text
    assert payload.folder == eml_path.parent.name
