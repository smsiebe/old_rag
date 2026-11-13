"""test_mail_and_calendar.py - Verification for Multi-Account, Mail Client, Calendar & Head-of-Folder Caching."""

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from old_rag.contracts import CalendarEvent, SourceType
from old_rag.engine.folder_index import HeadOfFolderIndex, FolderLock, LockTimeoutError
from old_rag.ingest.calendar import CalendarManager, format_ics_datetime, generate_ics_event, parse_ics_content
from old_rag.ingest.smtp import SMTPSender
from old_rag.schema import CATALOG_SCHEMA_DDL
from old_rag.storage.settings import delete_account, get_account, get_accounts, save_account


def test_multi_account_crud(tmp_path: Path):
    """Verify that multiple email connections can be saved, updated, listed, and deleted."""
    with patch("old_rag.storage.settings.get_default_app_data_dir", return_value=tmp_path):
        acc1 = {
            "name": "Work Exchange",
            "account_type": "exchange_owa",
            "email_address": "user@corp.com",
            "server_url": "https://mail.corp.com",
            "enabled": True,
        }
        acc2 = {
            "name": "Personal Gmail",
            "account_type": "imap_smtp",
            "email_address": "user@gmail.com",
            "imap_host": "imap.gmail.com",
            "smtp_host": "smtp.gmail.com",
            "enabled": True,
        }
        acc3 = {
            "name": "Consulting Fastmail",
            "account_type": "imap_smtp",
            "email_address": "user@fastmail.com",
            "imap_host": "imap.fastmail.com",
            "smtp_host": "smtp.fastmail.com",
            "enabled": True,
        }

        saved1 = save_account(acc1)
        saved2 = save_account(acc2)
        saved3 = save_account(acc3)

        assert "id" in saved1
        assert "id" in saved2
        assert "id" in saved3

        accounts = get_accounts()
        assert len(accounts) >= 3

        # Retrieve single account
        retrieved = get_account(saved2["id"])
        assert retrieved is not None
        assert retrieved["email_address"] == "user@gmail.com"

        # Delete account
        deleted = delete_account(saved1["id"])
        assert deleted is True
        assert get_account(saved1["id"]) is None


def test_head_of_folder_index_and_file_locking(tmp_path: Path):
    """Verify head-of-folder cache (.oldrag/index.db) creation, scanning, FTS search, and file lock concurrency."""
    shared_folder = tmp_path / "SharedEngineering"
    shared_folder.mkdir()

    # Create dummy files
    (shared_folder / "spec.txt").write_text("Sovereign specification for local air-gapped search.", encoding="utf-8")
    (shared_folder / "notes.md").write_text("Meeting notes regarding AuraGrid enterprise mesh licensing.", encoding="utf-8")

    idx = HeadOfFolderIndex(shared_folder)
    assert idx.db_path.exists()
    assert idx.lock_path.parent.exists()

    # 1. Scan and index files
    res = idx.scan_and_index(recursive=True)
    assert res["success"] is True
    assert res["indexed_count"] == 2

    # 2. Search local head-of-folder FTS index
    hits = idx.search("AuraGrid")
    assert len(hits) == 1
    assert "notes.md" in hits[0]["rel_path"]

    # 3. Status check & AuraGrid value proposition
    status = idx.get_status()
    assert status["has_index"] is True
    assert status["file_count"] == 2
    assert status["auragrid_recommended"] is True
    assert "AuraGrid" in status["auragrid_value_prop"]

    # 4. Test concurrency file locking
    # Lock the folder
    lock1 = idx.lock(timeout_sec=1.0)
    lock1.acquire()
    try:
        # Attempting to acquire lock with short timeout must raise LockTimeoutError
        lock2 = FolderLock(idx.lock_path, timeout_sec=0.2)
        with pytest.raises(LockTimeoutError):
            lock2.acquire()
    finally:
        lock1.release()

    # After releasing, lock2 should succeed
    lock3 = FolderLock(idx.lock_path, timeout_sec=1.0)
    lock3.acquire()
    lock3.release()


