/* ==========================================================================
   OldRAG Workspace: Chat Adapter Plugins & Workspace (Pillar 2)
   ========================================================================== */

(function (global) {
  "use strict";

  const api = global.OldRAGApi;

  let activeRoomId = null;
  let activeAccountId = null;
  let activeRoomName = null;
  let activeAdapterLabel = null;
  let chatPlugins = [];
  let chatAccounts = [];

  function initChatView() {
    const roomSubBtn = document.getElementById("btn-manage-subscriptions");
    if (roomSubBtn) {
      roomSubBtn.addEventListener("click", showSubscriptionModal);
    }

    const popoutBtn = document.getElementById("chat-popout-btn");
    if (popoutBtn) {
      popoutBtn.addEventListener("click", function () {
        if (global.OldRAGWindowManager && activeRoomId) {
          global.OldRAGWindowManager.openWindow({
            id: "chat_" + activeRoomId,
            type: "chat",
            target_id: activeRoomId,
            title: "Chat: " + (activeRoomName || activeRoomId),
            subBadge: activeAdapterLabel || "Chat",
            auto_pop: false,
            sound_enabled: false,
          });
        }
      });
    }

    const sendBtn = document.getElementById("btn-send-chat-msg");
    const chatInput = document.getElementById("chat-message-input");
    if (sendBtn && chatInput) {
      sendBtn.addEventListener("click", sendActiveChatMessage);
      chatInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendActiveChatMessage();
        }
      });
    }

    loadPluginsAndAccounts();
  }

  async function loadPluginsAndAccounts() {
    try {
      const [pluginsResp, accountsResp] = await Promise.all([
        api.listChatPlugins(),
        api.listChatAccounts(),
      ]);

      const pData = pluginsResp.ok ? pluginsResp.data : pluginsResp;
      chatPlugins = (pData && pData.plugins) || [];

      const aData = accountsResp.ok ? accountsResp.data : accountsResp;
      chatAccounts = (aData && aData.accounts) || [];

      loadChatRooms();
    } catch (e) {
      console.error("Failed to load chat metadata:", e);
    }
  }

  async function loadChatRooms() {
    const listContainer = document.getElementById("chat-rooms-tree");
    if (!listContainer) return;

    listContainer.innerHTML = '<div class="loading-state">Loading chat rooms...</div>';

    try {
      const resp = await api.listChatRooms();
      const data = resp.ok ? resp.data : resp;
      const rooms = (data && data.rooms) || [];
      renderRoomsRail(rooms);
    } catch (err) {
      listContainer.innerHTML = `<div class="error-state">Failed to load rooms: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  function renderRoomsRail(rooms) {
    const container = document.getElementById("chat-rooms-tree");
    if (!container) return;

    if (rooms.length === 0) {
      container.innerHTML = '<div class="empty-state">No chat rooms configured.<br><span class="form-hint" style="display:block;margin-top:6px;">Add chat accounts in the Admin tab to connect Rocket.Chat, Google Chat, or Slack.</span></div>';
      return;
    }

    // Group rooms by adapter_type -> is_dm
    const grouped = {};
    rooms.forEach((r) => {
      const at = r.adapter_type || "chat";
      if (!grouped[at]) grouped[at] = { dms: [], channels: [] };
      if (r.is_dm) {
        grouped[at].dms.push(r);
      } else {
        grouped[at].channels.push(r);
      }
    });

    let html = "";
    Object.keys(grouped).forEach((adapter) => {
      const g = grouped[adapter];
      let icon = "💬";
      if (adapter === "rocketchat") icon = "🚀";
      else if (adapter === "google_chat") icon = "🌐";
      else if (adapter === "teams") icon = "👥";
      else if (adapter === "slack") icon = "💬";
      const adapterLabel = icon + " " + adapter.replace(/_/g, " ").toUpperCase();

      html += `<div class="chat-adapter-group">`;
      html += `<div class="adapter-group-header">${api.escapeHtml(adapterLabel)}</div>`;

      if (g.channels.length > 0) {
        html += `<div class="sub-group-title">Channels / Rooms</div>`;
        g.channels.forEach((rm) => {
          const isAct = rm.room_id === activeRoomId;
          html += `
            <div class="chat-room-item ${isAct ? "active" : ""}" data-room-id="${api.escapeHtml(rm.room_id)}" data-account-id="${api.escapeHtml(rm.account_id)}" data-room-name="${api.escapeHtml(rm.room_name)}" data-adapter-label="${api.escapeHtml(adapterLabel)}">
              <span class="room-icon">#</span>
              <span class="room-name">${api.escapeHtml(rm.room_name)}</span>
              ${rm.unread_count > 0 ? `<span class="unread-pill">${rm.unread_count}</span>` : ""}
            </div>
          `;
        });
      }

      if (g.dms.length > 0) {
        html += `<div class="sub-group-title">Direct Messages</div>`;
        g.dms.forEach((rm) => {
          const isAct = rm.room_id === activeRoomId;
          html += `
            <div class="chat-room-item ${isAct ? "active" : ""}" data-room-id="${api.escapeHtml(rm.room_id)}" data-account-id="${api.escapeHtml(rm.account_id)}" data-room-name="${api.escapeHtml(rm.room_name)}" data-adapter-label="${api.escapeHtml(adapterLabel)}">
              <span class="room-icon">👤</span>
              <span class="room-name">${api.escapeHtml(rm.room_name)}</span>
            </div>
          `;
        });
      }

      html += `</div>`;
    });

    container.innerHTML = html;

    container.querySelectorAll(".chat-room-item").forEach((item) => {
      item.addEventListener("click", function () {
        container.querySelectorAll(".chat-room-item").forEach((it) => it.classList.remove("active"));
        this.classList.add("active");
        activeRoomId = this.dataset.roomId;
        activeAccountId = this.dataset.accountId;
        activeRoomName = this.dataset.roomName || this.dataset.roomId;
        activeAdapterLabel = this.dataset.adapterLabel || "Chat";

        // Update header badges
        const sourceBadge = document.getElementById("chat-source-badge");
        if (sourceBadge) {
          sourceBadge.textContent = activeAdapterLabel;
          sourceBadge.style.display = "inline-block";
        }
        const popoutBtn = document.getElementById("chat-popout-btn");
        if (popoutBtn) popoutBtn.style.display = "inline-block";

        loadChatMessages(activeRoomId, activeRoomName);
      });
    });
  }

  async function loadChatMessages(roomId, roomName) {
    const headerTitle = document.getElementById("chat-active-title");
    const container = document.getElementById("chat-messages-timeline");
    if (!container) return;

    if (headerTitle) headerTitle.textContent = roomName ? `#${roomName}` : `Room: ${roomId}`;
    container.innerHTML = '<div class="loading-state">Fetching message timeline...</div>';

    try {
      const resp = await api.listChatMessages({ room_id: roomId, limit: 100 });
      const data = resp.ok ? resp.data : resp;
      const messages = (data && data.messages) || [];
      renderMessagesTimeline(messages);
    } catch (err) {
      container.innerHTML = `<div class="error-state">Failed to load messages: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  function renderMessagesTimeline(messages) {
    const container = document.getElementById("chat-messages-timeline");
    if (!container) return;

    if (messages.length === 0) {
      container.innerHTML = '<div class="empty-state">No messages in this room yet.</div>';
      return;
    }

    let html = "";
    messages.forEach((msg) => {
      html += `
        <div class="chat-bubble-row" data-msg-id="${api.escapeHtml(msg.message_id)}">
          <div class="chat-avatar">${api.escapeHtml((msg.sender_name || msg.sender_id || "U").substring(0, 2).toUpperCase())}</div>
          <div class="chat-bubble-content">
            <div class="chat-bubble-meta">
              <span class="chat-sender">${api.escapeHtml(msg.sender_name || msg.sender_id || "Unknown")}</span>
              <span class="chat-time">${api.formatTimestamp(msg.timestamp_utc)}</span>
            </div>
            <div class="chat-text">${api.escapeHtml(msg.text || "")}</div>
          </div>
        </div>
      `;
    });

    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
  }

  async function sendActiveChatMessage() {
    const input = document.getElementById("chat-message-input");
    if (!input || !activeRoomId) return;
    const text = input.value.trim();
    if (!text) return;

    input.value = "";
    try {
      await api.sendChatMessage({
        room_id: activeRoomId,
        account_id: activeAccountId,
        text: text,
      });
      loadChatMessages(activeRoomId);
    } catch (e) {
      alert("Failed to send message: " + e.message);
    }
  }

  function showAccountSetupModal() {
    const modal = document.getElementById("chat-account-modal");
    if (!modal) return;
    modal.classList.add("open");
  }

  function showSubscriptionModal() {
    const modal = document.getElementById("chat-subscriptions-modal");
    if (!modal) return;
    modal.classList.add("open");
  }

  global.OldRAGChatView = {
    init: initChatView,
    loadRooms: loadChatRooms,
    loadMessages: loadChatMessages,
  };
})(window);
