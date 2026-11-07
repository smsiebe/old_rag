# old_rag

Zero-admin sovereign local Outlook and filesystem hybrid RAG engine.

## Core Invariants

1. **Sovereign Execution**: Runs 100% locally on standard Windows workstations.
2. **Multi-Process Architecture**:
   - `old-rag daemon`: Core coordination, MAPI poller (STA thread), filesystem watcher, and local named pipe IPC.
   - `old-rag vlm-worker`: Isolated subprocess running PaddleOCR-VL at `BELOW_NORMAL_PRIORITY_CLASS`.
   - `old-rag ui`: Native pywebview overlay companion bound to local bridge (`file://`, zero listening HTTP sockets).
3. **Envelope Encrypted CAS**:
   - Master Data Encryption Key (DEK) protected via Windows DPAPI (`win32crypt.CryptProtectData`).
   - 3-tier content-addressable storage using AES-256-GCM with HMAC-SHA256 obfuscated paths.
   - Text JSON tier stores canonical extraction output for offline re-indexing without touching Outlook or VLM.
4. **Centralized Schemas**:
   - Explicit Phase 0 DDL in `old_rag.schema` for `catalog.db` and SQLite FTS5 indexers.

