"""End-to-end user-validation tests spanning Scryer, OldRAG CAS/Catalog v2, FTS5, LanceDB, CLI, Daemon, and Folder Index."""

from __future__ import annotations

from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import docx
import numpy as np
import openpyxl
from PIL import Image
import pptx
import pypdfium2 as pdfium
import pytest

from old_rag.cli import main as old_rag_cli_main
from old_rag.contracts import ModelConfig, SearchQuery, StorageConfig
from old_rag.engine.chunker import ParentAwareChunker
from old_rag.engine.extractor import DocumentExtractor, build_scryer_options_from_settings
from old_rag.engine.folder_index import HeadOfFolderIndex
from old_rag.engine.indexer import IndexEngine, open_lancedb_table
from old_rag.engine.search import HybridSearchEngine
from old_rag.ingest.watcher import ingest_filesystem_path
from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.storage.cas import CASEngine
from old_rag.storage.catalog import CatalogRepository
from scryer.bridge import BaseRouterBridge, BridgeResponse, TriModeBridge
from scryer.contracts import TriMode
from scryer.extractor import ScryerExtractor


class _DeterministicHashEmbedder:
    """Fast deterministic 64-dim bag-of-trigrams embedder matching ModelResolver interface."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, texts: List[str]):
        for txt in texts:
            vec = np.zeros(self.dim, dtype=np.float32)
            clean = (txt or "").lower()
            if not clean:
                vec[0] = 1.0
            else:
                for token in clean.split():
                    h = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
                    vec[h % self.dim] += 1.0
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            yield vec


class _DeterministicResolver:
    def __init__(self, dim: int = 64):
        self._embedder = _DeterministicHashEmbedder(dim=dim)

    def get_text_embedding_model(self):
        return self._embedder


class _MockScryerBridge(BaseRouterBridge):
    """Simulates AuraRouter Phase 2 responses per document/image."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    def execute_multimodal(
        self,
        task: str,
        images: Any,
        response_schema: Optional[Dict[str, Any]] = None,
        intent: Optional[str] = None,
        context: Optional[str] = None,
        execution_mode: Optional[str] = None,
        route_override: Optional[str] = None,
        prefer_local: Optional[bool] = None,
    ) -> BridgeResponse:
        self.calls.append({"task": task, "images": len(images or []), "intent": intent})
        first_elem_id = str(getattr(images[0], "element_id", "")) if images else ""
        combined = f"{task}\n{context or ''}"
        if "Visual/Scanned" in combined or "q4_report.pdf" in first_elem_id:
            payload = {
                "vendor_name": "Northstar Defense",
                "contract_code": "ND-2026-Q4",
                "extracted_text": "Scanned Q4 Compliance Audit signed by Director Vega.",
            }
        elif "architecture_diagram" in first_elem_id or "diagram_chart" in first_elem_id:
            payload = {
                "setting": "cloud_datacenter",
                "detected_objects": ["gateway_firewall", "lancedb_cluster"],
                "summary": "Core telemetry pipeline diagram routing gateway_firewall logs into lancedb_cluster.",
            }
        else:
            payload = {
                "summary": "Warehouse rack photo showing pallet RACK-88B and safety signage.",
                "asset_tag": "RACK-88B",
                "zone": "Cold-Storage-East",
            }
        return BridgeResponse(
            raw_output=json.dumps(payload),
            structured_output=payload,
            intent=intent or "VISION_EXTRACTION",
            model_id="mock-qwen2.5-vl",
            provider="local",
            success=True,
        )


