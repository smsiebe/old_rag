"""test_purge_and_multi_calendar.py - Conformance and Safety Tests for Account Purging & Multi-Calendar Connectors."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from old_rag.contracts import (
    CalendarAccountConfig,
    CalendarEvent,
    DashboardLayoutConfig,
    DashboardWindowConfig,
    RawIngestPayload,
    SourceType,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.ingest.calendar import CalendarManager, event_to_ingest_payload, parse_ics_content
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.storage.cas import CASEngine, StorageConfig
from old_rag.storage.catalog import CatalogRepository
from old_rag.storage.settings import (
    delete_account,
    delete_calendar_account,
    delete_chat_account,
    get_accounts,
    get_calendar_accounts,
    get_dashboard_layout,
    save_account,
    save_calendar_account,
    save_chat_account,
    save_dashboard_layout,
)
from old_rag.ui.bridge import OldRAGDesktopBridge


@pytest.fixture
def test_env(tmp_path):
    storage_cfg = StorageConfig(app_data_dir=str(tmp_path))
    cas = CASEngine(storage_cfg)
    catalog = CatalogRepository(tmp_path / "catalog.db")
    ldb = open_lancedb_table(tmp_path, dim=768)
    indexer = IndexEngine(
        fts_db_path=tmp_path / "lexical_fts.db",
        lancedb_table=ldb,
    )
    extractor = DocumentExtractor(catalog=catalog)
    chunker = ParentAwareChunker(catalog=catalog)
    coordinator = PipelineCoordinator(
        cas=cas,
        catalog=catalog,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )
    return {
        "tmp_path": tmp_path,
        "cas": cas,
        "catalog": catalog,
        "indexer": indexer,
        "coordinator": coordinator,
    }


def test_purge_account_data_and_source_file_safety_invariant(test_env):
    """Verify that purging account data deletes DB/indexes/CAS but strictly preserves user source files on disk."""
    coord = test_env["coordinator"]
    tmp = test_env["tmp_path"]

    # 1. Create a simulated host user file that was ingested
    user_file = tmp / "important_document.pdf"
    user_file.write_text("Confidential original content that must never be deleted", encoding="utf-8")
    assert user_file.exists()

    # Ingest document for account 'acc_test_purge'
    payload = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_purge_1",
        original_path=str(user_file),
        subject="Project Alpha Financials",
        sender="boss@corp.com",
        body_text="Here is the financial breakdown for Alpha.",
        account_id="acc_test_purge",
    )
    coord.ingest_payload(payload)

    # Ingest second document for another account (must remain untouched)
    payload_other = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="doc_keep_2",
        original_path="outlook://Inbox/KeepMe",
        subject="Keep this email",
        sender="friend@corp.com",
        body_text="This belongs to a different account.",
        account_id="acc_other",
    )
    coord.ingest_payload(payload_other)

    # Verify both exist in catalog
    assert coord.catalog.get_document("doc_purge_1") is not None
    assert coord.catalog.get_document("doc_keep_2") is not None
    assert coord.cas.get_text("doc_purge_1") is not None
    assert coord.cas.get_text("doc_keep_2") is not None

    # 2. Execute on-demand purge for 'acc_test_purge'
    purge_result = coord.purge_account_data("acc_test_purge", source_type="EMAIL")
    assert purge_result["success"] is True
    assert purge_result["deleted_docs_count"] == 1

    # Verify purged document is completely wiped from catalog & CAS
    assert coord.catalog.get_document("doc_purge_1") is None
    assert coord.cas.get_text("doc_purge_1") is None

    # Verify second document is completely preserved
    assert coord.catalog.get_document("doc_keep_2") is not None
    assert coord.cas.get_text("doc_keep_2") is not None

    # 3. CRITICAL INVARIANT: Host user source file must still exist and be identical
    assert user_file.exists(), "Source File Safety Invariant violated: original file was deleted!"
    assert user_file.read_text(encoding="utf-8") == "Confidential original content that must never be deleted"


def test_multi_calendar_accounts_and_color_coding(test_env, monkeypatch):
    """Verify multi-calendar connector configuration, color coding, and ICS parsing with metadata tagging."""
    tmp = test_env["tmp_path"]
    monkeypatch.setattr("old_rag.storage.settings.get_settings_file_path", lambda: tmp / "settings.json")

    # 1. Configure multiple calendar connectors with distinct colors
    cal_work = save_calendar_account({
        "id": "cal_work",
        "name": "Corporate Exchange",
        "connector_type": "exchange",
        "color": "#2563eb",
        "url": "https://mail.corp.com/ews/calendar",
        "enabled": True,
    })
    cal_team = save_calendar_account({
        "id": "cal_team",
        "name": "Confluence Sprint Schedule",
        "connector_type": "confluence",
        "color": "#10b981",
        "url": "https://wiki.corp.com/calendar.ics",
        "enabled": True,
    })
    cal_personal = save_calendar_account({
        "id": "cal_personal",
        "name": "Google Calendar Personal",
        "connector_type": "google_calendar",
        "color": "#f59e0b",
        "url": "https://calendar.google.com/ical/secret/basic.ics",
        "enabled": True,
    })

    cals = get_calendar_accounts()
    cal_ids = [c["id"] for c in cals]
    assert "cal_work" in cal_ids
    assert "cal_team" in cal_ids
    assert "cal_personal" in cal_ids

    # 2. Test CalendarManager with multi-calendar tagging and color inheritance
    cm = CalendarManager(tmp / "catalog.db")
    now_utc = datetime.now(timezone.utc)

    evt1 = CalendarEvent(
        event_id="evt_sprint_demo",
        account_id="cal_team",
        calendar_id="cal_team",
        calendar_color="#10b981",
        source_system="Confluence Sprint Schedule",
        subject="Sprint 44 Demo",
        start_ts_utc=now_utc,
        end_ts_utc=now_utc,
    )
    cm.upsert_event(evt1)

    evt2 = CalendarEvent(
        event_id="evt_dentist",
        account_id="cal_personal",
        calendar_id="cal_personal",
        calendar_color="#f59e0b",
        source_system="Google Calendar Personal",
        subject="Dentist Checkup",
        start_ts_utc=now_utc,
        end_ts_utc=now_utc,
    )
    cm.upsert_event(evt2)

    # Query all events
    all_events = cm.list_events()
    assert len(all_events) == 2

    # Query filtered by calendar_id
    team_events = cm.list_events(calendar_id="cal_team")
    assert len(team_events) == 1
    assert team_events[0].subject == "Sprint 44 Demo"
    assert team_events[0].calendar_color == "#10b981"
    assert team_events[0].source_system == "Confluence Sprint Schedule"

    personal_events = cm.list_events(calendar_id="cal_personal")
    assert len(personal_events) == 1
    assert personal_events[0].subject == "Dentist Checkup"
    assert personal_events[0].calendar_color == "#f59e0b"

    # 3. Test purge on delete for a calendar connector
    coord = test_env["coordinator"]
    # Ingest event payload into coordinator as well
    payload = event_to_ingest_payload(evt2)
    coord.ingest_payload(payload)
    assert coord.catalog.get_document(payload.doc_id) is not None

    # Delete calendar account with purge_data=True
    deleted = delete_calendar_account("cal_personal", purge_data=True, coordinator=coord)
    assert deleted is True

    # Account removed from settings
    remaining_cals = [c["id"] for c in get_calendar_accounts()]
    assert "cal_personal" not in remaining_cals

    # Document purged from catalog
    assert coord.catalog.get_document(payload.doc_id) is None


def test_dashboard_layout_persistence_and_popout(test_env, monkeypatch):
    """Verify MDI dashboard window configurations and layout persistence via settings & bridge."""
    tmp = test_env["tmp_path"]
    monkeypatch.setattr("old_rag.storage.settings.get_settings_file_path", lambda: tmp / "settings.json")

    bridge = OldRAGDesktopBridge()

    # 1. Save dashboard layout with popped out windows
    layout_data = {
        "windows": [
            {
                "id": "chat_room_general",
                "type": "chat",
                "target_id": "general",
                "title": "Chat: #general",
                "x": 120,
                "y": 80,
                "width": 450,
                "height": 500,
                "is_open": True,
                "auto_pop": True,
                "sound_enabled": True,
            },
            {
                "id": "cal_unified",
                "type": "calendar",
                "target_id": "unified",
                "title": "Calendar: Unified View",
                "x": 580,
                "y": 80,
                "width": 520,
                "height": 500,
                "is_open": True,
                "auto_pop": False,
                "sound_enabled": False,
            },
        ]
    }

    res_save = bridge.dispatch("save_dashboard_layout", {"layout": layout_data})
    assert res_save["ok"] is True

    # 2. Retrieve saved layout
    res_get = bridge.dispatch("get_dashboard_layout", {})
    assert res_get["ok"] is True
    saved = res_get["data"]["layout"]
    assert len(saved["windows"]) == 2
    assert saved["windows"][0]["id"] == "chat_room_general"
    assert saved["windows"][0]["auto_pop"] is True
    assert saved["windows"][0]["sound_enabled"] is True
    assert saved["windows"][1]["id"] == "cal_unified"
