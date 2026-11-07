"""lifecycle.py - Session+SID Scoped Mutex, Anti-Squatting Named Pipe & Autostart."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple


def get_sid_and_session_id() -> Tuple[str, str]:
    """Retrieve current Windows user SID and Terminal Services Session ID."""
    if sys.platform != "win32":
        return "DEV_USER_SID", "0"

    try:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid_obj, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        sid_str = win32security.ConvertSidToStringSid(sid_obj)
        session_id = str(win32security.GetTokenInformation(token, win32security.TokenSessionId))
        return sid_str, session_id
    except Exception:
        user = os.environ.get("USERNAME", "default_user")
        return user, "0"


def get_scoped_pipe_name() -> str:
    """Return \\\\.\\pipe\\OldRAG_<SID>_<SessionID>."""
    sid, session_id = get_sid_and_session_id()
    # Normalize characters for pipe name
    clean_sid = sid.replace("\\", "_").replace(":", "_")
    return f"\\\\.\\pipe\\OldRAG_{clean_sid}_{session_id}"


def get_scoped_mutex_name() -> str:
    """Return Local\\OldRAG_Mutex_<SID>_<SessionID>."""
    sid, session_id = get_sid_and_session_id()
    clean_sid = sid.replace("\\", "_").replace(":", "_")
    return f"Local\\OldRAG_Mutex_{clean_sid}_{session_id}"


class SingleInstanceMutex:
    """Ensures only a single instance of the OldRAG daemon runs per user session."""

    def __init__(self, mutex_name: Optional[str] = None):
        self.mutex_name = mutex_name or get_scoped_mutex_name()
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        try:
            import win32event
            import winerror
            self._handle = win32event.CreateMutex(None, False, self.mutex_name)
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                return False
            return True
        except Exception:
            return True

    def release(self) -> None:
        if self._handle and sys.platform == "win32":
            try:
                import win32api
                win32api.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def set_user_autostart(enable: bool, executable_path: Optional[str] = None) -> bool:
    """Configure zero-admin HKCU Run autostart key."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        app_name = "OldRAG_Daemon"
        if enable:
            exe = executable_path or sys.executable
            cmd = f'"{exe}" -m old_rag.cli daemon'
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False
