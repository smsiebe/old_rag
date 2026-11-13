"""smtp.py - Standard-Library Zero-Dependency SMTP Sender & IMAP Sent-Box Synchronizer."""

from __future__ import annotations

import email.utils
import imaplib
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any, Dict, List, Optional


class SMTPSender:
    """Dispatches outgoing emails via authenticated SMTP with STARTTLS or SSL."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        use_ssl: bool = False,
        use_starttls: bool = True,
        username: str = "",
        password: str = "",
    ):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.use_starttls = use_starttls
        self.username = username
        self.password = password

    def send_message(
        self,
        from_addr: str,
        to_addrs: List[str],
        cc_addrs: Optional[List[str]] = None,
        bcc_addrs: Optional[List[str]] = None,
        subject: str = "",
        body_html: str = "",
        body_text: str = "",
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Compose and dispatch a MIME message over SMTP."""
        if not self.host:
            return {"success": False, "error": "SMTP host is not configured for this account."}

        msg = MIMEMultipart("mixed")
        msg["From"] = from_addr or self.username
        msg["To"] = ", ".join(to_addrs)
        if cc_addrs:
            msg["Cc"] = ", ".join(cc_addrs)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()

        # Alternative body (plain text + html)
        alt_part = MIMEMultipart("alternative")
        if body_text:
            alt_part.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            alt_part.attach(MIMEText(body_html, "html", "utf-8"))
        elif not body_text:
            alt_part.attach(MIMEText("", "plain", "utf-8"))

        msg.attach(alt_part)

        # Attachments
        if attachments:
            for file_path in attachments:
                p = Path(file_path)
                if p.is_file():
                    try:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(p.read_bytes())
                        encoders.encode_base64(part)
                        part.add_header("Content-Disposition", f'attachment; filename="{p.name}"')
                        msg.attach(part)
                    except Exception:
                        pass

        all_recipients = list(to_addrs) + (cc_addrs or []) + (bcc_addrs or [])

        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=15)

            try:
                if self.use_starttls and not self.use_ssl:
                    server.starttls()
                if self.username and self.password:
                    server.login(self.username, self.password)

                server.sendmail(msg["From"], all_recipients, msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass

            return {"success": True, "message_id": msg["Message-ID"], "raw_message": msg.as_string()}
        except Exception as e:
            return {"success": False, "error": f"SMTP Dispatch Error: {str(e)}"}

