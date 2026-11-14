# old_rag

Zero-admin sovereign local Outlook and filesystem hybrid RAG engine powered by **Scryer** (`>=0.2.0`) and **AuraRouter** (`>=0.5.8`).

## Architecture & Core Invariants

1. **Sovereign Local Execution**: Runs 100% locally on standard Windows workstations without administrative privileges.
2. **Unified Extraction via Scryer**:
   - Deterministic Phase 1 parsing and AuraRouter Phase 2 multi-modal enrichment run through `scryer` (`old_rag.engine.extractor.DocumentExtractor`) across `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.eml`, `.msg`, images (`.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`), and plain-text formats.
   - `settings.json["aurarouter"]` maps directly into `ScryerExtractionOptions` (`enabled`, `mode`, `endpoint_url`, `config_path`, `local_only`, `timeout_sec`, and optional `vision_role`).
   - `HeadOfFolderIndex` (`old_rag.engine.folder_index`) uses Scryer Phase 1 deterministic extraction so shared-folder scans index real text from binary documents rather than raw binary bytes.
3. **Multi-Process Runtime**:
   - `old-rag daemon`: Background coordinator handling MAPI polling, `DirectoryWatcher` filesystem monitoring, two-phase outbox recovery, FTS5 + LanceDB indexing, and local named-pipe IPC (`ingest_path`, `search`, `status`, `reindex`, `save_settings`, etc.).
   - `old-rag ui`: Native `pywebview` companion overlay communicating over local named pipes with in-process fallback (zero listening HTTP sockets).
   - `old-rag mcp`: FastMCP stdio server exposing archive search and retrieval tools to AuraRouter or external agents.
4. **Envelope Encrypted CAS**:
   - Master Data Encryption Key (DEK) protected via Windows DPAPI (`win32crypt.CryptProtectData`).
   - Three-tier content-addressable storage (`texts/`, `blobs/`, `msgs/`) using AES-256-GCM with HMAC-SHA256 obfuscated filenames.
   - Normalized `NormalizedDocumentJSON` archives enable 100% offline re-indexing without re-reading Outlook or re-running VLMs.
5. **Centralized Schemas (`old_rag.schema`, Schema v2)**:
   - SQLite `catalog.db` (`CURRENT_SCHEMA_VERSION = 2`) with composite-keyed `vlm_cache (blob_hash, template_key, schema_hash)` and searchable `document_entities (doc_id, entity_key, entity_value, source_filename)` tables, plus automatic v1 -> v2 migration.
   - Dual SQLite FTS5 lexical indexes (`chunks_fts_porter` & `chunks_fts_trigram`) paired with Matryoshka-reduced (`nomic-embed-text-v1.5`) vectors in LanceDB (`chunks_vec`).

---

## Installation & Testing

Requires **Python 3.12+** and installed `scryer` + `aurarouter`.

```bash
pip install -e .
```

Run the unit and E2E test suite:

```bash
pytest -q
```

---

## CLI Usage

```bash
# Initialize DPAPI-protected DEK, CAS storage, and catalog schema
old-rag init

# Start background coordinator daemon + named pipe IPC + configured DirectoryWatcher
old-rag daemon

# Ingest a local directory or single file (PDF, DOCX, XLSX, PPTX, EML, MSG, images)
old-rag ingest --path "C:\Users\you\Documents\Reports"

# Ingest only direct children of a folder (non-recursive)
old-rag ingest --path "C:\Users\you\Documents\Invoices" --no-recursive

# Ingest Classic Outlook MAPI mail for the last 30 days
old-rag ingest --days 30 --folder Inbox

# Query status, trigger offline re-index, or purge zero-ref blobs
old-rag status
old-rag reindex
old-rag purge

# Launch UI overlay or FastMCP stdio server
old-rag ui
old-rag mcp
```

---

## AuraRouter Configuration (`settings.json`)

In `%LOCALAPPDATA%\OldRAG\settings.json`:

```json
{
  "aurarouter": {
    "enabled": true,
    "mode": "auto",
    "endpoint_url": "",
    "config_path": "",
    "role": "assist",
    "local_only": true,
    "timeout_sec": 30.0
  }
}
```

- `enabled`: Enables/disables Scryer Phase 2 VLM/LLM enrichment (`enable_phase2`).
- `mode`: `"auto"` / `"embedded"` (`TriMode.PYTHON`), `"mcp_http"` / `"ipc"` (`TriMode.IPC`), or `"mas"` (`TriMode.MAS`).
- `local_only`: Requests sovereign local model routing (`prefer_local`).
- `config_path`: Optional explicit `auraconfig.yaml` override (`aurarouter_config_path`).
- `vision_role` (optional): Explicit AuraRouter role override for Phase 2 visual extraction (falls back to Scryer's default `"vision_extraction"` routing when unset or when `role` is `"assist"`).