def _create_sample_user_corpus(root: Path) -> Path:
    corpus = root / "user_docs"
    corpus.mkdir(parents=True, exist_ok=True)

    # 1. Scanned / low-text PDF
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(300, 300)
    with open(corpus / "q4_report.pdf", "wb") as f:
        pdf.save(f)
    pdf.close()

    # 2. DOCX with paragraph + table
    doc = docx.Document()
    doc.core_properties.author = "Legal Ops"
    doc.core_properties.title = "Vendor Agreement"
    doc.add_paragraph("Master services agreement with Apex Photonics.")
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "Milestone"
    tbl.cell(0, 1).text = "Fee"
    tbl.cell(1, 0).text = "DELIV-ALPHA"
    tbl.cell(1, 1).text = "$12,500"
    doc.save(corpus / "vendor_agreement.docx")

    # 3. XLSX workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Financials"
    ws.append(["CostCenter", "BudgetUSD"])
    ws.append(["CC-404-GPU", 48000])
    wb.save(corpus / "budget_2026.xlsx")
    wb.close()

    # 4. PPTX slide + speaker notes
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Sovereign Mesh Rollout"
    slide.placeholders[1].text = "Phase 2 validation targets air-gapped workstations."
    slide.notes_slide.notes_text_frame.text = "Speaker note: confirm ORBIT-991 firmware before cutover."
    prs.save(corpus / "architecture_deck.pptx")

    # 5. Standalone PNG image
    img = Image.new("RGB", (180, 120), color=(30, 90, 160))
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    png_bytes = img_buf.getvalue()
    (corpus / "warehouse_rack.png").write_bytes(png_bytes)

    # 6. EML email with PNG attachment
    msg = EmailMessage()
    msg["Subject"] = "Incident Follow-up INC-7721"
    msg["From"] = "ops@example.com"
    msg["To"] = "sre@example.com"
    msg.set_content("Attached diagram shows the gateway firewall topology.")
    msg.add_attachment(png_bytes, maintype="image", subtype="png", filename="architecture_diagram.png")
    (corpus / "incident_update.eml").write_bytes(msg.as_bytes())

    # 7. Ignored files (temp Office lock, hidden folder, unsupported binary)
    (corpus / "~$lock_file.docx").write_bytes(b"lock")
    (corpus / "unsupported.bin").write_bytes(b"\x00\x01\x02\x03")
    hidden_dir = corpus / ".oldrag_ignore"
    hidden_dir.mkdir(exist_ok=True)
    (hidden_dir / "hidden.txt").write_text("do not index", encoding="utf-8")

    return corpus


def test_e2e_build_scryer_options_from_settings():
    default_opts = build_scryer_options_from_settings(
        {
            "aurarouter": {
                "enabled": True,
                "mode": "auto",
                "role": "assist",
                "local_only": True,
                "timeout_sec": 45.0,
            }
        }
    )
    assert default_opts.enable_phase2 is True
    assert default_opts.tri_mode == TriMode.PYTHON
    assert default_opts.prefer_local is True
    assert default_opts.timeout_seconds == 45.0
    assert default_opts.route_override is None

    ipc_opts = build_scryer_options_from_settings(
        {
            "aurarouter": {
                "enabled": True,
                "mode": "mcp_http",
                "endpoint_url": "http://127.0.0.1:8765/mcp",
                "config_path": "C:/cfg/auraconfig.yaml",
                "vision_role": "vision_heavy",
                "local_only": False,
                "timeout_sec": None,
            }
        }
    )
    assert ipc_opts.tri_mode == TriMode.IPC
    assert ipc_opts.mcp_url == "http://127.0.0.1:8765/mcp"
    assert ipc_opts.aurarouter_config_path.replace("\\", "/") == "C:/cfg/auraconfig.yaml"
    assert ipc_opts.route_override == "vision_heavy"
    assert ipc_opts.prefer_local is False
    assert ipc_opts.timeout_seconds == 60.0

    mas_opts = build_scryer_options_from_settings({"aurarouter": {"enabled": False, "mode": "mas"}})
    assert mas_opts.enable_phase2 is False
    assert mas_opts.tri_mode == TriMode.MAS