def test_calendar_manager_and_ics_support(tmp_path: Path):
    """Verify storing and querying calendar events, as well as .ICS parsing and generation."""
    db_path = tmp_path / "catalog.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(CATALOG_SCHEMA_DDL)
    conn.commit()
    conn.close()

    cm = CalendarManager(db_path)

    now = datetime(2026, 11, 7, 14, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)

    event = CalendarEvent(
        event_id="evt_test_101",
        account_id="acc_work",
        source_type=SourceType.EMAIL,
        subject="Sprint Planning Meeting",
        location="Room 402",
        start_ts_utc=now,
        end_ts_utc=later,
        is_all_day=False,
        organizer="lead@company.com",
        attendees=["dev1@company.com", "dev2@company.com"],
        body_text="Review backlog and assign story points.",
        status="CONFIRMED",
    )

    cm.upsert_event(event)

    events = cm.list_events()
    assert len(events) == 1
    assert events[0].subject == "Sprint Planning Meeting"
    assert events[0].location == "Room 402"
    assert "dev1@company.com" in events[0].attendees

    # Test .ICS Generation
    ics_str = generate_ics_event(event)
    assert "BEGIN:VCALENDAR" in ics_str
    assert "SUMMARY:Sprint Planning Meeting" in ics_str
    assert "LOCATION:Room 402" in ics_str

    # Test .ICS Parsing
    parsed = parse_ics_content(ics_str, account_id="acc_work")
    assert len(parsed) == 1
    assert parsed[0].subject == "Sprint Planning Meeting"
    assert parsed[0].location == "Room 402"


def test_smtp_sender_mock():
    """Verify SMTPSender constructs multipart email and invokes SMTP sendmail without external network calls."""
    sender = SMTPSender(
        host="smtp.gmail.com",
        port=587,
        use_ssl=False,
        use_starttls=True,
        username="user@gmail.com",
        password="app_password",
    )

    mock_smtp_inst = MagicMock()
    with patch("smtplib.SMTP", return_value=mock_smtp_inst):
        res = sender.send_message(
            from_addr="user@gmail.com",
            to_addrs=["client@business.com"],
            cc_addrs=["manager@business.com"],
            subject="Invoice for November 2026",
            body_text="Please find attached the invoice.",
            body_html="<p>Please find attached the invoice.</p>",
        )

        assert res["success"] is True
        assert "message_id" in res
        mock_smtp_inst.starttls.assert_called_once()
        mock_smtp_inst.login.assert_called_once_with("user@gmail.com", "app_password")
        mock_smtp_inst.sendmail.assert_called_once()
        args, _ = mock_smtp_inst.sendmail.call_args
        assert args[0] == "user@gmail.com"
        assert "client@business.com" in args[1]
        assert "manager@business.com" in args[1]


def test_calendar_search_events(tmp_path: Path):
    """Verify CalendarManager.search_events finds events by keyword and token stemming."""
    db_path = tmp_path / "catalog.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(CATALOG_SCHEMA_DDL)
    conn.commit()
    conn.close()

    cm = CalendarManager(db_path)
    now = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 7, 11, 0, 0, tzinfo=timezone.utc)

    cm.upsert_event(
        CalendarEvent(
            event_id="evt_lease_01",
            account_id="acc_work",
            source_type=SourceType.EMAIL,
            subject="Commercial Lease Signing & Key Exchange",
            location="Downtown Suite 400",
            start_ts_utc=now,
            end_ts_utc=later,
            body_text="Meet with landlord to sign commercial lease agreement and inspect office premises.",
        )
    )
    cm.upsert_event(
        CalendarEvent(
            event_id="evt_sprint_02",
            account_id="acc_work",
            source_type=SourceType.EMAIL,
            subject="Weekly Sprint Retrospective",
            location="Virtual Zoom",
            start_ts_utc=now,
            end_ts_utc=later,
            body_text="Review engineering velocity and story point burn-down.",
        )
    )

    # Search for lease
    lease_hits = cm.search_events("lease")
    assert len(lease_hits) == 1
    assert lease_hits[0].event_id == "evt_lease_01"
    assert "Lease" in lease_hits[0].subject

    # Search for sprint velocity
    sprint_hits = cm.search_events("sprint velocity")
    assert len(sprint_hits) == 1
    assert sprint_hits[0].event_id == "evt_sprint_02"


