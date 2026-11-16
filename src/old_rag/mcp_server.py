"""mcp_server.py - FastMCP Server Exposing OldRAG over Stdio IPC & Win32 Named Pipe Thin Client."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from old_rag.contracts import safe_json_dumps, safe_json_loads
from old_rag.pipeline.lifecycle import NamedPipeClient
from old_rag.storage.cas import CASEngine, get_default_app_data_dir
from old_rag.storage.catalog import CatalogRepository

mcp = FastMCP("OldRAG")


def _dispatch_ipc(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Route tool invocation over Named Pipe to daemon; fallback to in-process read-only."""
    client = NamedPipeClient()
    res = client.call(payload, timeout_ms=10000)

    # Check if pipe succeeded
    if "error" not in res or not str(res.get("error", "")).startswith("Named Pipe"):
        return res

    # Fallback to read-only in-process execution
    action = payload.get("action")
    app_data_dir = get_default_app_data_dir()

    if action in ("search", "query_personal_archive"):
        from old_rag.contracts import SearchQuery
        from old_rag.engine.search import HybridSearchEngine

        q_dict = payload.get("query", {})
        q = SearchQuery.model_validate(q_dict)
        fts_db = app_data_dir / "lexical_fts.db"
        searcher = HybridSearchEngine(fts_db)
        results = searcher.search(q)
        return {"results": [r.model_dump() for r in results]}

    elif action == "get_archived_document":
        doc_id = payload.get("doc_id", "")
        cas = CASEngine()
        norm_doc = cas.get_text(doc_id)
        if norm_doc:
            return {"document": norm_doc.model_dump()}
        return {"error": f"Document {doc_id} not found in archive"}

    elif action == "list_recent_threads":
        limit = int(payload.get("limit", 20))
        cat = CatalogRepository(app_data_dir / "catalog.db")
        conn = cat._get_connection()
        cur = conn.execute(
            """
            SELECT doc_id, subject, sender, folder, received_ts_utc, conversation_id
            FROM documents
            WHERE is_deleted = 0
            ORDER BY received_ts_utc DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        return {"threads": rows}

    elif action == "open_cached_msg":
        doc_id = payload.get("doc_id", "")
        cas = CASEngine()
        temp_path = cas.create_temp_view_file(doc_id)
        if temp_path:
            return {"success": True, "temp_view_path": str(temp_path)}
        return {"success": False, "error": "Not found in CAS"}

    elif action == "list_chat_rooms":
        cat = CatalogRepository(app_data_dir / "catalog.db")
        rooms = cat.list_chat_rooms(account_id=payload.get("account_id"))
        return {"rooms": [r.model_dump(mode="json") for r in rooms]}

    elif action == "list_chat_messages":
        cat = CatalogRepository(app_data_dir / "catalog.db")
        since_str = payload.get("since_utc")
        since_dt = datetime.fromisoformat(since_str) if since_str else None
        msgs = cat.list_chat_messages(
            room_id=payload.get("room_id", ""),
            since_utc=since_dt,
            limit=int(payload.get("limit", 50)),
        )
        return {"messages": [m.model_dump(mode="json") for m in msgs]}

    elif action == "list_calendar_events":
        from old_rag.ingest.calendar import CalendarManager
        cm = CalendarManager(app_data_dir / "catalog.db")
        events = cm.list_events()
        return {"events": [e.model_dump(mode="json") for e in events[:int(payload.get("limit", 50))]]}

    return {"error": f"Unknown action {action}"}


@mcp.tool()
def query_personal_archive(
    query: str,
    top_k: int = 10,
    folder: Optional[str] = None,
    sender: Optional[str] = None,
    source_types: Optional[List[str]] = None,
) -> str:
    """Search personal email, file, chat, and calendar archive using hybrid FTS5 Porter/Trigram and vector search.
    
    Args:
        query: Search keywords, question, or serial number / identifier (e.g. SN-4471-AB).
        top_k: Maximum number of results to return (default 10).
        folder: Optional Outlook folder filter (e.g. Inbox, Sent).
        sender: Optional sender email or display name filter.
        source_types: Optional list of source types to filter (e.g. ["EMAIL", "FILE", "CALENDAR", "CHAT"]).
    """
    req = {
        "action": "search",
        "query": {
            "query_text": query,
            "top_k": top_k,
            "folder": folder,
            "sender": sender,
            "source_types": source_types,
        },
    }
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def list_chat_rooms(account_id: Optional[str] = None) -> str:
    """List chat channels, rooms, and direct message threads across registered chat connectors.
    
    Args:
        account_id: Optional chat account ID filter.
    """
    req = {"action": "list_chat_rooms", "account_id": account_id}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def list_chat_messages(
    room_id: str,
    limit: int = 50,
    since_utc: Optional[str] = None,
) -> str:
    """Retrieve chronological messages from a chat room or DM thread.
    
    Args:
        room_id: Identifier of the room or channel.
        limit: Maximum number of messages to return (default 50).
        since_utc: Optional ISO 8601 UTC timestamp filter.
    """
    req = {"action": "list_chat_messages", "room_id": room_id, "limit": limit, "since_utc": since_utc}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def list_calendar_events(limit: int = 50) -> str:
    """List upcoming and past calendar meetings, events, and appointments from local calendar store.
    
    Args:
        limit: Maximum number of events to return (default 50).
    """
    req = {"action": "list_calendar_events", "limit": limit}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def get_archived_document(doc_id: str) -> str:
    """Retrieve full extracted text, subject, sender, and metadata for an archived email or file.
    
    Args:
        doc_id: Unique document identifier returned by query_personal_archive.
    """
    req = {"action": "get_archived_document", "doc_id": doc_id}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def list_recent_threads(limit: int = 20) -> str:
    """List recent conversation threads and messages from the local archive.
    
    Args:
        limit: Number of recent items to return (default 20).
    """
    req = {"action": "list_recent_threads", "limit": limit}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


@mcp.tool()
def open_cached_msg(doc_id: str) -> str:
    """Decrypt and stage a cached Outlook .msg file to a temporary location for external opening.
    
    Args:
        doc_id: Unique document identifier for the email message.
    """
    req = {"action": "open_cached_msg", "doc_id": doc_id}
    resp = _dispatch_ipc(req)
    return safe_json_dumps(resp, indent=2)


def main():
    """Run FastMCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

