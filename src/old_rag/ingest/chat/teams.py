"""teams.py - Built-in Microsoft Teams Microsoft Graph adapter."""

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


class TeamsGraphAdapter(BaseChatAdapter):
    """Microsoft Teams adapter communicating via Microsoft Graph REST API v1.0."""

    adapter_type: str = "teams"

    def get_descriptor(self) -> ChatPluginDescriptor:
        return ChatPluginDescriptor(
            adapter_type="teams",
            display_name="Microsoft Teams",
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
                "tenant_id": "string",
            },
            is_builtin=True,
        )

    def _get_base_url(self) -> str:
        url = self.config.server_url.strip()
        if not url:
            return "https://graph.microsoft.com/v1.0"
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
        client_secret = self.config.credentials.get("client_secret", "")
        tenant_id = self.config.credentials.get("tenant_id", "common")
        if not (refresh_token and client_id):
            raise ServiceUnavailableError("Microsoft Teams credentials missing access_token and refresh parameters.")
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "Chat.ReadWrite ChannelMessage.Read.All offline_access",
        }
        if client_secret:
            data["client_secret"] = client_secret
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(token_url, data=data)
                if res.status_code != 200:
                    raise ServiceUnavailableError(f"Teams OAuth token renewal failed with HTTP {res.status_code}")
                token_data = res.json()
                new_token = token_data.get("access_token", "")
                self.config.credentials["access_token"] = new_token
                return new_token
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Teams token refresh unreachable: {e}") from e

    def test_connection(self) -> Dict[str, Any]:
        base_url = self._get_base_url()
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{base_url}/me", headers=self._get_headers())
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Teams connection check returned HTTP {res.status_code}")
                user_info = res.json()
                return {"status": "ok", "user": user_info.get("userPrincipalName", "Unknown")}
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Microsoft Teams Graph unreachable: {e}") from e

    def list_rooms(self, include_dms: bool = True) -> List[ChatRoomSummary]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        rooms: List[ChatRoomSummary] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                # 1. 1:1 and Group chats
                if include_dms:
                    c_res = client.get(f"{base_url}/me/chats?$top=50", headers=headers)
                    if c_res.status_code == 200:
                        for c in c_res.json().get("value", []):
                            chat_type = c.get("chatType", "")
                            is_dm = chat_type in ("oneOnOne", "group")
                            name = c.get("topic") or ("Direct Message" if chat_type == "oneOnOne" else "Group Chat")
                            rooms.append(
                                ChatRoomSummary(
                                    room_id=c.get("id", ""),
                                    account_id=self.config.id,
                                    adapter_type="teams",
                                    room_name=name,
                                    is_dm=is_dm,
                                    participant_count=0,
                                    unread_count=0,
                                    subscribed=True,
                                )
                            )

                # 2. Joined Teams and channels
                t_res = client.get(f"{base_url}/me/joinedTeams", headers=headers)
                if t_res.status_code == 200:
                    for team in t_res.json().get("value", []):
                        team_id = team.get("id", "")
                        team_name = team.get("displayName", "Team")
                        ch_res = client.get(f"{base_url}/teams/{team_id}/channels", headers=headers)
                        if ch_res.status_code == 200:
                            for ch in ch_res.json().get("value", []):
                                ch_id = ch.get("id", "")
                                ch_name = f"{team_name} / {ch.get('displayName', 'Channel')}"
                                rooms.append(
                                    ChatRoomSummary(
                                        room_id=f"channel:{team_id}:{ch_id}",
                                        account_id=self.config.id,
                                        adapter_type="teams",
                                        room_name=ch_name,
                                        is_dm=False,
                                        participant_count=0,
                                        unread_count=0,
                                        subscribed=True,
                                    )
                                )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Teams list_rooms failed: {e}") from e
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

        if room_id.startswith("channel:"):
            _, team_id, ch_id = room_id.split(":", 2)
            url = f"{base_url}/teams/{team_id}/channels/{ch_id}/messages"
        else:
            url = f"{base_url}/chats/{room_id}/messages"

        params: Dict[str, Any] = {"$top": min(limit, 50)}
        target_url = cursor if cursor else url

        messages: List[ChatMessageItem] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(target_url, headers=headers, params=params if not cursor else None)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Teams fetch_messages failed: HTTP {res.status_code}")
                data = res.json()
                raw_msgs = data.get("value", [])
                for rm in raw_msgs:
                    created_str = rm.get("createdDateTime", "")
                    try:
                        sent_ts = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    except Exception:
                        sent_ts = datetime.now(timezone.utc)

                    if since_utc and sent_ts < since_utc:
                        continue

                    sender_info = rm.get("from", {}).get("user", {})
                    body_content = rm.get("body", {}).get("content", "")

                    messages.append(
                        ChatMessageItem(
                            message_id=rm.get("id", ""),
                            doc_id=f"teams_{self.config.id}_{rm.get('id', '')}",
                            account_id=self.config.id,
                            adapter_type="teams",
                            room_id=room_id,
                            room_name="",
                            is_dm=not room_id.startswith("channel:"),
                            sender_id=sender_info.get("id", ""),
                            sender_name=sender_info.get("displayName", "Unknown"),
                            thread_root_id=rm.get("replyToId"),
                            sent_ts_utc=sent_ts,
                            body_text=body_content,
                            reactions={},
                            attachments=rm.get("attachments", []),
                        )
                    )
                next_link = data.get("@odata.nextLink")
                return messages, next_link
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Failed to fetch Teams messages: {e}") from e

    def send_message(self, msg: OutgoingChatMessage) -> Dict[str, Any]:
        base_url = self._get_base_url()
        headers = self._get_headers()
        body = {"body": {"content": msg.body_text}}

        if msg.room_id.startswith("channel:"):
            _, team_id, ch_id = msg.room_id.split(":", 2)
            if msg.thread_root_id:
                url = f"{base_url}/teams/{team_id}/channels/{ch_id}/messages/{msg.thread_root_id}/replies"
            else:
                url = f"{base_url}/teams/{team_id}/channels/{ch_id}/messages"
        else:
            url = f"{base_url}/chats/{msg.room_id}/messages"

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(url, headers=headers, json=body)
                if res.status_code >= 400:
                    raise ServiceUnavailableError(f"Teams post message failed: HTTP {res.status_code}")
                return res.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise ServiceUnavailableError(f"Teams send error: {e}") from e