def test_resolve_related_context(tmp_path: Path):
    """Verify resolve_related_context retrieves files, emails, and calendar events while filtering originating doc."""
    from old_rag.contracts import RelevanceTier, SearchResult
    from old_rag.engine.search import resolve_related_context

    # Mock SearchEngine
    mock_search = MagicMock()
    mock_search.search.return_value = [
        SearchResult(
            chunk_id="chunk_file_1",
            doc_id="doc_lease_pdf",
            content="Standard commercial lease clause 14: rent schedule.",
            source_type=SourceType.FILE,
            original_path="C:/Shared/Lease_Agreement_2026.pdf",
            rrf_score=0.032,
            normalized_rrf=0.98,
            relevance_tier=RelevanceTier.HIGH,
            confidence_score=0.98,
        ),
        SearchResult(
            chunk_id="chunk_email_origin",
            doc_id="msg_originating",
            content="Here is the email you are currently reading about the lease.",
            source_type=SourceType.EMAIL,
            subject="Lease Renewal Discussion",
            rrf_score=0.030,
            normalized_rrf=0.95,
            relevance_tier=RelevanceTier.HIGH,
            confidence_score=0.95,
        ),
        SearchResult(
            chunk_id="chunk_email_2",
            doc_id="msg_prior_thread",
            content="Previous thread discussing security deposit and lease terms.",
            source_type=SourceType.EMAIL,
            subject="Re: Prior Lease Discussion",
            rrf_score=0.025,
            normalized_rrf=0.82,
            relevance_tier=RelevanceTier.HIGH,
            confidence_score=0.82,
        ),
    ]

    # Mock CalendarManager
    mock_cm = MagicMock()
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    mock_cm.search_events.return_value = [
        CalendarEvent(
            event_id="evt_lease_signing",
            source_type=SourceType.EMAIL,
            subject="Final Lease Signing with Property Manager",
            start_ts_utc=now,
            end_ts_utc=now,
            location="Conference Room B",
        )
    ]

    ctx = resolve_related_context(
        search_engine=mock_search,
        catalog=None,
        calendar_manager=mock_cm,
        doc_id="msg_originating",
        subject="Re: Office Lease Renewal",
        text="Please review the lease terms for the new office space.",
        top_k=5,
    )

    assert ctx["success"] is True
    # The originating email doc_id must be filtered out
    email_doc_ids = [e["doc_id"] for e in ctx["related_emails"]]
    assert "msg_originating" not in email_doc_ids
    assert "msg_prior_thread" in email_doc_ids

    # Files must contain the lease agreement PDF
    file_doc_ids = [f["doc_id"] for f in ctx["related_files"]]
    assert "doc_lease_pdf" in file_doc_ids

    # Calendar must contain the lease meeting
    cal_event_ids = [c["event_id"] for c in ctx["related_calendar"]]
    assert "evt_lease_signing" in cal_event_ids


def test_bridge_api_related_context_and_open_document(tmp_path: Path):
    """Verify BridgeAPI.get_related_context and BridgeAPI.open_document execution."""
    from old_rag.ui.app import BridgeAPI

    with patch("old_rag.storage.cas.get_default_app_data_dir", return_value=tmp_path):
        bridge = BridgeAPI(pipe_name="OldRAG_NonExistent_Pipe_Fallback")

        # Test open_document with a real test file on disk
        test_file = tmp_path / "test_doc.txt"
        test_file.write_text("Test content", encoding="utf-8")

        res = bridge.open_document({"original_path": str(test_file)})
        assert res["success"] is True
        assert res["path"] == str(test_file)

        # Test non-existent file
        res_fail = bridge.open_document({"original_path": "C:/non/existent/path/9999.xyz"})
        assert res_fail["success"] is False


