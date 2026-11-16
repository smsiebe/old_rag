"""base.py - Base chat adapter class and message window aggregation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from old_rag.contracts import (
    ChatAccountConfig,
    ChatAdapterCapability,
    ChatIngestMeta,
    ChatMessageItem,
    ChatPluginDescriptor,
    ChatRoomSummary,
    OutgoingChatMessage,
    RawIngestPayload,
    ServiceUnavailableError,
    SourceType,
)
from old_rag.protocols import ChatAdapterProtocol


class BaseChatAdapter(ABC, ChatAdapterProtocol):
    """Base class for real-time chat connector adapters with shared windowing logic."""

    adapter_type: str = "base"

    def __init__(self, config: ChatAccountConfig):
        self.config = config

    def get_descriptor(self) -> ChatPluginDescriptor:
        return ChatPluginDescriptor(
            adapter_type=self.adapter_type,
            display_name="Base Chat Adapter",
            version="1.0.0",
            capabilities=ChatAdapterCapability(),
            config_schema={},
            is_builtin=True,
        )

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        raise NotImplementedError("Subclasses must implement test_connection")

    @abstractmethod
    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        raise NotImplementedError("Subclasses must implement list_rooms")

    @abstractmethod
    def fetch_messages(
        self,
        room_id: str,
        since_utc: Optional[datetime] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[ChatMessageItem], Optional[str]]:
        raise NotImplementedError("Subclasses must implement fetch_messages")

    @abstractmethod
    def send_message(self, msg: OutgoingChatMessage) -> Dict[str, Any]:
        raise NotImplementedError("Subclasses must implement send_message")

    def stream_events(self, on_message: Any, stop_event: Any) -> None:
        """Default fallback: periodic polling loop if streaming is unsupported."""
        import time
        poll_interval = self.config.poll_interval_sec or 30.0
        while not (stop_event.is_set() if hasattr(stop_event, "is_set") else bool(stop_event)):
            try:
                for room in self.list_rooms(include_dms=self.config.include_dms):
                    if not room.subscribed:
                        continue
                    msgs, _ = self.fetch_messages(room.room_id, limit=20)
                    for m in msgs:
                        on_message(m)
            except Exception:
                pass
            time.sleep(min(poll_interval, 5.0))

    @staticmethod
    def group_messages_into_ingest_payloads(
        messages: List[ChatMessageItem],
        account: ChatAccountConfig,
    ) -> List[RawIngestPayload]:
        """Aggregate individual chat messages into bounded conversational window payloads.

        Avoids CAS micro-file explosion and context-free 2-word embeddings while
        ensuring complete lineage and metadata preservation.
        """
        if not messages:
            return []

        # Sort chronologically
        sorted_msgs = sorted(messages, key=lambda m: m.sent_ts_utc)

        # Bucket by (room_id, thread_root_id or hour_bucket)
        buckets: Dict[str, List[ChatMessageItem]] = {}
        for m in sorted_msgs:
            if m.thread_root_id:
                bucket_key = f"{m.room_id}_thread_{m.thread_root_id}"
            else:
                hour_bucket = m.sent_ts_utc.strftime("%Y%m%d_%H")
                bucket_key = f"{m.room_id}_hour_{hour_bucket}"

            if bucket_key not in buckets:
                buckets[bucket_key] = []
            buckets[bucket_key].append(m)

        payloads: List[RawIngestPayload] = []

        # Chunk large buckets into max 25 messages per conversational window
        for bucket_key, b_msgs in buckets.items():
            for i in range(0, len(b_msgs), 25):
                window = b_msgs[i : i + 25]
                first_msg = window[0]
                last_msg = window[-1]

                room_name = first_msg.room_name or first_msg.room_id
                kind = "DM" if first_msg.is_dm else "Room"
                header = f"[Chat: {account.adapter_type.capitalize()} | {room_name} ({kind})]\n"

                lines = []
                combined_reactions: Dict[str, List[str]] = {}
                for m in window:
                    ts_str = m.sent_ts_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
                    lines.append(f"[{ts_str}] {m.sender_name}: {m.body_text}")
                    for r_emoji, r_users in m.reactions.items():
                        if r_emoji not in combined_reactions:
                            combined_reactions[r_emoji] = []
                        for u in r_users:
                            if u not in combined_reactions[r_emoji]:
                                combined_reactions[r_emoji].append(u)

                body_text = header + "\n".join(lines)

                # Deterministic window doc_id
                win_hash = hashlib.sha256(f"{first_msg.message_id}_{len(window)}".encode()).hexdigest()[:10]
                doc_id = f"chat_{account.id}_{first_msg.room_id}_{win_hash}"

                chat_meta = ChatIngestMeta(
                    account_id=account.id,
                    adapter_type=account.adapter_type,
                    room_id=first_msg.room_id,
                    room_name=room_name,
                    is_dm=first_msg.is_dm,
                    sender_id=first_msg.sender_id,
                    sender_name=first_msg.sender_name,
                    thread_root_id=first_msg.thread_root_id,
                    message_ids=[m.message_id for m in window],
                    reactions=combined_reactions,
                )

                payload = RawIngestPayload(
                    doc_id=doc_id,
                    source_type=SourceType.CHAT,
                    original_path=f"chat://{account.adapter_type}/{account.id}/{first_msg.room_id}",
                    entry_id=first_msg.message_id,
                    conversation_id=first_msg.thread_root_id or first_msg.room_id,
                    sender=first_msg.sender_name,
                    subject=f"{room_name} ({kind})",
                    folder=room_name,
                    received_ts_utc=last_msg.sent_ts_utc,
                    body_text=body_text,
                    account_id=account.id,
                    chat_meta=chat_meta,
                )
                payloads.append(payload)

        return payloads
