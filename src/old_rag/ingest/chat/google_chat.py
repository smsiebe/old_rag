"""google_chat.py - Built-in Google Chat REST API adapter."""

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


class GoogleChatAdapter(BaseChatAdapter):
    """Google Chat adapter communicating via Google Chat REST API v1."""

    adapter_type: str = "google_chat"

    def get_descriptor(self) -> ChatPluginDescriptor:
        return ChatPluginDescriptor(
            adapter_type="google_chat",
            display_name="Google Chat",
            version="1.0.0",
            capabilities=ChatAdapterCapability(
                supports_dms=True,
                supports_rooms=True,
                supports_threads=True,
                supports_streaming=False,
                supports_send=True,
            ),
            config_schema={
                "access_token": "string",
                "refresh_token": "string",
                "client_id": "string",
                "client_secret": "string",
            },
            is_builtin=True,
        )

    def _get_base_url(self) -> str:
        url = self.config.server_url.strip()
        if not url:
            return "https://chat.googleapis.com"
        return url.rstrip("/")

    def _get_headers(self) -> Dict[str, str]:
        token = self.config.credentials.get("access_token") or self.config.credentials.get("auth_token", "")
        if not token:
            token = self._refresh_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _refresh_access_token(self) -> str:
        refresh_token = self.config.credentials.get("refresh_token")
        client_id = self.config.credentials.get("client_id")
        client_secret = self.config.credentials.get("client_secret")
        if not (refresh_token and client_id and client_secret):
            raise ServiceUnavailableError("Google Chat credentials missing access_token and refresh parameters.")
        token_uri = self.config.credentials.get("token_uri", "https://oauth2.googleapis.com/token")
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(
                    token_uri,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
                if res.status_code != 200:
                    raise ServiceUnavailableError(f"OAuth token refresh failed with HTTP {res.status_code}")
                data = res.json()
                new_token = data.get("access_token", "")
                self.config.credentials["access_token"] = new_token
                return new_token
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Google Chat token refresh unreachable: {e}") from e

    def test_connection(self) -> Dict[str, Any]:
        base_url = self._get_base_url()
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{base_url}/v1/spaces?pageSize=1", headers=self._get_headers())
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Google Chat connection check returned HTTP {res.status_code}")
                return {"status": "ok", "provider": "Google Chat API v1"}
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Google Chat unreachable at {base_url}: {e}") from e

    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        rooms: List[ChatRoomSummary] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{base_url}/v1/spaces", headers=headers)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Failed to list Google Chat spaces: HTTP {res.status_code}")
                spaces = res.json().get("spaces", [])
                for sp in spaces:
                    space_type = sp.get("spaceType", "")
                    is_dm = space_type in ("DIRECT_MESSAGE", "singleUserBotDm")
                    if is_dm and not include_dms:
                        continue
                    room_name = sp.get("displayName") or sp.get("name", "Space")
                    rooms.append(
                        ChatRoomSummary(
                            room_id=sp.get("name", ""),  # format: spaces/{space}
                            account_id=self.config.id,
                            adapter_type="google_chat",
                            room_name=room_name,
                            is_dm=is_dm,
                            participant_count=sp.get("membershipCount", {}).get("joinedDirectHumanUserCount", 0),
                            unread_count=0,
                            subscribed=True,
                        )
                    )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Google Chat list_rooms failed: {e}") from e
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

        if since_utc is None and self.config.lookback_days:
            since_utc = datetime.now(timezone.utc) - timedelta(days=self.config.lookback_days)

        clean_room_id = room_id if room_id.startswith("spaces/") else f"spaces/{room_id}"
        params: Dict[str, Any] = {"pageSize": limit}
        if cursor:
            params["pageToken"] = cursor
        if since_utc:
            params["filter"] = f'createTime > "{since_utc.isoformat()}"'

        messages: List[ChatMessageItem] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{base_url}/v1/{clean_room_id}/messages", headers=headers, params=params)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Google Chat fetch_messages failed: HTTP {res.status_code}")
                data = res.json()
                raw_msgs = data.get("messages", [])
                for rm in raw_msgs:
                    sender = rm.get("sender", {})
                    ts_str = rm.get("createTime", "")
                    try:
                        sent_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        sent_ts = datetime.now(timezone.utc)

                    thread_name = rm.get("thread", {}).get("name")
                    messages.append(
                        ChatMessageItem(
                            message_id=rm.get("name", ""),
                            doc_id=f"gchat_{self.config.id}_{rm.get('name', '').replace('/', '_')}",
                            account_id=self.config.id,
                            adapter_type="google_chat",
                            room_id=clean_room_id,
                            room_name="",
                            is_dm=False,
                            sender_id=sender.get("name", ""),
                            sender_name=sender.get("displayName", "Unknown"),
                            thread_root_id=thread_name,
                            sent_ts_utc=sent_ts,
                            body_text=rm.get("text", ""),
                            reactions={},
                            attachments=rm.get("attachment", []),
                        )
                    )
                next_page = data.get("nextPageToken")
                return messages, next_page
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Failed to fetch Google Chat messages: {e}") from e

    def send_message(self, msg: OutgoingChatMessage) -> Dict[str, Any]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        clean_room_id = msg.room_id if msg.room_id.startswith("spaces/") else f"spaces/{msg.room_id}"
        body: Dict[str, Any] = {"text": msg.body_text}
        if msg.thread_root_id:
            body["thread"] = {"name": msg.thread_root_id}

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(f"{base_url}/v1/{clean_room_id}/messages", headers=headers, json=body)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Google Chat send failed: HTTP {res.status_code}")
                return res.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Google Chat send error: {e}") from e
