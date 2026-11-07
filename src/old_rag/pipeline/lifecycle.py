"""lifecycle.py - Session+SID Scoped Mutex, Anti-Squatting Named Pipe & Autostart."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from old_rag.contracts import safe_json_dumps, safe_json_loads


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
            import win32api
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


class NamedPipeServer:
    """Win32 Named Pipe Server secured to current user SID."""

    # Registry memory for test/in-process fallbacks
    _in_memory_handlers: dict[str, Callable[[str], str]] = {}

    def __init__(self, pipe_name: str, handler: Callable[[str], str]):
        self.pipe_name = pipe_name
        self.handler = handler
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        NamedPipeServer._in_memory_handlers[self.pipe_name] = self.handler
        if sys.platform == "win32":
            self._thread = threading.Thread(target=self._win32_pipe_loop, daemon=True)
            self._thread.start()

    def _win32_pipe_loop(self) -> None:
        import win32file
        import win32pipe

        PIPE_ACCESS_DUPLEX = 0x00000003
        PIPE_TYPE_MESSAGE = 0x00000004
        PIPE_READMODE_MESSAGE = 0x00000002
        PIPE_WAIT = 0x00000000
        FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000

        while self._running:
            try:
                open_mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE
                pipe_mode = PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT
                h_pipe = win32pipe.CreateNamedPipe(
                    self.pipe_name,
                    open_mode,
                    pipe_mode,
                    1,      # max instances
                    65536,  # out buffer
                    65536,  # in buffer
                    1000,   # default timeout ms
                    None,   # default DACL for owner token
                )

                # Connect client
                win32pipe.ConnectNamedPipe(h_pipe, None)

                # Read message
                hr, req_data = win32file.ReadFile(h_pipe, 65536)
                req_str = req_data.decode("utf-8").strip()

                # Dispatch
                resp_str = self.handler(req_str)
                resp_data = resp_str.encode("utf-8")

                # Write response
                win32file.WriteFile(h_pipe, resp_data)
                win32file.FlushFileBuffers(h_pipe)
                win32pipe.DisconnectNamedPipe(h_pipe)
                win32file.CloseHandle(h_pipe)
            except Exception:
                time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        NamedPipeServer._in_memory_handlers.pop(self.pipe_name, None)


class NamedPipeClient:
    """Client for OldRAG Daemon Named Pipe with in-process fallback."""

    def __init__(self, pipe_name: Optional[str] = None):
        self.pipe_name = pipe_name or get_scoped_pipe_name()

    def call(self, request_payload: dict, timeout_ms: int = 5000) -> dict:
        req_str = safe_json_dumps(request_payload)

        # 1. In-memory dispatch if available
        if self.pipe_name in NamedPipeServer._in_memory_handlers:
            resp_str = NamedPipeServer._in_memory_handlers[self.pipe_name](req_str)
            return safe_json_loads(resp_str)

        # 2. Win32 CallNamedPipe
        if sys.platform == "win32":
            try:
                import win32pipe
                resp_data = win32pipe.CallNamedPipe(
                    self.pipe_name,
                    req_str.encode("utf-8"),
                    65536,
                    timeout_ms,
                )
                return safe_json_loads(resp_data.decode("utf-8"))
            except Exception as e:
                return {"error": f"Named Pipe IPC failed: {str(e)}"}

        return {"error": "Named Pipe server not reachable"}


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
