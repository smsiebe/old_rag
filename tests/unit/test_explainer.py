"""test_explainer.py - Tests for Two-Stage Lazy Relevance & Scoring Explanations (Tier 1 + Tier 2 AuraRouter)."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from old_rag.contracts import CalendarEvent, SourceType
from old_rag.engine.explainer import MatchExplainer, explain_match_tier1
from old_rag.ingest.calendar import CalendarManager
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.pipeline.lifecycle import NamedPipeServer
from old_rag.schema import CATALOG_SCHEMA_DDL, FTS_SCHEMA_DDL
from old_rag.ui.app import BridgeAPI


@pytest.fixture
def sample_fts_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "lexical_fts.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(FTS_SCHEMA_DDL)
    rows = [
        (
            "chunk_dish_1",
            "doc_dish_1",
            "All dirty dishes must be placed in the commercial dishwasher before closing shift.",
            "chef@kitchen.local",
            "Inbox",
            "EMAIL",
            "2026-06-01T10:00:00Z",
        ),
        (
            "chunk_id_2",
            "doc_id_2",
            "Quality alert: please inspect module SN-4471-AB before customer dispatch.",
            "qa@factory.local",
            "Alerts",
            "EMAIL",
            "2026-06-02T14:30:00Z",
        ),
        (
            "chunk_vec_3",
            "doc_vec_3",
            "Annual commercial property occupancy terms and monthly rent schedule.",
            "legal@property.local",
            "Contracts",
            "FILE",
            "2026-06-03T09:15:00Z",
        ),
    ]
    for r in rows:
        conn.execute(
            """
            INSERT INTO chunks_fts_porter (chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            r,
        )
        conn.execute(
            """
            INSERT INTO chunks_fts_trigram (chunk_id, doc_id, content, sender, folder, source_type, received_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            r,
        )
    conn.commit()
    conn.close()
    return db_path


def test_tier1_hybrid_synergy_and_porter_stemming(sample_fts_db: Path):
    res = explain_match_tier1(
        fts_db_path=sample_fts_db,
        rrf_k=60.0,
        payload={
            "item_kind": "search_hit",
            "query_text": "give me stuff about dish",
            "doc_id": "doc_dish_1",
            "chunk_id": "chunk_dish_1",
            "source_context": {"surface": "global_search"},
            "score_snapshot": {
                "doc_id": "doc_dish_1",
                "chunk_id": "chunk_dish_1",
                "source_type": "EMAIL",
                "subject": "Kitchen Closing Checklist",
                "sender": "chef@kitchen.local",
                "folder": "Inbox",
                "lexical_rank": 1,
                "vector_rank": 2,
                "cosine_similarity": 0.69,
                "rrf_score": (1.0 / 61.0) + (1.0 / 62.0),
            },
        },
    )

    assert res["success"] is True
    assert res["item_kind"] == "search_hit"
    assert "FTS5_PORTER" in res["channels_matched"]
    assert "LANCEDB_VECTOR" in res["channels_matched"]
    assert "dish" in res["lexical_evidence"]["matched_tokens"]
    assert "give" in res["query_analysis"]["stopwords_removed"]
    assert "<mark>dishes</mark>" in res["passage"]["highlighted_html"]
    assert res["lexical_evidence"]["bm25_score"] is not None

    scoring = res["scoring"]
    assert scoring["calibration_rule"] == "Dual-Engine Synergy Boost"
    assert scoring["relevance_tier"] == "HIGH"
    assert scoring["confidence_score"] >= 0.65
    assert scoring["semantic_strength"] == round((0.69 - 0.40) / 0.32, 4)

    # Strictly JSON-serializable via standard json.dumps
    json.dumps(res)


def test_tier1_exact_identifier_pinning_and_trigram(sample_fts_db: Path):
    res = explain_match_tier1(
        fts_db_path=sample_fts_db,
        rrf_k=60.0,
        payload={
            "item_kind": "search_hit",
            "query_text": "SN-4471-AB",
            "doc_id": "doc_id_2",
            "chunk_id": "chunk_id_2",
            "score_snapshot": {
                "doc_id": "doc_id_2",
                "chunk_id": "chunk_id_2",
                "source_type": "EMAIL",
                "exact_identifier_match": True,
                "lexical_rank": 1,
                "confidence_score": 1.0,
                "relevance_tier": "HIGH",
            },
        },
    )

    assert res["success"] is True
    assert "TRIGRAM_EXACT_ID" in res["channels_matched"]
    assert "SN-4471-AB" in res["lexical_evidence"]["matched_identifiers"]
    assert res["lexical_evidence"]["trigram_matched"] is True
    assert res["scoring"]["calibration_rule"] == "Exact Identifier Pinning"
    assert res["scoring"]["confidence_pct"] == 100
    assert "<mark>SN-4471-AB</mark>" in res["passage"]["highlighted_html"]
    assert "Trigram" in res["lexical_evidence"]["strategy"]


def test_tier1_semantic_vector_only_explanation(sample_fts_db: Path):
    res = explain_match_tier1(
        fts_db_path=sample_fts_db,
        rrf_k=60.0,
        payload={
            "item_kind": "search_hit",
            "query_text": "housing rental contract",
            "doc_id": "doc_vec_3",
            "chunk_id": "chunk_vec_3",
            "score_snapshot": {
                "doc_id": "doc_vec_3",
                "chunk_id": "chunk_vec_3",
                "source_type": "FILE",
                "vector_rank": 1,
                "cosine_similarity": 0.67,
            },
        },
    )

    assert res["success"] is True
    assert res["channels_matched"] == ["LANCEDB_VECTOR"]
    assert "Semantic Vector Only" in res["lexical_evidence"]["strategy"]
    assert res["scoring"]["calibration_rule"] == "Semantic Cosine Calibration"
    assert res["scoring"]["relevance_tier"] == "HIGH"
    assert any("conceptual vector similarity" in b for b in res["summary_bullets"])


def test_tier1_json_safe_datetime_and_path_inputs(sample_fts_db: Path):
    res = explain_match_tier1(
        fts_db_path=sample_fts_db,
        payload={
            "item_kind": "search_hit",
            "query_text": "rent schedule",
            "doc_id": "doc_vec_3",
            "chunk_id": "chunk_vec_3",
            "source_context": {
                "surface": "ambient_reading_pane",
                "timestamp": datetime(2026, 6, 3, 9, 15, tzinfo=timezone.utc),
            },
            "score_snapshot": {
                "doc_id": "doc_vec_3",
                "chunk_id": "chunk_vec_3",
                "source_type": SourceType.FILE,
                "original_path": Path("C:/Contracts/lease_schedule.pdf"),
                "received_ts_utc": datetime(2026, 6, 3, 9, 15, tzinfo=timezone.utc),
                "vector_rank": 2,
                "cosine_similarity": 0.62,
            },
        },
    )

    dumped = json.dumps(res)
    parsed = json.loads(dumped)
    assert parsed["document"]["source_type"] == "FILE"
    assert "lease_schedule.pdf" in parsed["document"]["original_path"]
    assert "2026-06-03" in parsed["document"]["received_ts_utc"]


def test_tier1_calendar_event_breakdown(tmp_path: Path):
    db_path = tmp_path / "catalog.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(CATALOG_SCHEMA_DDL)
    conn.commit()
    conn.close()

    cm = CalendarManager(db_path)
    cm.upsert_event(
        CalendarEvent(
            event_id="evt_lease_sign",
            account_id="acc_work",
            source_type=SourceType.EMAIL,
            subject="Commercial Lease Signing",
            location="Downtown Suite 400",
            start_ts_utc=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
            end_ts_utc=datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc),
            organizer="landlord@property.local",
            body_text="Bring countersigned addendum and security deposit check.",
        )
    )

    res = explain_match_tier1(
        calendar_manager=cm,
        payload={
            "item_kind": "calendar_event",
            "event_id": "evt_lease_sign",
            "query_text": "lease downtown addendum",
            "source_context": {
                "surface": "ambient_reading_pane",
                "origin_subject": "New Office Key Pickup",
            },
        },
    )

    assert res["success"] is True
    assert res["item_kind"] == "calendar_event"
    assert res["channels_matched"] == ["CALENDAR_SQL_LIKE"]
    assert res["scoring"]["confidence_pct"] == 100
    assert res["scoring"]["relevance_tier"] == "HIGH"
    assert "lease" in res["lexical_evidence"]["matched_fields"]["subject"]
    assert "downtown" in res["lexical_evidence"]["matched_fields"]["location"]
    assert "addendum" in res["lexical_evidence"]["matched_fields"]["body_text"]
    assert "<mark>Lease</mark>" in res["passage"]["highlighted_html"]
    json.dumps(res)


def test_tier2_embedded_connected_role_fallback_and_lru_cache():
    mock_loader = MagicMock()
    mock_loader.get_role_chain.side_effect = lambda role: ["local_gemma"] if role == "summarization" else []
    mock_loader.get_model_config.return_value = {"provider": "ollama", "endpoint": "http://127.0.0.1:11434/api/generate"}

    mock_fabric = MagicMock()
    mock_fabric.config = mock_loader
    mock_fabric.get_local_chain.side_effect = lambda role: ["local_gemma"] if role == "summarization" else []
    mock_fabric.execute.return_value = SimpleNamespace(
        text="This email matched your query because Porter stemming linked 'dish' to 'dishes', boosted by semantic vector proximity.\n<!-- _aura_routing_context: {\"route\": \"summarization\"} -->",
        model_id="gemma3:4b",
        provider="ollama",
        input_tokens=85,
        output_tokens=24,
    )

    explainer = MatchExplainer(
        settings_override={
            "enabled": True,
            "mode": "embedded",
            "role": "assist",
            "local_only": True,
            "timeout_sec": 10.0,
        },
        fabric=mock_fabric,
    )

    payload = {
        "item_kind": "search_hit",
        "query_text": "dish",
        "doc_id": "doc_dish_1",
        "chunk_id": "chunk_dish_1",
        "passage_text": "All dirty dishes must be washed.",
        "score_snapshot": {"confidence_score": 0.88, "relevance_tier": "HIGH", "lexical_rank": 1, "vector_rank": 2},
    }

    res1 = explainer.explain_tier2(payload)
    assert res1["success"] is True
    assert res1["role"] == "summarization"
    assert res1["model_id"] == "gemma3:4b"
    assert res1["local_only"] is True
    assert res1["cached"] is False
    assert "_aura_routing_context" not in res1["explanation"]
    assert "Porter stemming" in res1["explanation"]
    assert mock_fabric.execute.call_count == 1

    # Second identical call hits bounded LRU cache
    res2 = explainer.explain_tier2(payload)
    assert res2["success"] is True
    assert res2["cached"] is True
    assert res2["latency_ms"] == 0
    assert mock_fabric.execute.call_count == 1

    # Force refresh bypasses cache
    res3 = explainer.explain_tier2(payload, force_refresh=True)
    assert res3["success"] is True
    assert res3["cached"] is False
    assert mock_fabric.execute.call_count == 2


def test_tier2_graceful_degradation_paths(tmp_path: Path):
    base_payload = {"item_kind": "search_hit", "query_text": "lease", "doc_id": "doc_1"}

    # 1. Disabled in settings
    exp_disabled = MatchExplainer(settings_override={"enabled": False})
    r_dis = exp_disabled.explain_tier2(base_payload)
    assert r_dis["success"] is False
    assert r_dis["error_code"] == "DISABLED"

    # 2. Missing auraconfig.yaml
    with patch("aurarouter.config.ConfigLoader", side_effect=FileNotFoundError("auraconfig.yaml missing")):
        exp_missing = MatchExplainer(settings_override={"enabled": True, "mode": "embedded"})
        r_miss = exp_missing.explain_tier2(base_payload)
        assert r_miss["success"] is False
        assert r_miss["error_code"] == "NOT_CONFIGURED"

    # 3. Cloud-only models rejected when local_only=True
    cloud_loader = MagicMock()
    cloud_loader.get_role_chain.side_effect = lambda role: ["cloud_gpt"] if role == "assist" else []
    cloud_fabric = MagicMock()
    cloud_fabric.config = cloud_loader
    cloud_fabric.get_local_chain.return_value = []
    exp_cloud = MatchExplainer(
        settings_override={"enabled": True, "mode": "embedded", "local_only": True},
        fabric=cloud_fabric,
    )
    r_cloud = exp_cloud.explain_tier2(base_payload)
    assert r_cloud["success"] is False
    assert r_cloud["error_code"] == "NO_LOCAL_MODELS"

    # 4. Unreachable local Ollama endpoint fails fast (<2s) via health probe
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()

    dead_cfg = tmp_path / "auraconfig.yaml"
    dead_cfg.write_text(
        f"""
models:
  dead_ollama:
    provider: ollama
    model_name: gemma3:4b
    endpoint: http://127.0.0.1:{closed_port}/api/generate
    hosting_tier: local
roles:
  assist:
    - dead_ollama
""",
        encoding="utf-8",
    )
    exp_dead = MatchExplainer(
        settings_override={
            "enabled": True,
            "mode": "embedded",
            "config_path": str(dead_cfg),
            "timeout_sec": 2.0,
        }
    )
    t0 = time.monotonic()
    r_dead = exp_dead.explain_tier2(base_payload)
    elapsed = time.monotonic() - t0
    assert r_dead["success"] is False
    assert r_dead["error_code"] == "CONNECTION_ERROR"
    assert elapsed < 2.5

    # 5. mcp_http unreachable endpoint
    exp_http = MatchExplainer(
        settings_override={
            "enabled": True,
            "mode": "mcp_http",
            "endpoint_url": f"http://127.0.0.1:{closed_port}",
            "timeout_sec": 1.5,
        }
    )
    r_http = exp_http.explain_tier2(base_payload)
    assert r_http["success"] is False
    assert r_http["error_code"] in ("CONNECTION_ERROR", "TIMEOUT")

    # 6. ComputeFabric returns None, error text, or times out
    failing_loader = MagicMock()
    failing_loader.get_role_chain.return_value = ["m1"]
    failing_loader.get_model_config.return_value = {"provider": "ollama"}
    failing_fabric = MagicMock()
    failing_fabric.config = failing_loader
    failing_fabric.get_local_chain.return_value = ["m1"]

    failing_fabric.execute.return_value = None
    exp_fail = MatchExplainer(settings_override={"enabled": True, "mode": "embedded"}, fabric=failing_fabric)
    assert exp_fail.explain_tier2(base_payload, force_refresh=True)["error_code"] == "CONNECTION_ERROR"

    failing_fabric.execute.return_value = SimpleNamespace(text="ERROR: model failed to load")
    assert exp_fail.explain_tier2(base_payload, force_refresh=True)["error_code"] == "INFERENCE_ERROR"

    def _slow_exec(*args, **kwargs):
        time.sleep(1.5)
        return SimpleNamespace(text="too late")

    failing_fabric.execute.side_effect = _slow_exec
    exp_timeout = MatchExplainer(
        settings_override={"enabled": True, "mode": "embedded", "timeout_sec": 1.0},
        fabric=failing_fabric,
    )
    r_to = exp_timeout.explain_tier2(base_payload, force_refresh=True)
    assert r_to["success"] is False
    assert r_to["error_code"] == "TIMEOUT"


def test_tier2_mcp_http_roundtrip_success():
    class _MCPHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            req = json.loads(raw)
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req.get("id", 1),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "This record pinned at 100% due to exact serial token SN-4471-AB.",
                            }
                        ]
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _MCPHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        explainer = MatchExplainer(
            settings_override={
                "enabled": True,
                "mode": "mcp_http",
                "endpoint_url": f"http://127.0.0.1:{port}",
                "local_only": True,
                "timeout_sec": 5.0,
            }
        )
        res = explainer.explain_tier2({"query_text": "SN-4471-AB", "doc_id": "doc_id_2"})
        assert res["success"] is True
        assert res["transport"] == "mcp_http"
        assert res["model_id"] == "aurarouter:local_inference"
        assert "SN-4471-AB" in res["explanation"]
    finally:
        server.shutdown()
        server.server_close()


def test_daemon_and_bridge_ipc_explain_and_no_double_retry(temp_app_dir, storage_config):
    daemon = DaemonProcess(storage_cfg=storage_config)

    # Tier 1 over DaemonProcess.handle_client_request
    req1 = json.dumps(
        {
            "action": "explain_match_tier1",
            "item_kind": "search_hit",
            "query_text": "turbine",
            "doc_id": "doc_x",
            "score_snapshot": {
                "doc_id": "doc_x",
                "chunk_id": "chunk_x",
                "source_type": "EMAIL",
                "content": "Routine <mark>turbine</mark> inspection log.",
                "lexical_rank": 1,
            },
        }
    )
    res1 = json.loads(daemon.handle_client_request(req1))
    assert res1["success"] is True
    assert res1["scoring"]["lexical_rank"] == 1

    # Tier 2 over DaemonProcess.handle_client_request (mocking daemon.explainer)
    with patch.object(
        daemon.explainer,
        "explain_tier2",
        return_value={
            "success": True,
            "explanation": "Matched turbine log.",
            "model_id": "gemma3:4b",
            "cached": False,
        },
    ):
        req2 = json.dumps({"action": "explain_match_tier2", "query_text": "turbine", "doc_id": "doc_x"})
        res2 = json.loads(daemon.handle_client_request(req2))
        assert res2["success"] is True
        assert res2["model_id"] == "gemma3:4b"

    # BridgeAPI must NOT re-attempt in-process Tier 2 when connected daemon returns an application error
    test_pipe = "\\\\.\\pipe\\OldRAG_TestExplain_IPC"

    def _pipe_handler(raw_req: str) -> str:
        req = json.loads(raw_req)
        if req.get("action") == "explain_match_tier2":
            return json.dumps(
                {
                    "success": False,
                    "error_code": "CONNECTION_ERROR",
                    "error": "AuraRouter local provider unreachable from daemon",
                }
            )
        return json.dumps({"error": "Unknown action"})

    pipe_srv = NamedPipeServer(test_pipe, handler=_pipe_handler)
    pipe_srv.start()
    try:
        api = BridgeAPI(pipe_name=test_pipe)
        with patch.object(api, "_get_explainer") as spy_local_explainer:
            out = api.explain_match_tier2({"query_text": "lease", "doc_id": "doc_1"})
            assert out["success"] is False
            assert out["error_code"] == "CONNECTION_ERROR"
            assert "from daemon" in out["error"]
            spy_local_explainer.assert_not_called()
    finally:
        pipe_srv.stop()

    # In-process fallback when pipe does not exist
    api_offline = BridgeAPI(pipe_name="\\\\.\\pipe\\OldRAG_NonExistentExplain_Pipe")
    with patch.object(api_offline, "_get_explainer") as mock_get_exp:
        mock_exp = MagicMock()
        mock_exp.explain_tier2.return_value = {"success": False, "error_code": "DISABLED", "error": "Disabled"}
        mock_get_exp.return_value = mock_exp
        fallback_out = api_offline.explain_match_tier2({"query_text": "lease"})
        assert fallback_out["error_code"] == "DISABLED"
        mock_exp.explain_tier2.assert_called_once()


def test_ui_and_bridge_schema_explain_contract():
    src_root = Path(__file__).resolve().parents[2] / "src" / "old_rag"
    schema = json.loads((src_root / "bridge_schema.json").read_text(encoding="utf-8"))
    defs = schema["definitions"]
    for key in (
        "ExplainMatchTier1Request",
        "ExplainMatchTier1Response",
        "ExplainMatchTier2Request",
        "ExplainMatchTier2Response",
    ):
        assert key in defs

    static_dir = src_root / "ui" / "static"
    bridge_js = (static_dir / "bridge.js").read_text(encoding="utf-8")
    assert "explainMatchTier1" in bridge_js
    assert "explainMatchTier2" in bridge_js

    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    for elem_id in (
        "modal-explain",
        "explain-item-banner",
        "explain-tier1-content",
        "explain-tier2-meta",
        "explain-tier2-body",
        "explain-retry-btn",
    ):
        assert elem_id in index_html

    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    for token in ("data-explain-key", "openExplainModal", "renderExplainTier1", "triggerTier2Synthesis"):
        assert token in app_js

    styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
    assert ".modal-explain-overlay" in styles_css
    assert ".explain-progress-fill" in styles_css