def test_e2e_multi_format_directory_ingest_entities_lancedb_and_hybrid_search(
    temp_app_dir: Path, storage_config: StorageConfig, cas_engine: CASEngine, catalog_repo: CatalogRepository
):
    corpus_dir = _create_sample_user_corpus(temp_app_dir)
    dim = 64
    model_cfg = ModelConfig(matryoshka_dim=dim)
    resolver = _DeterministicResolver(dim=dim)
    lancedb_table = open_lancedb_table(temp_app_dir, dim=dim)
    assert lancedb_table is not None

    stub_bridge = _MockScryerBridge()
    scryer = ScryerExtractor(bridge=TriModeBridge(in_process=stub_bridge))
    extractor = DocumentExtractor(catalog=catalog_repo, scryer_extractor=scryer)
    chunker = ParentAwareChunker(catalog=catalog_repo)
    indexer = IndexEngine(
        fts_db_path=temp_app_dir / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
        resolver=resolver,
    )
    coord = PipelineCoordinator(
        cas=cas_engine,
        catalog=catalog_repo,
        extractor=extractor,
        chunker=chunker,
        indexer=indexer,
    )

    stats = ingest_filesystem_path(corpus_dir, coord, recursive=True)
    assert stats["ingested"] == 6
    assert stats["errors"] == 0
    assert stats["skipped"] >= 3
    assert lancedb_table.count_rows() >= 6

    # Verify structured entities persisted in Catalog v2
    all_entities: List[Dict[str, Any]] = []
    for doc_id in stats["doc_ids"]:
        all_entities.extend(catalog_repo.get_document_entities(doc_id))
    entity_keys = {row["entity_key"] for row in all_entities}
    entity_vals = {row["entity_value"] for row in all_entities}
    assert "asset_tag" in entity_keys or "vendor_name" in entity_keys
    assert "RACK-88B" in entity_vals or "Northstar Defense" in entity_vals

    # Verify hybrid retrieval finds DOCX table, XLSX sheet, PPTX notes, EML, PDF, and PNG context
    search_engine = HybridSearchEngine(
        fts_db_path=temp_app_dir / "lexical_fts.db",
        lancedb_table=lancedb_table,
        config=model_cfg,
        resolver=resolver,
        catalog=catalog_repo,
    )

    for q_text, expected_substr in (
        ("DELIV-ALPHA", "vendor_agreement.docx"),
        ("CC-404-GPU", "budget_2026.xlsx"),
        ("ORBIT-991", "architecture_deck.pptx"),
        ("INC-7721", "incident_update.eml"),
        ("ND-2026-Q4", "q4_report.pdf"),
        ("RACK-88B", "warehouse_rack.png"),
    ):
        hits = search_engine.search(SearchQuery(query_text=q_text, top_k=5))
        assert len(hits) >= 1, f"Expected search hit for {q_text}"
        matched_paths = [str(catalog_repo.get_document(h.doc_id).get("original_path", "")) for h in hits]
        assert any(expected_substr in p for p in matched_paths), (
            f"Expected '{expected_substr}' in {matched_paths} for query '{q_text}'"
        )

    # Verify offline reindex preserves LanceDB rows and entities
    reindexed = coord.reindex_all()
    assert reindexed == 6
    assert lancedb_table.count_rows() >= 6


def test_e2e_folder_index_daemon_and_cli_ingest_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    local_app = tmp_path / "localappdata"
    local_app.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    corpus_dir = _create_sample_user_corpus(tmp_path)

    # 1. HeadOfFolderIndex uses Scryer Phase 1 for binary documents instead of raw binary decoding
    f_index = HeadOfFolderIndex(corpus_dir)
    scan_res = f_index.scan_and_index(recursive=False)
    assert scan_res["indexed_count"] >= 4

    docx_hits = f_index.search("DELIV-ALPHA")
    assert any("vendor_agreement.docx" in h["rel_path"] for h in docx_hits)
    xlsx_hits = f_index.search("Financials")
    assert any("budget_2026.xlsx" in h["rel_path"] for h in xlsx_hits)

    # 2. DaemonProcess IPC handles save_settings + ingest_path cleanly
    app_dir = tmp_path / "daemon_home"
    daemon = DaemonProcess(
        storage_cfg=StorageConfig(app_data_dir=str(app_dir)),
        model_cfg=ModelConfig(matryoshka_dim=64),
    )
    # Use deterministic resolver so Daemon test avoids loading full ONNX weights
    daemon.indexer.resolver = _DeterministicResolver(dim=64)
    daemon.search_engine.resolver = _DeterministicResolver(dim=64)

    save_resp = json.loads(
        daemon.handle_client_request(
            json.dumps(
                {
                    "action": "save_settings",
                    "settings": {
                        "storage_location": str(app_dir),
                        "aurarouter": {
                            "enabled": False,
                            "mode": "auto",
                            "local_only": True,
                            "timeout_sec": 25.0,
                        },
                    },
                }
            )
        )
    )
    assert save_resp["success"] is True
    assert daemon.extractor.default_options.enable_phase2 is False
    assert daemon.extractor.default_options.timeout_seconds == 25.0

    ing_resp = json.loads(
        daemon.handle_client_request(
            json.dumps({"action": "ingest_path", "path": str(corpus_dir), "recursive": True})
        )
    )
    assert ing_resp["success"] is True
    assert ing_resp["ingested"] == 6

    # 3. CLI dispatch: old-rag ingest --path <single_docx>
    monkeypatch.setattr("old_rag.storage.settings.save_settings", lambda s: None)
    monkeypatch.setattr(
        "old_rag.engine.indexer.IndexEngine._get_embedder",
        lambda self: _DeterministicHashEmbedder(dim=768),
    )
    monkeypatch.setattr(
        sys_argv_target := __import__("sys"),
        "argv",
        ["old-rag", "ingest", "--path", str(corpus_dir / "vendor_agreement.docx")],
    )
    assert sys_argv_target.argv[1] == "ingest"
    old_rag_cli_main()
