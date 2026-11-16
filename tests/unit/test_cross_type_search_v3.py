"""test_cross_type_search_v3.py - Comprehensive Unit Tests for Task Group 2.

Verifies:
1. Hybrid search across Email, File, Calendar, and Chat with single and multi-source filters.
2. Exact identifier matching and document_entities boosting.
3. resolve_related_context returning related_files, related_emails, related_chats, and related_calendar.
4. MatchExplainer Tier 1 deterministic explanations for Chat and Calendar search hits.
5. MCP Server tool interfaces for query_personal_archive with source_types, list_chat_rooms, list_chat_messages, list_calendar_events.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from old_rag.contracts import (
    CalendarEvent,
    CalendarIngestMeta,
    ChatIngestMeta,
    ChatMessageItem,
    ModelConfig,
    RawIngestPayload,
    SearchQuery,
    SourceType,
    StorageConfig,
)
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.explainer import explain_match_tier1
from old_rag.engine.extractor import DocumentExtractor
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.engine.search import HybridSearchEngine, resolve_related_context
from old_rag.ingest.calendar import CalendarManager, event_to_ingest_payload
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.storage.cas import CASEngine
from old_rag.storage.catalog import CatalogRepository


@pytest.fixture
def search_fixture(tmp_path: Path):
    app_data = tmp_path / "search_tg2"
    app_data.mkdir(parents=True, exist_ok=True)

    storage_cfg = StorageConfig(app_data_dir=str(app_data))
    cas = CASEngine(storage_cfg)
    catalog = CatalogRepository(app_data / "catalog.db")
    model_cfg = ModelConfig()

    lancedb_table = open_lancedb_table(app_data, dim=model_cfg.matryoshka_dim)
    indexer = IndexEngine(
        fts_db_path=app_data / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
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
    search_engine = HybridSearchEngine(
        fts_db_path=app_data / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
        catalog=catalog,
    )
    cal_manager = CalendarManager(app_data / "catalog.db")

    return {
        "app_data": app_data,
        "cas": cas,
        "catalog": catalog,
        "coordinator": coordinator,
        "search_engine": search_engine,
        "cal_manager": cal_manager,
        "model_cfg": model_cfg,
    }


def test_hybrid_search_across_email_file_calendar_and_chat_with_source_filters(search_fixture):
    coordinator = search_fixture["coordinator"]
    search_engine = search_fixture["search_engine"]
    catalog = search_fixture["catalog"]
    cal_manager = search_fixture["cal_manager"]

    now = datetime.now(timezone.utc)

    # 1. Ingest EMAIL
    p_email = RawIngestPayload(
        source_type=SourceType.EMAIL,
        doc_id="email_apollo_1",
        original_path="mailbox://inbox/101",
        received_ts_utc=now - timedelta(days=5),
        sender="mission-control@nasa.gov",
        subject="Project Apollo Flight Trajectory Review",
        body_text="Discussion of Project Apollo orbital flight trajectory and docking procedures.",
    )
    coordinator.ingest_payload(p_email)

    # 2. Ingest FILE
    p_file = RawIngestPayload(
        source_type=SourceType.FILE,
        doc_id="file_apollo_1",
        original_path="C:/documents/Project_Apollo_Specs.pdf",
        received_ts_utc=now - timedelta(days=4),
        subject="Project Apollo Specifications",
        body_text="Technical specifications document for Project Apollo lunar module propulsion systems.",
    )
    coordinator.ingest_payload(p_file)

    # 3. Ingest CALENDAR
    cal_event = CalendarEvent(
        event_id="cal_apollo_1",
        doc_id="doc_cal_apollo_1",
        account_id="acc_cal",
        source_type=SourceType.CALENDAR,
        subject="Project Apollo Flight Readiness Review",
        location="Mission Control Room 4",
        start_ts_utc=now + timedelta(days=2),
        end_ts_utc=now + timedelta(days=2, hours=2),
        organizer="gene.kranz@nasa.gov",
        attendees=["flight-directors@nasa.gov"],
        body_text="Mandatory Go/No-Go flight readiness meeting for Project Apollo.",
    )
    cal_manager.upsert_event(cal_event)
    p_cal = event_to_ingest_payload(cal_event)
    coordinator.ingest_payload(p_cal)

    # 4. Ingest CHAT
    chat_meta = ChatIngestMeta(
        account_id="rc_space",
        adapter_type="rocketchat",
        room_id="flight-ops",
        room_name="Flight Operations",
        is_dm=False,
        message_ids=["msg_chat_1", "msg_chat_2"],
    )
    p_chat = RawIngestPayload(
        source_type=SourceType.CHAT,
        doc_id="chat_apollo_1",
        original_path="rocketchat://rc_space/flight-ops",
        received_ts_utc=now - timedelta(hours=3),
        sender="capcom",
        subject="Flight Operations: Project Apollo telemetry check",
        body_text="Telemetry verified for Project Apollo guidance computer SN-APOLLO-77.",
        chat_meta=chat_meta,
    )
    coordinator.ingest_payload(p_chat)

    # Ingest document entity for entity boosting test
    catalog.upsert_document_entities("file_apollo_1", [("part_number", "SN-APOLLO-77", "Project_Apollo_Specs.pdf")])

    # A. Search without source filter: must surface hits across all 4 sources
    q_all = SearchQuery(query_text="Project Apollo", top_k=20)
    results_all = search_engine.search(q_all)
    found_types = {r.source_type for r in results_all}

    assert SourceType.EMAIL in found_types
    assert SourceType.FILE in found_types
    assert SourceType.CALENDAR in found_types
    assert SourceType.CHAT in found_types

    # Verify correct source types on results
    chat_results = [r for r in results_all if r.source_type == SourceType.CHAT]
    assert len(chat_results) >= 1
    assert chat_results[0].doc_id == "chat_apollo_1"

    cal_results = [r for r in results_all if r.source_type == SourceType.CALENDAR]
    assert len(cal_results) >= 1
    assert "cal_apollo" in cal_results[0].doc_id

    # B. Single-source filter: CHAT only
    q_chat = SearchQuery(query_text="Project Apollo", source_type=SourceType.CHAT, top_k=10)
    results_chat = search_engine.search(q_chat)
    assert len(results_chat) >= 1
    assert all(r.source_type == SourceType.CHAT for r in results_chat)

    # C. Multi-source filter: CALENDAR and FILE only
    q_multi = SearchQuery(
        query_text="Project Apollo",
        source_types=[SourceType.CALENDAR, SourceType.FILE],
        top_k=10,
    )
    results_multi = search_engine.search(q_multi)
    assert len(results_multi) >= 2
    assert all(r.source_type in (SourceType.CALENDAR, SourceType.FILE) for r in results_multi)
    assert not any(r.source_type in (SourceType.EMAIL, SourceType.CHAT) for r in results_multi)

    # D. Exact identifier / document_entities boosting: "SN-APOLLO-77"
    q_ident = SearchQuery(query_text="SN-APOLLO-77", top_k=10)
    results_ident = search_engine.search(q_ident)
    assert len(results_ident) >= 1
    top_hit = results_ident[0]
    assert top_hit.exact_identifier_match is True
    assert top_hit.confidence_score == 1.0


def test_related_context_graph_links_meeting_email_chat_and_file(search_fixture):
    coordinator = search_fixture["coordinator"]
    search_engine = search_fixture["search_engine"]
    catalog = search_fixture["catalog"]
    cal_manager = search_fixture["cal_manager"]

    now = datetime.now(timezone.utc)

    # 1. Related File
    coordinator.ingest_payload(
        RawIngestPayload(
            source_type=SourceType.FILE,
            doc_id="file_quarterly_budget",
            original_path="C:/finance/Q4_Budget_Review.xlsx",
            received_ts_utc=now - timedelta(days=2),
            subject="Q4 Budget Review",
            body_text="Detailed financial spreadsheets for Q4 Budget Review and allocation.",
        )
    )

    # 2. Related Email
    coordinator.ingest_payload(
        RawIngestPayload(
            source_type=SourceType.EMAIL,
            doc_id="email_budget_threads",
            original_path="mailbox://inbox/budget_1",
            received_ts_utc=now - timedelta(days=1),
            sender="cfo@company.com",
            subject="Re: Q4 Budget Review Notes",
            body_text="Here are the executive notes regarding Q4 Budget Review.",
        )
    )

    # 3. Related Chat
    coordinator.ingest_payload(
        RawIngestPayload(
            source_type=SourceType.CHAT,
            doc_id="chat_budget_standup",
            original_path="rocketchat://rc/finance-ops",
            received_ts_utc=now - timedelta(hours=4),
            sender="controller",
            subject="Finance Ops: Q4 Budget Review final adjustments",
            body_text="Adjusted line items for Q4 Budget Review spreadsheet.",
            chat_meta=ChatIngestMeta(
                account_id="rc",
                adapter_type="rocketchat",
                room_id="finance-ops",
                room_name="Finance Operations",
                is_dm=False,
                message_ids=["m1"],
            ),
        )
    )

    # 4. Related Calendar Event
    cal_event = CalendarEvent(
        event_id="cal_budget_meeting",
        doc_id="doc_cal_budget_meeting",
        account_id="acc1",
        source_type=SourceType.CALENDAR,
        subject="Q4 Budget Review Board Presentation",
        start_ts_utc=now + timedelta(days=1),
        end_ts_utc=now + timedelta(days=1, hours=1),
        organizer="cfo@company.com",
        body_text="Annual board review for Q4 Budget Review.",
    )
    cal_manager.upsert_event(cal_event)
    coordinator.ingest_payload(event_to_ingest_payload(cal_event))

    # Query related context using subject "Q4 Budget Review"
    context = resolve_related_context(
        search_engine=search_engine,
        catalog=catalog,
        calendar_manager=cal_manager,
        subject="Q4 Budget Review",
        text="Reviewing the annual allocations and spreadsheets.",
        top_k=5,
    )

    assert context["success"] is True
    assert len(context["related_files"]) >= 1
    assert len(context["related_emails"]) >= 1
    assert len(context["related_chats"]) >= 1
    assert len(context["related_calendar"]) >= 1

    # Verify segregation
    assert all(f.get("source_type") == "FILE" for f in context["related_files"])
    assert all(e.get("source_type") == "EMAIL" for e in context["related_emails"])
    assert all(c.get("source_type") == "CHAT" for c in context["related_chats"])


def test_explainer_tier1_supports_chat_and_calendar_search_hits(search_fixture):
    app_data = search_fixture["app_data"]
    cas = search_fixture["cas"]
    catalog = search_fixture["catalog"]
    cal_manager = search_fixture["cal_manager"]
    model_cfg = search_fixture["model_cfg"]
    coordinator = search_fixture["coordinator"]

    now = datetime.now(timezone.utc)

    # 1. Ingest Chat Hit
    chat_meta = ChatIngestMeta(
        account_id="teams_corp",
        adapter_type="teams",
        room_id="engineering-core",
        room_name="Engineering Core",
        is_dm=False,
        message_ids=["msg_101", "msg_102"],
    )
    p_chat = RawIngestPayload(
        source_type=SourceType.CHAT,
        doc_id="chat_eng_sync",
        original_path="teams://teams_corp/engineering-core",
        received_ts_utc=now,
        sender="sarah@corp.com",
        subject="Engineering Core: Architecture Refactor",
        body_text="Architecture refactor milestone and design doc review.",
        chat_meta=chat_meta,
    )
    coordinator.ingest_payload(p_chat)

    # 2. Ingest Calendar Hit
    cal_evt = CalendarEvent(
        event_id="cal_arch_review",
        doc_id="doc_cal_arch_review",
        account_id="cal_acc",
        source_type=SourceType.CALENDAR,
        subject="Architecture Refactor Sync",
        location="Room 3B",
        start_ts_utc=now + timedelta(days=1),
        end_ts_utc=now + timedelta(days=1, hours=1),
        organizer="sarah@corp.com",
        body_text="Sync meeting to discuss architecture refactor.",
    )
    cal_manager.upsert_event(cal_evt)
    coordinator.ingest_payload(event_to_ingest_payload(cal_evt))

    # Test Explainer on CHAT hit
    chat_payload = {
        "score_snapshot": {
            "chunk_id": "chat_eng_sync_0000",
            "doc_id": "chat_eng_sync",
            "source_type": "CHAT",
            "subject": "Engineering Core: Architecture Refactor",
            "content": "Architecture refactor milestone and design doc review.",
            "lexical_rank": 1,
            "vector_rank": 2,
            "rrf_score": 0.032,
            "normalized_rrf": 0.98,
            "relevance_tier": "HIGH",
            "cosine_similarity": 0.88,
        },
        "query_text": "Architecture refactor",
    }
    chat_exp = explain_match_tier1(
        fts_db_path=app_data / "lexical_fts.db",
        catalog=catalog,
        cas=cas,
        calendar_manager=cal_manager,
        rrf_k=float(model_cfg.rrf_k),
        payload=chat_payload,
    )
    assert chat_exp["success"] is True
    assert chat_exp["document"]["source_type"] == "CHAT"
    assert chat_exp["document"]["chat_meta"] is not None
    assert chat_exp["document"]["chat_meta"]["adapter_type"] == "teams"

    # Test Explainer on CALENDAR hit
    cal_payload = {
        "score_snapshot": {
            "chunk_id": "doc_cal_arch_review_0000",
            "doc_id": "doc_cal_arch_review",
            "source_type": "CALENDAR",
            "subject": "Architecture Refactor Sync",
            "content": "Sync meeting to discuss architecture refactor.",
            "lexical_rank": 2,
            "vector_rank": 1,
            "rrf_score": 0.031,
            "normalized_rrf": 0.95,
            "relevance_tier": "HIGH",
            "cosine_similarity": 0.91,
        },
        "query_text": "Architecture refactor",
    }
    cal_exp = explain_match_tier1(
        fts_db_path=app_data / "lexical_fts.db",
        catalog=catalog,
        cas=cas,
        calendar_manager=cal_manager,
        rrf_k=float(model_cfg.rrf_k),
        payload=cal_payload,
    )
    assert cal_exp["success"] is True
    assert cal_exp["document"]["source_type"] == "CALENDAR"
    assert cal_exp["document"]["calendar_meta"] is not None
    assert cal_exp["document"]["calendar_meta"]["location"] == "Room 3B"


def test_mcp_server_tools_and_cross_type_search(monkeypatch, tmp_path: Path):
    from old_rag.mcp_server import (
        list_calendar_events,
        list_chat_messages,
        list_chat_rooms,
        query_personal_archive,
    )

    # Mock _dispatch_ipc to return deterministic structures
    def mock_ipc(payload):
        action = payload.get("action")
        if action == "search":
            return {"results": [{"doc_id": "mcp_doc_1", "source_type": "CHAT", "content": "Found in chat"}]}
        elif action == "list_chat_rooms":
            return {"rooms": [{"room_id": "r1", "room_name": "General"}]}
        elif action == "list_chat_messages":
            return {"messages": [{"message_id": "m1", "body_text": "Hello MCP"}]}
        elif action == "list_calendar_events":
            return {"events": [{"event_id": "e1", "subject": "Standup"}]}
        return {"error": "unknown"}

    monkeypatch.setattr("old_rag.mcp_server._dispatch_ipc", mock_ipc)

    res_search = json.loads(query_personal_archive("test", source_types=["CHAT"]))
    assert len(res_search["results"]) == 1
    assert res_search["results"][0]["source_type"] == "CHAT"

    res_rooms = json.loads(list_chat_rooms())
    assert len(res_rooms["rooms"]) == 1
    assert res_rooms["rooms"][0]["room_id"] == "r1"

    res_msgs = json.loads(list_chat_messages("r1"))
    assert len(res_msgs["messages"]) == 1
    assert res_msgs["messages"][0]["body_text"] == "Hello MCP"

    res_cal = json.loads(list_calendar_events())
    assert len(res_cal["events"]) == 1
    assert res_cal["events"][0]["subject"] == "Standup"
