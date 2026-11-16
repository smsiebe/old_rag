/* ==========================================================================
   OldRAG Floating MDI Dashboard Window Manager
   Draggable, Resizable Windows | Channel Pop-Out | Auto-Pop & Offline Web Audio
   Zero External Dependencies | 100% Offline Desktop Operation
   ========================================================================== */

(function (global) {
  "use strict";

  const STORAGE_KEY = "oldrag_dashboard_layout";
  let activeWindows = {};
  let zIndexCounter = 1100;
  let audioCtx = null;

  function getAudioContext() {
    if (!audioCtx) {
      const AudioContextClass = global.AudioContext || global.webkitAudioContext;
      if (AudioContextClass) {
        audioCtx = new AudioContextClass();
      }
    }
    if (audioCtx && audioCtx.state === "suspended") {
      audioCtx.resume();
    }
    return audioCtx;
  }

  function playNotificationChime() {
    try {
      const ctx = getAudioContext();
      if (!ctx) return;
      const now = ctx.currentTime;

      // Two-tone pleasant notification chime (C5 -> E5)
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      osc.type = "sine";
      osc.frequency.setValueAtTime(523.25, now); // C5
      osc.frequency.setValueAtTime(659.25, now + 0.12); // E5

      gain.gain.setValueAtTime(0.001, now);
      gain.gain.exponentialRampToValueAtTime(0.18, now + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.42);

      osc.connect(gain);
      gain.connect(ctx.destination);

      osc.start(now);
      osc.stop(now + 0.45);
    } catch (e) {
      console.warn("Could not play notification chime:", e);
    }
  }

  function getOrCreateContainer() {
    let container = document.getElementById("mdi-windows-root");
    if (!container) {
      container = document.createElement("div");
      container.id = "mdi-windows-root";
      container.className = "mdi-windows-root";
      document.body.appendChild(container);
    }
    return container;
  }

  function createFloatingWindow(config) {
    const winId = config.id || ("win_" + Math.random().toString(36).substr(2, 9));
    if (activeWindows[winId]) {
      bringToFront(winId);
      if (activeWindows[winId].element.classList.contains("minimized")) {
        activeWindows[winId].element.classList.remove("minimized");
      }
      return activeWindows[winId];
    }

    const container = getOrCreateContainer();
    const winEl = document.createElement("div");
    winEl.className = "floating-window";
    winEl.id = winId;
    winEl.dataset.type = config.type || "custom";
    winEl.dataset.targetId = config.target_id || "";

    const left = config.x !== undefined ? config.x : (100 + (Object.keys(activeWindows).length * 30) % 300);
    const top = config.y !== undefined ? config.y : (100 + (Object.keys(activeWindows).length * 30) % 250);
    const width = config.width || (config.type === "chat" ? 440 : (config.type === "search" ? 540 : 500));
    const height = config.height || (config.type === "chat" ? 520 : 480);

    winEl.style.left = Math.max(10, Math.min(window.innerWidth - 120, left)) + "px";
    winEl.style.top = Math.max(50, Math.min(window.innerHeight - 100, top)) + "px";
    winEl.style.width = width + "px";
    winEl.style.height = height + "px";
    winEl.style.zIndex = ++zIndexCounter;

    const icon = config.type === "chat" ? "💬" : (config.type === "mail" ? "📧" : (config.type === "calendar" ? "📅" : "🔍"));
    const title = config.title || "Window";
    const subBadge = config.subBadge ? `<span class="win-sub-badge">${escapeHtml(config.subBadge)}</span>` : "";

    winEl.innerHTML = `
      <div class="floating-window-header" id="${winId}-header">
        <div class="floating-window-title">
          <span class="win-icon">${icon}</span>
          <span class="win-title-text" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
          ${subBadge}
        </div>
        <div class="floating-window-controls">
          <button type="button" class="win-ctrl-btn btn-settings" title="Notification &amp; Pop-up Settings">⚙️</button>
          <button type="button" class="win-ctrl-btn btn-minimize" title="Minimize">_</button>
          <button type="button" class="win-ctrl-btn btn-maximize" title="Maximize">🗖</button>
          <button type="button" class="win-ctrl-btn btn-close" title="Close and dock back">✕</button>
        </div>
      </div>
      <div class="floating-window-settings-panel" style="display: none;">
        <div class="win-settings-row">
          <label>
            <input type="checkbox" class="chk-auto-pop" ${config.auto_pop ? "checked" : ""}>
            Auto pop-up when new message arrives
          </label>
        </div>
        <div class="win-settings-row">
          <label>
            <input type="checkbox" class="chk-sound" ${config.sound_enabled ? "checked" : ""}>
            Play notification chime on new message
          </label>
          <button type="button" class="btn btn-sm btn-secondary btn-test-chime" style="margin-left:8px;">🔔 Test Chime</button>
        </div>
      </div>
      <div class="floating-window-body" id="${winId}-body">
        <div class="loading-placeholder">Loading component content...</div>
      </div>
    `;

    container.appendChild(winEl);

    const winObj = {
      id: winId,
      type: config.type,
      target_id: config.target_id || "",
      title: title,
      subBadge: config.subBadge || "",
      element: winEl,
      auto_pop: Boolean(config.auto_pop),
      sound_enabled: Boolean(config.sound_enabled),
      contentRenderer: config.renderContent || null,
    };
    activeWindows[winId] = winObj;

    // Header interaction: Focus and Drag
    const headerEl = winEl.querySelector(".floating-window-header");
    winEl.addEventListener("mousedown", () => bringToFront(winId));
    setupDraggable(winEl, headerEl);

    // Controls setup
    const settingsBtn = winEl.querySelector(".btn-settings");
    const settingsPanel = winEl.querySelector(".floating-window-settings-panel");
    settingsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      settingsPanel.style.display = settingsPanel.style.display === "none" ? "block" : "none";
    });

    const chkAutoPop = winEl.querySelector(".chk-auto-pop");
    chkAutoPop.addEventListener("change", () => {
      winObj.auto_pop = chkAutoPop.checked;
      saveLayout();
    });

    const chkSound = winEl.querySelector(".chk-sound");
    chkSound.addEventListener("change", () => {
      winObj.sound_enabled = chkSound.checked;
      saveLayout();
    });

    const testChimeBtn = winEl.querySelector(".btn-test-chime");
    testChimeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      playNotificationChime();
    });

    const minBtn = winEl.querySelector(".btn-minimize");
    minBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      winEl.classList.toggle("minimized");
    });

    const maxBtn = winEl.querySelector(".btn-maximize");
    maxBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      winEl.classList.toggle("maximized");
    });

    const closeBtn = winEl.querySelector(".btn-close");
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      closeWindow(winId);
    });

    // Populate body
    const bodyEl = winEl.querySelector(".floating-window-body");
    if (typeof config.renderContent === "function") {
      config.renderContent(bodyEl, winObj);
    } else {
      renderDefaultComponentContent(config.type, config.target_id, bodyEl, winObj);
    }

    saveLayout();
    return winObj;
  }

  function setupDraggable(el, handle) {
    let startX = 0, startY = 0, initialLeft = 0, initialTop = 0;
    let isDragging = false;

    handle.addEventListener("mousedown", (e) => {
      if (e.target.closest("button") || e.target.closest("input")) return;
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      initialLeft = el.offsetLeft;
      initialTop = el.offsetTop;
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
      e.preventDefault();
    });

    function onMouseMove(e) {
      if (!isDragging) return;
      if (el.classList.contains("maximized")) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      const newLeft = Math.max(0, Math.min(window.innerWidth - 80, initialLeft + dx));
      const newTop = Math.max(40, Math.min(window.innerHeight - 60, initialTop + dy));
      el.style.left = newLeft + "px";
      el.style.top = newTop + "px";
    }

    function onMouseUp() {
      if (!isDragging) return;
      isDragging = false;
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      saveLayout();
    }
  }

  function bringToFront(winId) {
    const win = activeWindows[winId];
    if (win && win.element) {
      win.element.style.zIndex = ++zIndexCounter;
    }
  }

  function closeWindow(winId) {
    const win = activeWindows[winId];
    if (win && win.element) {
      win.element.remove();
      delete activeWindows[winId];
      saveLayout();
    }
  }

  function closeAll() {
    Object.keys(activeWindows).forEach(closeWindow);
  }

  function onNewMessage(roomId, message) {
    // Check if any open window corresponds to this room
    let targetWin = null;
    for (const id in activeWindows) {
      const win = activeWindows[id];
      if (win.type === "chat" && win.target_id === roomId) {
        targetWin = win;
        break;
      }
    }

    if (targetWin) {
      if (targetWin.sound_enabled) {
        playNotificationChime();
      }
      if (targetWin.auto_pop) {
        bringToFront(targetWin.id);
        if (targetWin.element.classList.contains("minimized")) {
          targetWin.element.classList.remove("minimized");
        }
        targetWin.element.classList.add("flash-highlight");
        setTimeout(() => targetWin.element.classList.remove("flash-highlight"), 1200);
      }
      // Re-render conversation inside window if present
      const bodyEl = targetWin.element.querySelector(".floating-window-body");
      if (bodyEl && typeof targetWin.refreshMessages === "function") {
        targetWin.refreshMessages();
      }
    }
  }

  function renderDefaultComponentContent(type, targetId, container, winObj) {
    if (type === "chat") {
      renderChatInWindow(targetId, container, winObj);
    } else if (type === "mail") {
      renderMailInWindow(targetId, container, winObj);
    } else if (type === "calendar") {
      renderCalendarInWindow(targetId, container, winObj);
    } else if (type === "search") {
      renderSearchInWindow(container, winObj);
    } else {
      container.innerHTML = `<div class="p-3">Component: ${escapeHtml(type)} [${escapeHtml(targetId || "unified")}]</div>`;
    }
  }

  function renderChatInWindow(roomId, container, winObj) {
    container.innerHTML = `
      <div class="popout-chat-layout">
        <div class="popout-chat-msgs-timeline" id="${winObj.id}-timeline">
          <div class="loading-placeholder">Loading messages for room...</div>
        </div>
        <div class="popout-chat-input-bar">
          <input type="text" class="form-input popout-input" placeholder="Type a message...">
          <button type="button" class="btn btn-primary popout-send-btn">Send</button>
        </div>
      </div>
    `;

    const timelineEl = container.querySelector(".popout-chat-msgs-timeline");
    const inputEl = container.querySelector(".popout-input");
    const sendBtn = container.querySelector(".popout-send-btn");

    function refresh() {
      if (!roomId) {
        timelineEl.innerHTML = `<div class="empty-state">No chat room specified.</div>`;
        return;
      }
      if (global.OldRAGApi) {
        global.OldRAGApi.listChatMessages({ room_id: roomId, limit: 50 }).then((res) => {
          const msgs = res.messages || [];
          if (msgs.length === 0) {
            timelineEl.innerHTML = `<div class="empty-state"><p>No messages yet in this channel.</p></div>`;
            return;
          }
          let html = "";
          msgs.forEach((m) => {
            const sender = escapeHtml(m.sender_name || m.sender_id || "User");
            const time = m.sent_ts_utc ? new Date(m.sent_ts_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
            html += `
              <div class="chat-msg-row">
                <div class="chat-msg-header">
                  <span class="chat-sender-name">${sender}</span>
                  <span class="chat-time-stamp">${time}</span>
                </div>
                <div class="chat-msg-body">${escapeHtml(m.body_text || "")}</div>
              </div>
            `;
          });
          timelineEl.innerHTML = html;
          timelineEl.scrollTop = timelineEl.scrollHeight;
        }).catch((err) => {
          timelineEl.innerHTML = `<div class="alert-box alert-warning">Failed to load chat: ${escapeHtml(err.message)}</div>`;
        });
      }
    }

    winObj.refreshMessages = refresh;
    refresh();

    function doSend() {
      const text = inputEl.value.trim();
      if (!text || !global.OldRAGApi) return;
      global.OldRAGApi.sendChatMessage({ room_id: roomId, body_text: text }).then(() => {
        inputEl.value = "";
        refresh();
      }).catch((err) => {
        alert("Send failed: " + err.message);
      });
    }

    sendBtn.addEventListener("click", doSend);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSend();
    });
  }

  function renderMailInWindow(folder, container, winObj) {
    container.innerHTML = `
      <div class="popout-mail-layout">
        <div class="popout-mail-list" id="${winObj.id}-maillist">
          <div class="loading-placeholder">Loading mail for ${escapeHtml(folder || "All")}...</div>
        </div>
      </div>
    `;
    const listEl = container.querySelector(".popout-mail-list");
    if (global.OldRAGApi) {
      global.OldRAGApi.listMail({ folder: folder === "ALL" ? null : folder, limit: 30 }).then((res) => {
        const msgs = res.messages || [];
        if (msgs.length === 0) {
          listEl.innerHTML = `<div class="empty-state"><p>No messages in ${escapeHtml(folder || "Inbox")}.</p></div>`;
          return;
        }
        let html = "";
        msgs.forEach((m) => {
          const sender = escapeHtml(m.sender || "Unknown");
          const subj = escapeHtml(m.subject || "(No Subject)");
          const time = m.received_ts_utc ? new Date(m.received_ts_utc).toLocaleDateString() : "";
          html += `
            <div class="popout-mail-item" style="padding:6px 8px; border-bottom:1px solid var(--border-subtle); cursor:pointer;">
              <div style="display:flex; justify-content:space-between; font-size:12px;">
                <strong>${sender}</strong><span style="color:var(--text-muted);">${time}</span>
              </div>
              <div style="font-size:13px; color:var(--text-primary); text-overflow:ellipsis; overflow:hidden; white-space:nowrap;">${subj}</div>
            </div>
          `;
        });
        listEl.innerHTML = html;
      }).catch((err) => {
        listEl.innerHTML = `<div class="alert-box alert-warning">Error: ${escapeHtml(err.message)}</div>`;
      });
    }
  }

  function renderCalendarInWindow(calendarId, container, winObj) {
    container.innerHTML = `
      <div class="popout-cal-layout">
        <div class="popout-cal-list" id="${winObj.id}-callist">
          <div class="loading-placeholder">Loading calendar events...</div>
        </div>
      </div>
    `;
    const listEl = container.querySelector(".popout-cal-list");
    if (global.OldRAGApi) {
      global.OldRAGApi.listCalendarEvents({ calendar_id: calendarId === "unified" ? null : calendarId, limit: 40 }).then((res) => {
        const events = res.events || [];
        if (events.length === 0) {
          listEl.innerHTML = `<div class="empty-state"><p>No upcoming events.</p></div>`;
          return;
        }
        let html = "";
        events.forEach((e) => {
          const color = e.calendar_color || "#339af0";
          const sys = e.source_system || "Calendar";
          const subj = escapeHtml(e.subject || "Event");
          const start = e.start_ts_utc ? new Date(e.start_ts_utc).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "";
          const loc = e.location ? `<div style="font-size:11px; color:var(--text-muted);">📍 ${escapeHtml(e.location)}</div>` : "";
          html += `
            <div class="popout-cal-item" style="padding:8px; border-left:4px solid ${color}; background:var(--bg-surface); margin-bottom:6px; border-radius:4px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong style="font-size:13px;">${subj}</strong>
                <span class="badge" style="background:${color}22; color:${color}; font-size:10px;">${escapeHtml(sys)}</span>
              </div>
              <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">🕒 ${start}</div>
              ${loc}
            </div>
          `;
        });
        listEl.innerHTML = html;
      }).catch((err) => {
        listEl.innerHTML = `<div class="alert-box alert-warning">Error: ${escapeHtml(err.message)}</div>`;
      });
    }
  }

  function renderSearchInWindow(container, winObj) {
    container.innerHTML = `
      <div class="popout-search-layout" style="display:flex; flex-direction:column; height:100%;">
        <div style="display:flex; gap:6px; margin-bottom:8px;">
          <input type="text" class="form-input popout-search-input" placeholder="Type search query...">
          <button type="button" class="btn btn-primary popout-search-btn">Search</button>
        </div>
        <div class="popout-search-results" style="flex:1; overflow:auto;">
          <div class="empty-state">Enter query to search indexed documents &amp; mail.</div>
        </div>
      </div>
    `;
    const inputEl = container.querySelector(".popout-search-input");
    const searchBtn = container.querySelector(".popout-search-btn");
    const resultsEl = container.querySelector(".popout-search-results");

    function doSearch() {
      const q = inputEl.value.trim();
      if (!q || !global.OldRAGApi) return;
      resultsEl.innerHTML = `<div class="loading-placeholder">Searching...</div>`;
      global.OldRAGApi.search({ query_text: q, top_k: 25 }).then((res) => {
        const hits = res.results || [];
        if (hits.length === 0) {
          resultsEl.innerHTML = `<div class="empty-state">No matching results found.</div>`;
          return;
        }
        let html = "";
        hits.forEach((h) => {
          const subj = escapeHtml(h.subject || h.folder || "Document");
          const conf = Math.round((h.confidence_score || h.normalized_rrf || 0) * 100);
          const icon = h.source_type === "EMAIL" ? "📧" : (h.source_type === "CHAT" ? "💬" : (h.source_type === "CALENDAR" ? "📅" : "📁"));
          html += `
            <div style="padding:6px; border-bottom:1px solid var(--border-subtle);">
              <div style="display:flex; justify-content:space-between; font-size:12px;">
                <span>${icon} <strong>${subj}</strong></span>
                <span class="badge badge-info">${conf}%</span>
              </div>
              <div style="font-size:12px; color:var(--text-muted); margin-top:2px;">${escapeHtml((h.content || "").slice(0, 90))}...</div>
            </div>
          `;
        });
        resultsEl.innerHTML = html;
      }).catch((err) => {
        resultsEl.innerHTML = `<div class="alert-box alert-warning">Search error: ${escapeHtml(err.message)}</div>`;
      });
    }

    searchBtn.addEventListener("click", doSearch);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch();
    });
  }

  function saveLayout() {
    const layoutWindows = [];
    for (const id in activeWindows) {
      const win = activeWindows[id];
      const el = win.element;
      if (!el) continue;
      layoutWindows.push({
        id: win.id,
        type: win.type,
        target_id: win.target_id,
        title: win.title,
        subBadge: win.subBadge,
        x: parseInt(el.style.left, 10) || 100,
        y: parseInt(el.style.top, 10) || 100,
        width: parseInt(el.style.width, 10) || 450,
        height: parseInt(el.style.height, 10) || 450,
        is_open: true,
        auto_pop: win.auto_pop,
        sound_enabled: win.sound_enabled,
      });
    }

    const payload = { windows: layoutWindows };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch (e) {
      // quota or local storage restriction
    }
    if (global.OldRAGApi && typeof global.OldRAGApi.saveDashboardLayout === "function") {
      global.OldRAGApi.saveDashboardLayout({ layout: payload }).catch(() => {});
    }
  }

  function restoreLayout() {
    let saved = null;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) saved = JSON.parse(raw);
    } catch (e) {}

    if (saved && Array.isArray(saved.windows) && saved.windows.length > 0) {
      saved.windows.forEach(createFloatingWindow);
      return;
    }

    if (global.OldRAGApi && typeof global.OldRAGApi.getDashboardLayout === "function") {
      global.OldRAGApi.getDashboardLayout().then((res) => {
        const layout = res.layout || res;
        if (layout && Array.isArray(layout.windows)) {
          layout.windows.forEach(createFloatingWindow);
        }
      }).catch(() => {});
    }
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Expose on window
  global.OldRAGWindowManager = {
    openWindow: createFloatingWindow,
    closeWindow: closeWindow,
    closeAll: closeAll,
    bringToFront: bringToFront,
    saveLayout: saveLayout,
    restoreLayout: restoreLayout,
    playNotificationChime: playNotificationChime,
    onNewMessage: onNewMessage,
    getActiveWindows: () => activeWindows,
  };

  // Auto restore layout once DOM is loaded
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => setTimeout(restoreLayout, 500));
  } else {
    setTimeout(restoreLayout, 500);
  }
})(window);

