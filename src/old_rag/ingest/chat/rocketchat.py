"""rocketchat.py - Built-in Rocket.Chat REST API adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import httpx

from old_rag.contracts import (
    ChatAccountConfig,
    ChatAdapterCapability,
    ChatMessageItem,
    ChatPluginDescriptor,
    ChatRoomSummary,
    OutgoingChatMessage,
    ServiceUnavailableError,
)
from old_rag.ingest.chat.base import BaseChatAdapter


class RocketChatAdapter(BaseChatAdapter):
    """Rocket.Chat adapter communicating via official REST API v1."""

    adapter_type: str = "rocketchat"

    def get_descriptor(self) -> ChatPluginDescriptor:
        return ChatPluginDescriptor(
            adapter_type="rocketchat",
            display_name="Rocket.Chat",
            version="1.0.0",
            capabilities=ChatAdapterCapability(
                supports_dms=True,
                supports_rooms=True,
                supports_threads=True,
                supports_streaming=False,
                supports_send=True,
            ),
            config_schema={
                "server_url": "string (e.g. https://chat.example.com)",
                "auth_token": "string",
                "user_id": "string",
            },
            is_builtin=True,
        )

    def _get_base_url(self) -> str:
        url = self.config.server_url.strip()
        if not url:
            raise ServiceUnavailableError("Rocket.Chat server_url is not configured.")
        return url.rstrip("/")

    def _get_headers(self) -> Dict[str, str]:
        auth_token = self.config.credentials.get("auth_token", "")
        user_id = self.config.credentials.get("user_id", "")
        headers = {"Content-Type": "application/json"}
        if auth_token and user_id:
            headers["X-Auth-Token"] = auth_token
            headers["X-User-Id"] = user_id
        return headers

    def test_connection(self) -> Dict[str, Any]:
        base_url = self._get_base_url()
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{base_url}/api/v1/info", headers=self._get_headers())
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Rocket.Chat connection failed with HTTP {res.status_code}")
                data = res.json()
                return {"status": "ok", "version": data.get("info", {}).get("version", "unknown")}
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Rocket.Chat unreachable at {base_url}: {e}") from e

    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        rooms: List[ChatRoomSummary] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                # Public channels
                c_res = client.get(f"{base_url}/api/v1/channels.list.joined", headers=headers)
                if c_res.status_code == 200:
                    for ch in c_res.json().get("channels", []):
                        rooms.append(
                            ChatRoomSummary(
                                room_id=ch.get("_id", ""),
                                account_id=self.config.id,
                                adapter_type="rocketchat",
                                room_name=ch.get("name", "channel"),
                                is_dm=False,
                                participant_count=ch.get("usersCount", 0),
                                unread_count=ch.get("unread", 0),
                                subscribed=True,
                            )
                        )
                # Private groups
                g_res = client.get(f"{base_url}/api/v1/groups.list", headers=headers)
                if g_res.status_code == 200:
                    for grp in g_res.json().get("groups", []):
                        rooms.append(
                            ChatRoomSummary(
                                room_id=grp.get("_id", ""),
                                account_id=self.config.id,
                                adapter_type="rocketchat",
                                room_name=grp.get("name", "group"),
                                is_dm=False,
                                participant_count=grp.get("usersCount", 0),
                                unread_count=grp.get("unread", 0),
                                subscribed=True,
                            )
                        )
                # Direct messages (IMs)
                if include_dms:
                    im_res = client.get(f"{base_url}/api/v1/im.list", headers=headers)
                    if im_res.status_code == 200:
                        for im in im_res.json().get("ims", []):
                            usernames = im.get("usernames", [])
                            dm_name = ", ".join(usernames) or "Direct Message"
                            rooms.append(
                                ChatRoomSummary(
                                    room_id=im.get("_id", ""),
                                    account_id=self.config.id,
                                    adapter_type="rocketchat",
                                    room_name=dm_name,
                                    is_dm=True,
                                    participant_count=len(usernames),
                                    unread_count=im.get("unread", 0),
                                    subscribed=True,
                                )
                            )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Failed to fetch Rocket.Chat rooms: {e}") from e
        return rooms

    def fetch_messages(
        self,
        room_id: str,
        since_utc: Optional[datetime] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[ChatMessageItem], Optional[str]]:
        base_url = self._get_base_url()
        headers = self._get_headers()

        # If since_utc not provided, determine from lookback_days
        if since_utc is None and self.config.lookback_days:
            since_utc = datetime.now(timezone.utc) - timedelta(days=self.config.lookback_days)

        params: Dict[str, Any] = {"roomId": room_id, "count": limit}
        if since_utc:
            params["oldest"] = since_utc.isoformat()
        if cursor:
            params["offset"] = cursor

        messages: List[ChatMessageItem] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                # Try channels.history first; fall back to groups.history or im.history
                res = client.get(f"{base_url}/api/v1/channels.history", headers=headers, params=params)
                if res.status_code == 400 or res.status_code == 404:
                    res = client.get(f"{base_url}/api/v1/groups.history", headers=headers, params=params)
                if res.status_code == 400 or res.status_code == 404:
                    res = client.get(f"{base_url}/api/v1/im.history", headers=headers, params=params)

                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Rocket.Chat history failed: HTTP {res.status_code}")

                raw_data = res.json()
                raw_msgs = raw_data.get("messages", [])
                for rm in raw_msgs:
                    u = rm.get("u", {})
                    ts_str = rm.get("ts", "")
                    try:
                        sent_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        sent_ts = datetime.now(timezone.utc)

                    reactions: Dict[str, List[str]] = {}
                    for rx_key, rx_val in rm.get("reactions", {}).items():
                        reactions[rx_key] = rx_val.get("usernames", [])

                    messages.append(
                        ChatMessageItem(
                            message_id=rm.get("_id", ""),
                            doc_id=f"rc_{self.config.id}_{rm.get('_id')}",
                            account_id=self.config.id,
                            adapter_type="rocketchat",
                            room_id=room_id,
                            room_name="",
                            is_dm=False,
                            sender_id=u.get("_id", ""),
                            sender_name=u.get("name") or u.get("username", "Unknown"),
                            thread_root_id=rm.get("tmid"),
                            sent_ts_utc=sent_ts,
                            body_text=rm.get("msg", ""),
                            reactions=reactions,
                            attachments=rm.get("attachments", []),
                        )
                    )
                next_cursor = None
                if len(raw_msgs) == limit:
                    next_cursor = str(int(cursor or 0) + limit)
                return messages, next_cursor
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Failed to fetch Rocket.Chat messages for room {room_id}: {e}") from e

    def send_message(self, msg: OutgoingChatMessage) -> Dict[str, Any]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        body: Dict[str, Any] = {
            "message": {
                "rid": msg.room_id,
                "msg": msg.body_text,
            }
        }
        if msg.thread_root_id:
            body["message"]["tmid"] = msg.thread_root_id
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(f"{base_url}/api/v1/chat.sendMessage", headers=headers, json=body)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Failed to post Rocket.Chat message: HTTP {res.status_code}")
                return res.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Failed to send Rocket.Chat message: {e}") from e
