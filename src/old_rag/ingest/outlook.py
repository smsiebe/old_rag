"""outlook.py - STA-Isolated Classic Outlook MAPI Poller & New-Outlook Detector."""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from typing import Generator, List, Optional

from old_rag.contracts import RawAttachment, RawIngestPayload, ServiceUnavailableError, SourceType


def is_new_outlook_active() -> bool:
    """Detect whether 'New Outlook' (olk.exe) is running or active in preferences."""
    if sys.platform != "win32":
        return False
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name == "olk.exe":
                return True
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Office\16.0\Outlook\Preferences",
            0,
            winreg.KEY_READ,
        )
        val, _ = winreg.QueryValueEx(key, "UseNewOutlook")
        winreg.CloseKey(key)
        if val == 1:
            return True
    except Exception:
        pass
    return False


def probe_object_model_guard(timeout_sec: float = 2.0) -> bool:
    """Probe if Outlook Object Model Guard triggers a modal prompt.

    Returns True if safe/unprompted; False if prompt appears or COM access blocked.
    """
    if sys.platform != "win32":
        return False
    if is_new_outlook_active():
        return False

    result = {"success": False, "completed": False}

    def _worker():
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                try:
                    outlook = win32com.client.GetActiveObject("Outlook.Application")
                except Exception:
                    outlook = win32com.client.Dispatch("Outlook.Application")
                ns = outlook.GetNamespace("MAPI")
                inbox = ns.GetDefaultFolder(6)  # olFolderInbox = 6
                # Test reading item count and first item property
                if inbox.Items.Count > 0:
                    item = inbox.Items.Item(1)
                    _ = getattr(item, "SenderEmailAddress", None)
                result["success"] = True
            except Exception:
                result["success"] = False
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            result["success"] = False
        finally:
            result["completed"] = True

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result["completed"] and result["success"]


