"""pipeline package for old_rag."""

from old_rag.pipeline.coordinator import PipelineCoordinator
from old_rag.pipeline.daemon import DaemonProcess
from old_rag.pipeline.lifecycle import (
    SingleInstanceMutex,
    get_scoped_mutex_name,
    get_scoped_pipe_name,
    get_sid_and_session_id,
    set_user_autostart,
)

__all__ = [
    "PipelineCoordinator",
    "DaemonProcess",
    "SingleInstanceMutex",
    "get_scoped_mutex_name",
    "get_scoped_pipe_name",
    "get_sid_and_session_id",
    "set_user_autostart",
]

