"""calendar.py - Sovereign Calendar Events Management, Outlook Sync & .ICS Support."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import CalendarEvent, SourceType


def parse_ics_datetime(val: str) -> Optional[datetime]:
    """Parse iCalendar DTSTART / DTEND strings (e.g. 20261107T140000Z or 20261107)."""
    if not val:
        return None
    clean = val.strip().replace("-", "").replace(":", "")
    try:
        if "T" in clean:
            if clean.endswith("Z"):
                return datetime.strptime(clean, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return datetime.strptime(clean[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return datetime.strptime(clean[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def format_ics_datetime(dt: datetime) -> str:
    """Format datetime as iCalendar UTC timestamp."""
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y%m%dT%H%M%SZ")


def parse_ics_content(ics_text: str, account_id: Optional[str] = None) -> List[CalendarEvent]:
    """Parse standard RFC 5545 iCalendar content into CalendarEvent list."""
    events: List[CalendarEvent] = []
    blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, re.DOTALL)

    for block in blocks:
        summary_m = re.search(r"^SUMMARY:(.*?)$", block, re.MULTILINE)
        dtstart_m = re.search(r"^DTSTART.*?:(.*?)$", block, re.MULTILINE)
        dtend_m = re.search(r"^DTEND.*?:(.*?)$", block, re.MULTILINE)
        loc_m = re.search(r"^LOCATION:(.*?)$", block, re.MULTILINE)
        desc_m = re.search(r"^DESCRIPTION:(.*?)$", block, re.MULTILINE)
        uid_m = re.search(r"^UID:(.*?)$", block, re.MULTILINE)
        organizer_m = re.search(r"^ORGANIZER.*?:(.*?)$", block, re.MULTILINE)

        subject = summary_m.group(1).strip() if summary_m else "Meeting"
        start_ts = parse_ics_datetime(dtstart_m.group(1).strip()) if dtstart_m else datetime.now(timezone.utc)
        end_ts = parse_ics_datetime(dtend_m.group(1).strip()) if dtend_m else start_ts
        event_id = uid_m.group(1).strip() if uid_m else f"ics_{uuid.uuid4().hex[:12]}"

        events.append(
            CalendarEvent(
                event_id=event_id,
                account_id=account_id,
                source_type=SourceType.EMAIL,
                subject=subject,
                location=loc_m.group(1).strip() if loc_m else None,
                start_ts_utc=start_ts or datetime.now(timezone.utc),
                end_ts_utc=end_ts or start_ts or datetime.now(timezone.utc),
                is_all_day=False,
                organizer=organizer_m.group(1).strip() if organizer_m else None,
                attendees=[],
                body_text=desc_m.group(1).strip() if desc_m else "",
            )
        )

    return events


def generate_ics_event(event: CalendarEvent) -> str:
    """Generate RFC 5545 compliant VCALENDAR/VEVENT string."""
    now_str = format_ics_datetime(datetime.now(timezone.utc))
    start_str = format_ics_datetime(event.start_ts_utc)
    end_str = format_ics_datetime(event.end_ts_utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OldRAG//Sovereign Calendar Engine//EN",
        "BEGIN:VEVENT",
        f"UID:{event.event_id}",
        f"DTSTAMP:{now_str}",
        f"DTSTART:{start_str}",
        f"DTEND:{end_str}",
        f"SUMMARY:{event.subject}",
    ]
    if event.location:
        lines.append(f"LOCATION:{event.location}")
    if event.body_text:
        clean_desc = event.body_text.replace("\n", "\\n").replace("\r", "")
        lines.append(f"DESCRIPTION:{clean_desc}")
    if event.organizer:
        lines.append(f"ORGANIZER:{event.organizer}")
    lines.extend(["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines)


class CalendarManager:
    """Persists and manages calendar events in catalog.db."""

    def __init__(self, catalog_db_path: Path):
        self.db_path = catalog_db_path

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_event(self, event: CalendarEvent) -> None:
        """Store or update a calendar event in catalog.db."""
        conn = self._get_connection()
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            attendees_json = json.dumps(event.attendees)
            conn.execute(
                """
                INSERT INTO calendar_events (
                    event_id, account_id, source_type, subject, location,
                    start_ts_utc, end_ts_utc, is_all_day, organizer,
                    attendees_json, body_text, status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    subject = excluded.subject,
                    location = excluded.location,
                    start_ts_utc = excluded.start_ts_utc,
                    end_ts_utc = excluded.end_ts_utc,
                    is_all_day = excluded.is_all_day,
                    organizer = excluded.organizer,
                    attendees_json = excluded.attendees_json,
                    body_text = excluded.body_text,
                    status = excluded.status;
                """,
                (
                    event.event_id,
                    event.account_id,
                    event.source_type.value,
                    event.subject,
                    event.location,
                    event.start_ts_utc.isoformat(),
                    event.end_ts_utc.isoformat(),
                    1 if event.is_all_day else 0,
                    event.organizer,
                    attendees_json,
                    event.body_text,
                    event.status,
                    now_str,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def list_events(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        account_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[CalendarEvent]:
        """Query calendar events ordered by start time."""
        conn = self._get_connection()
        try:
            query = "SELECT * FROM calendar_events WHERE 1=1"
            params: List[Any] = []

            if start_date:
                query += " AND start_ts_utc >= ?"
                params.append(start_date.isoformat())
            if end_date:
                query += " AND start_ts_utc <= ?"
                params.append(end_date.isoformat())
            if account_id:
                query += " AND account_id = ?"
                params.append(account_id)

            query += " ORDER BY start_ts_utc ASC LIMIT ?"
            params.append(limit)

            cur = conn.execute(query, params)
            rows = cur.fetchall()

            events: List[CalendarEvent] = []
            for r in rows:
                att = []
                if r["attendees_json"]:
                    try:
                        att = json.loads(r["attendees_json"])
                    except Exception:
                        pass

                events.append(
                    CalendarEvent(
                        event_id=r["event_id"],
                        account_id=r["account_id"],
                        source_type=SourceType(r["source_type"]),
                        subject=r["subject"],
                        location=r["location"],
                        start_ts_utc=datetime.fromisoformat(r["start_ts_utc"]),
                        end_ts_utc=datetime.fromisoformat(r["end_ts_utc"]),
                        is_all_day=bool(r["is_all_day"]),
                        organizer=r["organizer"],
                        attendees=att,
                        body_text=r["body_text"] or "",
                        status=r["status"] or "CONFIRMED",
                    )
                )
            return events
        finally:
            conn.close()

    def search_events(
        self,
        query_text: str,
        limit: int = 10,
    ) -> List[CalendarEvent]:
        """Search calendar events matching tokens in subject, body_text, or location."""
        if not query_text or not query_text.strip():
            return []

        from old_rag.engine.search import ENGLISH_STOPWORDS

        raw_tokens = [t.strip().lower() for t in re.split(r"\W+", query_text) if len(t.strip()) >= 2]
        tokens = [t for t in raw_tokens if t not in ENGLISH_STOPWORDS]
        if not tokens and raw_tokens:
            tokens = raw_tokens[:3]
        if not tokens:
            return []

        conn = self._get_connection()
        try:
            where_parts = []
            params: List[Any] = []
            for t in tokens[:5]:
                where_parts.append("(LOWER(subject) LIKE ? OR LOWER(body_text) LIKE ? OR LOWER(location) LIKE ?)")
                params.extend([f"%{t}%", f"%{t}%", f"%{t}%"])

            query = f"SELECT * FROM calendar_events WHERE {' OR '.join(where_parts)} ORDER BY start_ts_utc DESC LIMIT ?"
            params.append(limit)

            cur = conn.execute(query, params)
            rows = cur.fetchall()

            events: List[CalendarEvent] = []
            for r in rows:
                att = []
                if r["attendees_json"]:
                    try:
                        att = json.loads(r["attendees_json"])
                    except Exception:
                        pass
                events.append(
                    CalendarEvent(
                        event_id=r["event_id"],
                        account_id=r["account_id"],
                        source_type=SourceType(r["source_type"]),
                        subject=r["subject"],
                        location=r["location"],
                        start_ts_utc=datetime.fromisoformat(r["start_ts_utc"]),
                        end_ts_utc=datetime.fromisoformat(r["end_ts_utc"]),
                        is_all_day=bool(r["is_all_day"]),
                        organizer=r["organizer"],
                        attendees=att,
                        body_text=r["body_text"] or "",
                        status=r["status"] or "CONFIRMED",
                    )
                )
            return events
        finally:
            conn.close()

    def sync_outlook_calendar(self) -> int:
        """Poll and sync live events from Classic Outlook."""
        from old_rag.ingest.outlook import ClassicMAPIConnector
        connector = ClassicMAPIConnector()
        try:
            count = 0
            for event in connector.poll_calendar():
                self.upsert_event(event)
                count += 1
            return count
        except Exception:
            return 0

