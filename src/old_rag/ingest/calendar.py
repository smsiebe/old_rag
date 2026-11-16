"""calendar.py - Sovereign Calendar Events Management, Outlook Sync & .ICS Support."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from old_rag.contracts import CalendarEvent, CalendarIngestMeta, RawIngestPayload, ServiceUnavailableError, SourceType


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
                source_type=SourceType.CALENDAR,
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


def event_to_ingest_payload(event: CalendarEvent) -> RawIngestPayload:
    """Convert a CalendarEvent into a searchable RawIngestPayload with full lineage."""
    doc_id = event.doc_id or f"cal_{event.event_id}"
    cal_meta = CalendarIngestMeta(
        event_id=event.event_id,
        account_id=event.account_id,
        location=event.location,
        start_ts_utc=event.start_ts_utc,
        end_ts_utc=event.end_ts_utc,
        is_all_day=event.is_all_day,
        organizer=event.organizer,
        attendees=event.attendees,
        status=event.status,
    )
    start_str = event.start_ts_utc.strftime("%Y-%m-%d %H:%M UTC")
    end_str = event.end_ts_utc.strftime("%Y-%m-%d %H:%M UTC")
    body_parts = [
        f"Calendar Event: {event.subject}",
        f"Time: {start_str} to {end_str}",
        f"Location: {event.location or 'N/A'}",
        f"Organizer: {event.organizer or 'N/A'}",
        f"Attendees: {', '.join(event.attendees) if event.attendees else 'None'}",
        f"Status: {event.status}",
    ]
    if event.body_text:
        body_parts.append(f"Notes:\n{event.body_text}")
    full_body = "\n".join(body_parts)

    return RawIngestPayload(
        source_type=SourceType.CALENDAR,
        doc_id=doc_id,
        original_path=f"calendar://{event.account_id or 'default'}/{event.event_id}",
        entry_id=event.event_id,
        sender=event.organizer,
        subject=event.subject,
        folder="Calendar",
        received_ts_utc=event.start_ts_utc,
        body_text=full_body,
        account_id=event.account_id,
        calendar_meta=cal_meta,
    )


def generate_ics_event(event: CalendarEvent) -> str:
    """Generate RFC 5545 compliant VCALENDAR/VEVENT string."""
    return export_events_to_ics([event])


def export_events_to_ics(events: List[CalendarEvent]) -> str:
    """Generate RFC 5545 compliant VCALENDAR containing multiple events."""
    now_str = format_ics_datetime(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OldRAG//Sovereign Calendar Engine//EN",
    ]
    for event in events:
        start_str = format_ics_datetime(event.start_ts_utc)
        end_str = format_ics_datetime(event.end_ts_utc)
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{event.event_id}",
            f"DTSTAMP:{now_str}",
            f"DTSTART:{start_str}",
            f"DTEND:{end_str}",
            f"SUMMARY:{event.subject}",
        ])
        if event.location:
            lines.append(f"LOCATION:{event.location}")
        if event.body_text:
            clean_desc = event.body_text.replace("\n", "\\n").replace("\r", "")
            lines.append(f"DESCRIPTION:{clean_desc}")
        if event.organizer:
            lines.append(f"ORGANIZER:{event.organizer}")
        lines.extend(["STATUS:CONFIRMED", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


class CalendarManager:
    """Persists and manages calendar events in catalog.db."""

    def __init__(self, catalog_db_path: Path):
        self.db_path = catalog_db_path
        self._schema_ensured = False

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ensured:
            return
        self._schema_ensured = True
        try:
            cur = conn.execute("PRAGMA table_info(calendar_events)")
            cols = {row["name"] for row in cur.fetchall()}
            if cols and "calendar_id" not in cols:
                conn.execute("ALTER TABLE calendar_events ADD COLUMN calendar_id TEXT;")
            if cols and "calendar_color" not in cols:
                conn.execute("ALTER TABLE calendar_events ADD COLUMN calendar_color TEXT;")
            if cols and "source_system" not in cols:
                conn.execute("ALTER TABLE calendar_events ADD COLUMN source_system TEXT;")
            conn.commit()
        except Exception:
            pass

    def upsert_event(self, event: CalendarEvent) -> None:
        """Store or update a calendar event in catalog.db."""
        conn = self._get_connection()
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            attendees_json = json.dumps(event.attendees)
            cal_id = event.calendar_id or event.account_id or "default"
            cal_color = event.calendar_color or "#339af0"
            source_sys = event.source_system or "Outlook"
            conn.execute(
                """
                INSERT INTO calendar_events (
                    event_id, doc_id, account_id, calendar_id, calendar_color, source_system,
                    source_type, subject, location,
                    start_ts_utc, end_ts_utc, is_all_day, organizer,
                    attendees_json, body_text, status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    doc_id = COALESCE(excluded.doc_id, calendar_events.doc_id),
                    account_id = excluded.account_id,
                    calendar_id = COALESCE(excluded.calendar_id, calendar_events.calendar_id),
                    calendar_color = COALESCE(excluded.calendar_color, calendar_events.calendar_color),
                    source_system = COALESCE(excluded.source_system, calendar_events.source_system),
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
                    event.doc_id,
                    event.account_id,
                    cal_id,
                    cal_color,
                    source_sys,
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
        calendar_id: Optional[str] = None,
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
            if calendar_id:
                query += " AND (calendar_id = ? OR account_id = ?)"
                params.extend([calendar_id, calendar_id])

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

                keys = r.keys()
                events.append(
                    CalendarEvent(
                        event_id=r["event_id"],
                        account_id=r["account_id"],
                        calendar_id=r["calendar_id"] if "calendar_id" in keys else r["account_id"],
                        calendar_color=r["calendar_color"] if ("calendar_color" in keys and r["calendar_color"]) else "#339af0",
                        source_system=r["source_system"] if ("source_system" in keys and r["source_system"]) else "outlook",
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
                keys = r.keys()
                events.append(
                    CalendarEvent(
                        event_id=r["event_id"],
                        account_id=r["account_id"],
                        calendar_id=r["calendar_id"] if "calendar_id" in keys else r["account_id"],
                        calendar_color=r["calendar_color"] if ("calendar_color" in keys and r["calendar_color"]) else "#339af0",
                        source_system=r["source_system"] if ("source_system" in keys and r["source_system"]) else "outlook",
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

    def sync_calendar_account(
        self,
        account_config: Dict[str, Any],
        coordinator: Optional[Any] = None,
    ) -> int:
        """Poll and sync live events for a specific calendar account connector."""
        connector_type = account_config.get("connector_type", "outlook_mapi")
        acc_id = account_config.get("id", "default_outlook_cal")
        acc_name = account_config.get("name", "Calendar")
        color = account_config.get("color", "#339af0")
        synced_count = 0

        if connector_type == "outlook_mapi":
            from old_rag.ingest.outlook import ClassicMAPIConnector
            connector = ClassicMAPIConnector()
            try:
                for event in connector.poll_calendar():
                    event.account_id = acc_id
                    event.calendar_id = acc_id
                    event.calendar_color = color
                    event.source_system = "Outlook"
                    self.upsert_event(event)
                    if coordinator is not None:
                        try:
                            payload = event_to_ingest_payload(event)
                            coordinator.ingest_payload(payload)
                        except Exception:
                            pass
                    synced_count += 1
            except Exception:
                pass
        else:
            url = account_config.get("url")
            username = account_config.get("username", "")
            creds = account_config.get("credentials", {})
            password = account_config.get("password") or creds.get("password", "")
            if url:
                try:
                    if url.startswith("http://") or url.startswith("https://"):
                        connector = CalDAVConnector(
                            url=url,
                            username=username,
                            password=password,
                            account_id=acc_id,
                            lookback_days=account_config.get("lookback_days", 30),
                        )
                        events = connector.fetch_events()
                    else:
                        ics_path = Path(url)
                        if ics_path.exists():
                            events = parse_ics_content(ics_path.read_text(encoding="utf-8", errors="replace"), account_id=acc_id)
                        else:
                            events = []
                    for event in events:
                        event.account_id = acc_id
                        event.calendar_id = acc_id
                        event.calendar_color = color
                        event.source_system = acc_name
                        self.upsert_event(event)
                        if coordinator is not None:
                            try:
                                payload = event_to_ingest_payload(event)
                                coordinator.ingest_payload(payload)
                            except Exception:
                                pass
                        synced_count += 1
                except Exception:
                    pass

        return synced_count

    def sync_outlook_calendar(self) -> int:
        """Poll and sync live events from Classic Outlook."""
        from old_rag.storage.settings import get_calendar_accounts
        accounts = get_calendar_accounts()
        mapi_acc = next((a for a in accounts if a.get("connector_type") == "outlook_mapi"), None)
        if not mapi_acc:
            mapi_acc = {
                "id": "default_outlook_cal",
                "name": "Classic Outlook Calendar",
                "connector_type": "outlook_mapi",
                "color": "#339af0",
            }
        return self.sync_calendar_account(mapi_acc)


class CalDAVConnector:
    """CalDAV client for RFC 4791 calendar retrieval with lookback_days filtering."""

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        account_id: Optional[str] = None,
        lookback_days: Optional[int] = 30,
    ):
        self.url = url
        self.username = username
        self.password = password
        self.account_id = account_id
        self.lookback_days = lookback_days

    def fetch_events(self, since_utc: Optional[datetime] = None) -> List[CalendarEvent]:
        """Fetch calendar events over HTTP CalDAV/ICS."""
        import httpx
        if not self.url:
            return []
        auth = (self.username, self.password) if (self.username and self.password) else None
        if since_utc is None and self.lookback_days:
            since_utc = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)

        try:
            with httpx.Client(timeout=15.0, auth=auth) as client:
                res = client.get(self.url)
                if res.status_code == 200 and "BEGIN:VCALENDAR" in res.text:
                    events = parse_ics_content(res.text, account_id=self.account_id)
                    if since_utc:
                        events = [e for e in events if e.end_ts_utc >= since_utc]
                    return events
                elif res.status_code >= 400:
                    raise ServiceUnavailableError(f"CalDAV endpoint returned HTTP {res.status_code}")
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"CalDAV server unreachable at {self.url}: {e}") from e
        return []

