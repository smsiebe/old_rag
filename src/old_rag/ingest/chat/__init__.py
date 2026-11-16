"""chat - Real-time chat plugin host and built-in chat adapters."""

from old_rag.ingest.chat.base import BaseChatAdapter
from old_rag.ingest.chat.google_chat import GoogleChatAdapter
from old_rag.ingest.chat.jabber import JabberAdapter
from old_rag.ingest.chat.registry import ChatPluginRegistry
from old_rag.ingest.chat.rocketchat import RocketChatAdapter
from old_rag.ingest.chat.teams import TeamsGraphAdapter

__all__ = [
    "BaseChatAdapter",
    "ChatPluginRegistry",
    "GoogleChatAdapter",
    "JabberAdapter",
    "RocketChatAdapter",
    "TeamsGraphAdapter",
]
