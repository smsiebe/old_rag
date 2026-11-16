/* ==========================================================================
   OldRAG Workspace: Mail Client & Viewer (Pillar 1)
   ========================================================================== */

(function (global) {
  "use strict";

  const api = global.OldRAGApi;

  let currentFolder = "ALL";
  let currentAccountId = "ALL";
  let selectedDocId = null;

  function initMailView() {
    const folderButtons = document.querySelectorAll(".mail-folders-list .folder-btn");
    folderButtons.forEach((btn) => {
      btn.addEventListener("click", function () {
        folderButtons.forEach((b) => b.classList.remove("active"));
        this.classList.add("active");
        currentFolder = this.dataset.folder || "ALL";
        loadMailMessages();
      });
    });

    const accountSelect = document.getElementById("mail-account-select");
    if (accountSelect) {
      accountSelect.addEventListener("change", function () {
        currentAccountId = this.value;
        loadMailMessages();
      });
    }

    const composeBtn = document.getElementById("mail-compose-btn");
    if (composeBtn) {
      composeBtn.addEventListener("click", function () {
        showComposeModal();
      });
    }

    loadMailAccounts();
    loadMailMessages();
  }

  async function loadMailAccounts() {
    const select = document.getElementById("mail-account-select");
    if (!select) return;
    try {
      const resp = await api.listAccounts();
      const data = resp.ok ? resp.data : resp;
      const accounts = (data && data.accounts) || [];
      select.innerHTML = '<option value="ALL">All Accounts (Unified)</option>';
      accounts.forEach((acc) => {
        const opt = document.createElement("option");
        opt.value = acc.id || acc.account_id || "";
        opt.textContent = acc.name || acc.email_address || acc.username || acc.id;
        select.appendChild(opt);
      });
    } catch (e) {
      console.error("Failed to load accounts:", e);
    }
  }

  async function loadMailMessages() {
    const container = document.getElementById("mail-messages-container") || document.getElementById("mail-items-list");
    if (!container) return;

    container.innerHTML = '<div class="loading-state">Loading messages...</div>';

    const payload = {
      folder: currentFolder === "ALL" ? null : currentFolder,
      account_id: currentAccountId === "ALL" ? null : currentAccountId,
      limit: 50,
      offset: 0,
    };

    try {
      const resp = await api.listMail(payload);
      const data = resp.ok ? resp.data : resp;
      const messages = (data && data.messages) || [];
      renderMailList(messages);
    } catch (err) {
      container.innerHTML = `<div class="error-state">Failed to load mail: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  function renderMailList(messages) {
    const container = document.getElementById("mail-messages-container") || document.getElementById("mail-items-list");
    if (!container) return;
    if (messages.length === 0) {
      container.innerHTML = '<div class="empty-state">No messages in this folder.</div>';
      return;
    }

    let html = "";
    messages.forEach((msg) => {
      const isSelected = msg.doc_id === selectedDocId;
      html += `
        <div class="mail-item-row ${isSelected ? "selected" : ""} ${msg.is_read ? "" : "unread"}" data-doc-id="${api.escapeHtml(msg.doc_id)}">
          <div class="mail-item-top">
            <span class="mail-sender">${api.escapeHtml(msg.sender || "Unknown Sender")}</span>
            <span class="mail-date">${api.formatTimestamp(msg.received_ts_utc)}</span>
          </div>
          <div class="mail-subject">${api.escapeHtml(msg.subject || "(No Subject)")}</div>
          <div class="mail-snippet">${api.escapeHtml(msg.snippet || "")}</div>
        </div>
      `;
    });

    container.innerHTML = html;

    container.querySelectorAll(".mail-item-row").forEach((row) => {
      row.addEventListener("click", function () {
        container.querySelectorAll(".mail-item-row").forEach((r) => r.classList.remove("selected"));
        this.classList.add("selected");
        const docId = this.dataset.docId;
        selectedDocId = docId;
        loadMessageDetail(docId);
      });
    });
  }

  async function loadMessageDetail(docId) {
    const pane = document.getElementById("mail-reading-pane");
    if (!pane) return;
    pane.innerHTML = '<div class="loading-state">Loading message contents...</div>';

    try {
      const resp = await api.getMailDetail(docId);
      const data = resp.ok ? resp.data : resp;
      const msg = data && data.message ? data.message : data;
      if (!msg) {
        pane.innerHTML = '<div class="error-state">Message not found.</div>';
        return;
      }

      api.recordAccess(docId);

      const attachments = msg.attachments || [];
      let attHtml = "";
      if (attachments.length > 0) {
        attHtml = '<div class="mail-attachments-bar"><strong>Attachments:</strong> ';
        attachments.forEach((att) => {
          attHtml += `<span class="attachment-pill">📎 ${api.escapeHtml(att.filename || "file")} (${api.formatBytes(att.size_bytes || 0)})</span> `;
        });
        attHtml += "</div>";
      }

      pane.innerHTML = `
        <article class="message-detail-view">
          <header class="message-detail-header">
            <h2>${api.escapeHtml(msg.subject || "(No Subject)")}</h2>
            <div class="message-detail-meta">
              <div><strong>From:</strong> ${api.escapeHtml(msg.sender || "Unknown")}</div>
              <div><strong>Date:</strong> ${api.formatTimestamp(msg.received_ts_utc)}</div>
              <div><strong>Folder:</strong> ${api.escapeHtml(msg.folder || "Inbox")}</div>
            </div>
            <div class="message-toolbar">
              <button id="btn-open-msg-file" class="btn btn-sm btn-outline">📂 Open in Outlook (.msg)</button>
              <button id="btn-mail-related" class="btn btn-sm btn-outline">🔗 Related Context</button>
            </div>
          </header>
          ${attHtml}
          <div class="message-body-content">
            ${msg.clean_body_text ? `<pre class="body-text">${api.escapeHtml(msg.clean_body_text)}</pre>` : "<em>No message body text.</em>"}
          </div>
        </article>
      `;

      const openMsgBtn = document.getElementById("btn-open-msg-file");
      if (openMsgBtn) {
        openMsgBtn.addEventListener("click", function () {
          api.openCachedMsg(docId);
        });
      }

      const relBtn = document.getElementById("btn-mail-related");
      if (relBtn && global.OldRAGSearchView) {
        relBtn.addEventListener("click", function () {
          global.OldRAGSearchView.showRelatedDrawer(docId, msg.subject);
        });
      }
    } catch (err) {
      pane.innerHTML = `<div class="error-state">Error loading message: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  function showComposeModal() {
    const modal = document.getElementById("compose-modal");
    if (!modal) return;
    modal.classList.add("open");
  }

  global.OldRAGMailView = {
    init: initMailView,
    loadMessages: loadMailMessages,
    loadDetail: loadMessageDetail,
  };
})(window);
