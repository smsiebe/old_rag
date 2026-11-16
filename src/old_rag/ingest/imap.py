"""imap.py - Standard-Library Zero-Dependency IMAP Connector for Gmail, Yahoo, Fastmail & Custom IMAP."""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

from old_rag.contracts import RawAttachment, RawIngestPayload, SourceType


PRESETS: Dict[str, Dict[str, Any]] = {
    "gmail": {
        "name": "Google Gmail / Google Workspace",
        "host": "imap.gmail.com",
        "port": 993,
        "ssl": True,
        "help": "Requires a 16-character Google App Password (myaccount.google.com/apppasswords) if 2-Step Verification is active.",
    },
    "yahoo": {
        "name": "Yahoo Mail",
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "ssl": True,
        "help": "Requires a Yahoo App Password generated from your Yahoo Account Security settings.",
    },
    "fastmail": {
        "name": "Fastmail",
        "host": "imap.fastmail.com",
        "port": 993,
        "ssl": True,
        "help": "Requires a Fastmail App Password generated from Settings > Password & Security.",
    },
    "custom": {
        "name": "Custom IMAP Server",
        "host": "",
        "port": 993,
        "ssl": True,
        "help": "Standard RFC 3501 IMAP4rev1 server.",
    },
}


def _decode_header_str(val: Optional[str]) -> str:
    """Decode RFC 2047 MIME encoded headers."""
    if not val:
        return ""
    try:
        decoded_parts = email.header.decode_header(val)
        out = []
        for text, encoding in decoded_parts:
            if isinstance(text, bytes):
                out.append(text.decode(encoding or "utf-8", errors="replace"))
            else:
                out.append(str(text))
        return " ".join(out)
    except Exception:
        return str(val)


def _parse_folder_name(raw_line: bytes) -> str:
    """Extract folder name from IMAP LIST response line."""
    decoded = raw_line.decode("utf-8", errors="replace")
    # IMAP LIST response pattern: (\HasNoChildren) "/" "INBOX" or (\HasChildren) "/" INBOX
    match = re.search(r'\((.*?)\)\s+"([^"]+)"\s+(.+)$', decoded)
    if match:
        name = match.group(3).strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        return name
    parts = decoded.split()
    if parts:
        last = parts[-1].strip('"')
        return last
    return decoded