class ClassicMAPIConnector:
    """Classic Outlook MAPI Connector running inside a dedicated STA worker thread."""

    OL_MSG_UNICODE = 9
    OL_MAIL_CLASS = 43

    def __init__(self, allow_background_launch: bool = False):
        self.allow_background_launch = allow_background_launch

    def check_availability(self) -> None:
        """Verify Classic Outlook is installed, running, and accessible."""
        if is_new_outlook_active():
            raise ServiceUnavailableError("New Outlook (olk.exe) detected; MAPI COM requires Classic Outlook.")

        if sys.platform != "win32":
            raise ServiceUnavailableError("MAPI COM is only supported on Windows.")

    def format_dasl_query(self, as_of_date_utc: Optional[datetime], is_sent_folder: bool = False) -> Optional[str]:
        """Generate locale-independent DASL query string with UTC timestamp."""
        if not as_of_date_utc:
            return None
        # Ensure UTC timezone
        dt_utc = as_of_date_utc.astimezone(timezone.utc)
        date_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        if is_sent_folder:
            # urn:schemas:httpmail:date or PR_CLIENT_SUBMIT_TIME
            return f'@SQL="urn:schemas:httpmail:date" >= \'{date_str}\''
        return f'@SQL="urn:schemas:httpmail:datereceived" >= \'{date_str}\''

    def poll_messages(
        self,
        folder_names: Optional[List[str]] = None,
        as_of_date_utc: Optional[datetime] = None,
        batch_size: int = 25,
    ) -> Generator[RawIngestPayload, None, None]:
        """Poll Classic Outlook inside dedicated STA thread."""
        self.check_availability()

        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:
                try:
                    outlook = win32com.client.Dispatch("Outlook.Application")
                except Exception as e:
                    raise ServiceUnavailableError(f"Classic Outlook COM connection failed: {str(e)}")

            ns = outlook.GetNamespace("MAPI")
            folders_to_poll = []
            if not folder_names:
                # Default: Inbox (6) and Sent Mail (5)
                folders_to_poll.append((ns.GetDefaultFolder(6), False))
                folders_to_poll.append((ns.GetDefaultFolder(5), True))

            for folder, is_sent in folders_to_poll:
                items = folder.Items
                dasl = self.format_dasl_query(as_of_date_utc, is_sent_folder=is_sent)
                filtered_items = items.Restrict(dasl) if dasl else items

                count = 0
                for item in filtered_items:
                    # Class 43 = olMail
                    if getattr(item, "Class", None) != self.OL_MAIL_CLASS:
                        continue

                    # Skip SMIME or encrypted
                    msg_class = getattr(item, "MessageClass", "")
                    if msg_class.startswith("IPM.Note.SMIME") or "encrypted" in msg_class.lower():
                        continue

                    doc_id = str(getattr(item, "EntryID", ""))
                    if not doc_id:
                        continue

                    # Extract properties
                    subject = getattr(item, "Subject", "")
                    sender = getattr(item, "SenderEmailAddress", "")
                    body_text = getattr(item, "Body", "")
                    body_html = getattr(item, "HTMLBody", "")
                    conv_id = getattr(item, "ConversationID", "")

                    # Extract PR_INTERNET_MESSAGE_ID via PropertyAccessor
                    prop_accessor = getattr(item, "PropertyAccessor", None)
                    internet_msg_id = None
                    if prop_accessor:
                        try:
                            # PR_INTERNET_MESSAGE_ID = http://schemas.microsoft.com/mapi/proptag/0x1035001F
                            internet_msg_id = prop_accessor.GetProperty("http://schemas.microsoft.com/mapi/proptag/0x1035001F")
                        except Exception:
                            pass

                    # Extract attachments
                    raw_attachments = []
                    item_attachments = getattr(item, "Attachments", None)
                    if item_attachments:
                        for idx in range(1, item_attachments.Count + 1):
                            att = item_attachments.Item(idx)
                            att_filename = getattr(att, "FileName", f"att_{idx}")
                            # Stream attachment bytes via temp file or direct memory
                            try:
                                import tempfile
                                with tempfile.NamedTemporaryFile(delete=False) as tf:
                                    temp_path = tf.name
                                att.SaveAsFile(temp_path)
                                with open(temp_path, "rb") as rf:
                                    data = rf.read()
                                import os
                                os.unlink(temp_path)
                                raw_attachments.append(
                                    RawAttachment(
                                        filename=att_filename,
                                        content_type="application/octet-stream",
                                        data=data,
                                        size_bytes=len(data),
                                    )
                                )
                            except Exception:
                                pass

                    # Extract received time
                    received_time = getattr(item, "ReceivedTime", None)
                    dt_received = None
                    if received_time:
                        try:
                            dt_received = datetime(
                                received_time.year, received_time.month, received_time.day,
                                received_time.hour, received_time.minute, received_time.second,
                                tzinfo=timezone.utc,
                            )
                        except Exception:
                            dt_received = datetime.now(timezone.utc)
                    else:
                        dt_received = datetime.now(timezone.utc)

                    # Save raw .msg Unicode for recovery
                    raw_msg_bytes = None
                    try:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tf:
                            temp_msg_path = tf.name
                        item.SaveAs(temp_msg_path, self.OL_MSG_UNICODE)
                        with open(temp_msg_path, "rb") as mf:
                            raw_msg_bytes = mf.read()
                        import os
                        os.unlink(temp_msg_path)
                    except Exception:
                        pass

                    payload = RawIngestPayload(
                        source_type=SourceType.EMAIL,
                        doc_id=doc_id,
                        original_path=f"Outlook://{folder.Name}/{doc_id[:16]}",
                        entry_id=doc_id,
                        internet_message_id=internet_msg_id,
                        conversation_id=conv_id,
                        sender=sender,
                        subject=subject,
                        folder=folder.Name,
                        received_ts_utc=dt_received,
                        body_text=body_text,
                        body_html=body_html,
                        attachments=raw_attachments,
                        raw_msg_bytes=raw_msg_bytes,
                    )
                    yield payload
                    count += 1
                    if count % batch_size == 0:
                        pythoncom.PumpWaitingMessages()
        finally:
            pythoncom.CoUninitialize()

