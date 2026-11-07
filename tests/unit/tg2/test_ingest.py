"""test_ingest.py - Unit tests for Classic MAPI and Watcher Ingestors."""

from datetime import datetime, timezone
from pathlib import Path

from old_rag.ingest.outlook import ClassicMAPIConnector
from old_rag.ingest.watcher import (
    is_cloud_placeholder,
    is_temporary_office_file,
    parse_eml_file,
)


def test_dasl_utc_formatting():
    connector = ClassicMAPIConnector()
    dt = datetime(2026, 11, 6, 15, 30, 0, tzinfo=timezone.utc)
    dasl_inbox = connector.format_dasl_query(dt, is_sent_folder=False)
    assert dasl_inbox == '@SQL="urn:schemas:httpmail:datereceived" >= \'2026-11-06 15:30:00\''

    dasl_sent = connector.format_dasl_query(dt, is_sent_folder=True)
    assert dasl_sent == '@SQL="urn:schemas:httpmail:date" >= \'2026-11-06 15:30:00\''


def test_temporary_office_file_filter():
    assert is_temporary_office_file("~$Budget2026.xlsx") is True
    assert is_temporary_office_file("temp_file.tmp") is True
    assert is_temporary_office_file("Budget2026.xlsx") is False
    assert is_temporary_office_file("report.docx") is False


def test_eml_file_parsing(temp_app_dir):
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