class IMAPConnector:
    """Connects to IMAP mailboxes, tests credentials, lists folders, and ingests emails."""

    def __init__(
        self,
        host: str,
        port: int = 993,
        use_ssl: bool = True,
        username: str = "",
        password: str = "",
    ):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.username = username
        self.password = password

    def _connect(self) -> imaplib.IMAP4:
        """Create and authenticate IMAP connection."""
        if self.use_ssl:
            client = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            client = imaplib.IMAP4(self.host, self.port)
        if self.username and self.password:
            client.login(self.username, self.password)
        return client

    def test_connection(self) -> Dict[str, Any]:
        """Test authentication and discover available mail folders."""
        try:
            client = self._connect()
            typ, data = client.list()
            client.logout()

            folders = []
            if typ == "OK" and data:
                for line in data:
                    if isinstance(line, bytes):
                        f_name = _parse_folder_name(line)
                        if f_name:
                            folders.append(f_name)

            return {
                "success": True,
                "host": self.host,
                "folders": folders,
                "error": None,
            }
        except Exception as e:
            err_msg = str(e)
            if "AUTHENTICATIONFAILED" in err_msg or "Invalid credentials" in err_msg:
                err_msg += " (Hint: For Gmail or Yahoo, ensure you are using an App-Specific Password, not your standard account password)."
            return {
                "success": False,
                "host": self.host,
                "folders": [],
                "error": err_msg,
            }

    def append_sent_message(self, raw_msg_bytes: bytes, sent_folder: Optional[str] = None) -> bool:
        """Append sent email message to the remote Sent folder."""
        import time
        try:
            client = self._connect()
            target_folder = sent_folder or "Sent"
            typ, data = client.list()
            if typ == "OK" and data:
                for line in data:
                    if isinstance(line, bytes):
                        fn = _parse_folder_name(line)
                        if "sent" in fn.lower():
                            target_folder = fn
                            break

            client.append(f'"{target_folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), raw_msg_bytes)
            try:
                client.logout()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def fetch_messages(
        self,
        folders: Optional[List[str]] = None,
        date_range: str = "1yr",
        limit_per_folder: int = 50,
        lookback_days: Optional[int] = None,
        uid_cursor: Optional[str] = None,
    ) -> Generator[RawIngestPayload, None, None]:
        """Fetch emails across folders within the specified date range or lookback days, optionally filtered by UID."""
        client = self._connect()
        try:
            # Build search criteria
            since_date: Optional[datetime] = None
            now = datetime.now(timezone.utc)
            if lookback_days is not None:
                since_date = now - timedelta(days=lookback_days) if lookback_days > 0 else None
            elif date_range == "30d":
                since_date = now - timedelta(days=30)
            elif date_range == "90d":
                since_date = now - timedelta(days=90)
            elif date_range == "1yr":
                since_date = now - timedelta(days=365)
            elif date_range == "all":
                since_date = None

            search_criteria = []
            if since_date:
                search_criteria.append(f'SINCE "{since_date.strftime("%d-%b-%Y")}"')
            if uid_cursor and uid_cursor.isdigit():
                next_uid = int(uid_cursor) + 1
                search_criteria.append(f'UID {next_uid}:*')

            search_query = " ".join(search_criteria) if search_criteria else "ALL"

            folders_to_poll = folders or ["INBOX"]
            for folder in folders_to_poll:
                typ, _ = client.select(f'"{folder}"', readonly=True)
                if typ != "OK":
                    continue

                typ, data = client.search(None, search_query)
                if typ != "OK" or not data or not data[0]:
                    continue

                msg_ids = data[0].split()
                # Process latest messages first
                msg_ids.reverse()
                if limit_per_folder > 0:
                    msg_ids = msg_ids[:limit_per_folder]

                for mid in msg_ids:
                    typ, fetch_data = client.fetch(mid, "(RFC822)")
                    if typ != "OK" or not fetch_data:
                        continue

                    raw_bytes = None
                    for part in fetch_data:
                        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                            raw_bytes = part[1]
                            break

                    if not raw_bytes:
                        continue

                    msg = email.message_from_bytes(raw_bytes)
                    subject = _decode_header_str(msg.get("Subject", ""))
                    sender = _decode_header_str(msg.get("From", ""))
                    msg_id = msg.get("Message-ID", f"imap_{folder}_{mid.decode()}")

                    # Parse Date
                    date_header = msg.get("Date")
                    received_dt = None
                    if date_header:
                        try:
                            parsed_tuple = email.utils.parsedate_to_datetime(date_header)
                            if parsed_tuple:
                                received_dt = parsed_tuple.astimezone(timezone.utc)
                        except Exception:
                            pass

                    # Extract body and attachments
                    body_text = ""
                    body_html = ""
                    raw_attachments: List[RawAttachment] = []

                    if msg.is_multipart():
                        for subpart in msg.walk():
                            c_type = subpart.get_content_type()
                            c_disp = str(subpart.get("Content-Disposition", ""))
                            filename = subpart.get_filename()

                            if filename:
                                payload = subpart.get_payload(decode=True)
                                if payload:
                                    raw_attachments.append(
                                        RawAttachment(
                                            filename=_decode_header_str(filename),
                                            content_type=c_type,
                                            data=payload,
                                            size_bytes=len(payload),
                                        )
                                    )
                            elif c_type == "text/plain" and "attachment" not in c_disp:
                                payload = subpart.get_payload(decode=True)
                                if payload:
                                    body_text += payload.decode(subpart.get_content_charset() or "utf-8", errors="replace") + "\n"
                            elif c_type == "text/html" and "attachment" not in c_disp:
                                payload = subpart.get_payload(decode=True)
                                if payload:
                                    body_html += payload.decode(subpart.get_content_charset() or "utf-8", errors="replace") + "\n"
                    else:
                        c_type = msg.get_content_type()
                        payload = msg.get_payload(decode=True)
                        if payload:
                            decoded_str = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                            if c_type == "text/html":
                                body_html = decoded_str
                            else:
                                body_text = decoded_str

                    doc_id = f"imap_{self.host}_{folder}_{mid.decode()}"
                    original_path = f"imap://{self.host}/{folder}/{mid.decode()}"

                    yield RawIngestPayload(
                        source_type=SourceType.EMAIL,
                        doc_id=doc_id,
                        original_path=original_path,
                        entry_id=doc_id,
                        internet_message_id=msg_id,
                        conversation_id=None,
                        sender=sender,
                        subject=subject,
                        folder=folder,
                        received_ts_utc=received_dt,
                        body_text=body_text.strip(),
                        body_html=body_html.strip(),
                        attachments=raw_attachments,
                        raw_msg_bytes=raw_bytes,
                    )
        finally:
            try:
                client.logout()
            except Exception:
                pass

