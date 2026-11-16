/* ==========================================================================
   OldRAG Workspace: Real-Time Sync, Hard-Drive Quotas & Admin (Pillar 5)
   ========================================================================== */

(function (global) {
  "use strict";

  const api = global.OldRAGApi;

  function initSyncQuotaView() {
    const syncNowBtn = document.getElementById("btn-sync-all-now");
    if (syncNowBtn) {
      syncNowBtn.addEventListener("click", triggerManualSync);
    }

    const purgeNowBtn = document.getElementById("btn-quota-purge-now");
    if (purgeNowBtn) {
      purgeNowBtn.addEventListener("click", runActiveQuotaPurge);
    }

    const previewBtn = document.getElementById("btn-quota-preview-eviction");
    if (previewBtn) {
      previewBtn.addEventListener("click", previewActiveQuotaEviction);
    }

    const policySelect = document.getElementById("quota-policy-select");
    if (policySelect) {
      policySelect.addEventListener("change", updateCurrentQuotaPolicy);
    }

    const scanHofBtn = document.getElementById("btn-scan-head-of-folder");
    if (scanHofBtn) {
      scanHofBtn.addEventListener("click", triggerHeadOfFolderScan);
    }

    loadSyncAndQuotaData();
  }

  async function loadSyncAndQuotaData() {
    await Promise.all([
      loadSyncStatus(),
      loadQuotaCards(),
      loadAdminOverview(),
    ]);
  }

  async function loadSyncStatus() {
    const tableBody = document.getElementById("sync-status-tbody");
    if (!tableBody) return;

    try {
      const resp = await api.getSyncStatus();
      const data = resp.ok ? resp.data : resp;
      const statuses = (data && data.statuses) || [];

      if (statuses.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="5" class="empty-state">No sync connectors active.</td></tr>';
        return;
      }

      let html = "";
      statuses.forEach((st) => {
        html += `
          <tr>
            <td><strong>${api.escapeHtml(st.source_type)}</strong> (${api.escapeHtml(st.account_id || "default")})</td>
            <td><span class="status-badge badge-${(st.status || "IDLE").toLowerCase()}">${api.escapeHtml(st.status || "IDLE")}</span></td>
            <td>${api.formatTimestamp(st.low_watermark_utc) || "Initial"}</td>
            <td>${api.escapeHtml(st.high_watermark_cursor || "None")}</td>
            <td>
              <button class="btn btn-sm btn-outline btn-sync-one" data-source="${api.escapeHtml(st.source_type)}" data-account="${api.escapeHtml(st.account_id || "")}">
                Sync
              </button>
            </td>
          </tr>
        `;
      });

      tableBody.innerHTML = html;

      tableBody.querySelectorAll(".btn-sync-one").forEach((btn) => {
        btn.addEventListener("click", function () {
          api.triggerSync({ source_type: this.dataset.source, account_id: this.dataset.account || null });
        });
      });
    } catch (e) {
      tableBody.innerHTML = `<tr><td colspan="5" class="error-state">Failed to load sync status: ${api.escapeHtml(e.message)}</td></tr>`;
    }
  }

  async function loadQuotaCards() {
    const container = document.getElementById("quota-cards-container");
    if (!container) return;

    const sourceTypes = ["EMAIL", "CHAT", "CALENDAR", "FILE"];
    try {
      const results = await Promise.all(
        sourceTypes.map((st) => api.getQuotaStatus(st))
      );

      let html = "";
      results.forEach((res, idx) => {
        const data = res.ok ? res.data : res;
        const st = sourceTypes[idx];
        if (!data) return;

        const used = data.used_bytes || 0;
        const max = data.max_bytes || (10 * 1024 * 1024 * 1024);
        const ratio = Math.min(100, Math.round((used / max) * 100));
        const barClass = ratio > 90 ? "bar-danger" : ratio > 75 ? "bar-warning" : "bar-ok";

        html += `
          <div class="quota-card" data-source-type="${st}">
            <div class="quota-card-header">
              <h4>${st} Storage Quota</h4>
              <span class="quota-pct">${ratio}%</span>
            </div>
            <div class="quota-progress-track">
              <div class="quota-progress-fill ${barClass}" style="width: ${ratio}%;"></div>
            </div>
            <div class="quota-stats-grid">
              <div><strong>Used:</strong> ${api.formatBytes(used)}</div>
              <div><strong>Max:</strong> ${api.formatBytes(max)}</div>
              <div><strong>Docs:</strong> ${data.document_count || 0}</div>
              <div><strong>Shared Blobs:</strong> ${api.formatBytes(data.shared_blob_bytes || 0)}</div>
            </div>
            <div class="quota-policy-label">Policy: <code>${api.escapeHtml(data.policy || "COMPOSITE_SCORE")}</code></div>
          </div>
        `;
      });

      container.innerHTML = html;
    } catch (e) {
      container.innerHTML = `<div class="error-state">Failed to load quotas: ${api.escapeHtml(e.message)}</div>`;
    }
  }

  async function previewActiveQuotaEviction() {
    const select = document.getElementById("eviction-source-type-select");
    const st = select ? select.value : "EMAIL";
    const tbody = document.getElementById("eviction-preview-tbody");
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="5" class="loading-state">Scoring and ranking eviction candidates...</td></tr>';

    try {
      const resp = await api.previewQuotaEviction({ source_type: st, bytes_to_free: 50 * 1024 * 1024 });
      const data = resp.ok ? resp.data : resp;
      const candidates = (data && data.candidates) || [];

      if (candidates.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No candidates eligible for eviction (all protected or under quota).</td></tr>';
        return;
      }

      let html = "";
      candidates.forEach((c) => {
        html += `
          <tr>
            <td><code>${api.escapeHtml(c.doc_id.substring(0, 10))}...</code></td>
            <td>${api.escapeHtml(c.subject || "Untitled")}</td>
            <td>${api.formatBytes(c.byte_size)}</td>
            <td><span class="score-badge">${(c.retention_score || 0).toFixed(4)}</span></td>
            <td>${api.escapeHtml(c.reason || "Policy ranked")}</td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="error-state">Preview failed: ${api.escapeHtml(e.message)}</td></tr>`;
    }
  }

  async function runActiveQuotaPurge() {
    const select = document.getElementById("eviction-source-type-select");
    const st = select ? select.value : "EMAIL";

    if (!confirm(`Are you sure you want to run hard-drive quota purge on ${st}? CAS internal records will be deleted, but your source files will NEVER be modified.`)) {
      return;
    }

    try {
      const resp = await api.runQuotaPurge({ source_type: st, dry_run: false });
      const data = resp.ok ? resp.data : resp;
      alert(`Purge completed: ${api.formatBytes(data.freed_bytes || 0)} reclaimed!`);
      loadQuotaCards();
    } catch (e) {
      alert("Purge failed: " + e.message);
    }
  }

  async function updateCurrentQuotaPolicy() {
    const stSelect = document.getElementById("eviction-source-type-select");
    const pSelect = document.getElementById("quota-policy-select");
    if (!stSelect || !pSelect) return;

    try {
      await api.updateQuotaPolicy({
        config: {
          source_type: stSelect.value,
          policy: pSelect.value,
        },
      });
      loadQuotaCards();
    } catch (e) {
      alert("Failed to update policy: " + e.message);
    }
  }

  async function triggerManualSync() {
    try {
      await api.triggerSync({});
      alert("Real-time sync triggered across all connectors!");
      loadSyncStatus();
    } catch (e) {
      alert("Sync trigger failed: " + e.message);
    }
  }

  async function triggerHeadOfFolderScan() {
    const pathInput = document.getElementById("hof-path-input");
    const p = pathInput ? pathInput.value.trim() : "";
    if (!p) {
      alert("Please specify a directory path to scan.");
      return;
    }

    try {
      const resp = await api.scanHeadOfFolder({ folder_path: p, recursive: true });
      const data = resp.ok ? resp.data : resp;
      alert(`Head-of-Folder scan complete: indexed ${data.indexed_count || 0} items.`);
    } catch (e) {
      alert("Scan failed: " + e.message);
    }
  }

  async function loadAdminOverview() {
    const statusDot = document.getElementById("daemon-status-dot");
    const statusText = document.getElementById("daemon-status-text");

    try {
      const resp = await api.getStatus();
      const data = resp.ok ? resp.data : resp;
      if (statusDot && statusText) {
        if (data && data.daemon_running) {
          statusDot.className = "status-dot online";
          statusText.textContent = "Daemon Active";
        } else {
          statusDot.className = "status-dot offline";
          statusText.textContent = "In-Process Mode";
        }
      }
    } catch (e) {
      if (statusDot) statusDot.className = "status-dot error";
      if (statusText) statusText.textContent = "Offline";
    }
  }

  global.OldRAGSyncQuotaView = {
    init: initSyncQuotaView,
    loadData: loadSyncAndQuotaData,
  };
})(window);
