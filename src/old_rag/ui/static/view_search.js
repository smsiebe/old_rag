/* ==========================================================================
   OldRAG Workspace: Global Cross-Search & Discovery (Pillar 4)
   ========================================================================== */

(function (global) {
  "use strict";

  const api = global.OldRAGApi;

  let currentSourceFilter = null;
  let activeSearchQuery = "";

  function initSearchView() {
    const searchInput = document.getElementById("global-search-input") || document.getElementById("search-input");
    const searchBtn = document.getElementById("global-search-btn") || document.getElementById("search-btn");
    const filterPills = document.querySelectorAll(".source-filter-pill, .pill-btn[data-source]");

    if (searchInput) {
      searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          executeGlobalSearch();
        }
      });
    }

    if (searchBtn) {
      searchBtn.addEventListener("click", executeGlobalSearch);
    }

    filterPills.forEach((pill) => {
      pill.addEventListener("click", function () {
        filterPills.forEach((p) => p.classList.remove("active"));
        this.classList.add("active");
        const val = this.dataset.source;
        currentSourceFilter = (val === "ALL" || !val) ? null : val;
        executeGlobalSearch();
      });
    });
  }

  async function executeGlobalSearch() {
    const input = document.getElementById("global-search-input") || document.getElementById("search-input");
    const query = input ? input.value.trim() : "";
    if (!query) return;

    activeSearchQuery = query;
    const container = document.getElementById("search-results-list") || document.getElementById("results-list");
    if (container) {
      container.innerHTML = '<div class="loading-state">🔍 Searching across Mail, Chat, Calendar, and Files...</div>';
    }

    const payload = {
      query_text: query,
      top_k: 40,
    };
    if (currentSourceFilter) {
      payload.source_types = [currentSourceFilter];
    }

    try {
      const resp = await api.search(payload);
      const data = resp.ok ? resp.data : resp;
      const results = (data && data.results) || [];
      renderSearchResults(results);
    } catch (err) {
      if (container) {
        container.innerHTML = `<div class="error-state">Search failed: ${api.escapeHtml(err.message)}</div>`;
      }
    }
  }

  function renderSearchResults(results) {
    const container = document.getElementById("search-results-list") || document.getElementById("results-list");
    const countBadge = document.getElementById("search-results-count") || document.getElementById("results-count-text");
    if (countBadge) {
      countBadge.textContent = `${results.length} hits`;
    }

    if (!container) return;
    if (results.length === 0) {
      container.innerHTML = '<div class="empty-state">No matching documents or records found.</div>';
      return;
    }

    let html = "";
    results.forEach((item, idx) => {
      const tierClass = (item.relevance_tier || "LOW").toLowerCase();
      const confPct = Math.round((item.confidence_score || item.normalized_rrf || 0) * 100);
      const st = item.source_type || "UNKNOWN";
      const icon = st === "EMAIL" ? "📧" : st === "CHAT" ? "💬" : st === "CALENDAR" ? "📅" : "📄";

      html += `
        <article class="search-result-card tier-${tierClass}" data-doc-id="${api.escapeHtml(item.doc_id)}" data-index="${idx}">
          <div class="result-header">
            <span class="source-badge badge-${st.toLowerCase()}">${icon} ${api.escapeHtml(st)}</span>
            <span class="confidence-badge badge-${tierClass}">${confPct}% Match (${api.escapeHtml(item.relevance_tier || "LOW")})</span>
            ${item.exact_identifier_match ? '<span class="exact-match-badge">🎯 Exact ID</span>' : ''}
            <span class="result-date">${api.formatTimestamp(item.received_ts_utc)}</span>
          </div>
          <h3 class="result-title">${api.escapeHtml(item.subject || item.original_path || "Untitled Document")}</h3>
          <div class="result-meta">
            ${item.sender ? `<span class="meta-item"><strong>From:</strong> ${api.escapeHtml(item.sender)}</span>` : ""}
            ${item.folder ? `<span class="meta-item"><strong>Folder:</strong> ${api.escapeHtml(item.folder)}</span>` : ""}
          </div>
          <div class="result-snippet">${api.escapeHtml(item.content || "")}</div>
          <div class="result-actions">
            <button class="btn btn-sm btn-outline btn-open-doc" data-doc-id="${api.escapeHtml(item.doc_id)}" data-path="${api.escapeHtml(item.original_path || "")}">Open</button>
            <button class="btn btn-sm btn-outline btn-explain-t1" data-doc-id="${api.escapeHtml(item.doc_id)}" data-chunk-id="${api.escapeHtml(item.chunk_id)}">Why This Matched</button>
            <button class="btn btn-sm btn-outline btn-show-related" data-doc-id="${api.escapeHtml(item.doc_id)}" data-subject="${api.escapeHtml(item.subject || "")}">Related Context</button>
          </div>
        </article>
      `;
    });

    container.innerHTML = html;

    // Attach listeners
    container.querySelectorAll(".btn-open-doc").forEach((btn) => {
      btn.addEventListener("click", function () {
        const docId = this.dataset.docId;
        const origPath = this.dataset.path;
        api.openDocument({ doc_id: docId, original_path: origPath });
        api.recordAccess(docId);
      });
    });

    container.querySelectorAll(".btn-explain-t1").forEach((btn) => {
      btn.addEventListener("click", function () {
        const docId = this.dataset.docId;
        const chunkId = this.dataset.chunkId;
        showExplainModal(docId, chunkId, activeSearchQuery);
      });
    });

    container.querySelectorAll(".btn-show-related").forEach((btn) => {
      btn.addEventListener("click", function () {
        const docId = this.dataset.docId;
        const subj = this.dataset.subject;
        showRelatedDrawer(docId, subj);
      });
    });
  }

  async function showExplainModal(docId, chunkId, queryText) {
    const modal = document.getElementById("explain-modal") || document.getElementById("modal-explain");
    const body = document.getElementById("explain-modal-body") || document.getElementById("explain-tier1-content");
    if (!modal || !body) return;

    modal.style.display = "flex";
    modal.classList.add("open");
    body.innerHTML = '<div class="loading-state">Generating retrieval explainability evidence...</div>';

    try {
      const resp = await api.explainMatchTier1({
        query_text: queryText,
        doc_id: docId,
        chunk_id: chunkId,
      });
      const data = resp.ok ? resp.data : resp;
      if (!data) {
        body.innerHTML = '<div class="error-state">Failed to generate match explanation.</div>';
        return;
      }

      const bullets = (data.summary_bullets || []).map((b) => `<li>${api.escapeHtml(b)}</li>`).join("");
      const channels = (data.channels_matched || []).map((c) => `<span class="channel-pill">${api.escapeHtml(c)}</span>`).join(" ");

      body.innerHTML = `
        <div class="explain-content">
          <h4>Match Quality: <span class="confidence-badge badge-${(data.confidence_tier || "LOW").toLowerCase()}">${data.confidence_tier} (${Math.round((data.confidence_score || 0) * 100)}%)</span></h4>
          <p><strong>Matched Index Channels:</strong> ${channels || "None"}</p>
          <div class="explain-bullets">
            <h5>Retrieval Evidence:</h5>
            <ul>${bullets}</ul>
          </div>
          <div class="explain-tier2-box">
            <button id="btn-run-tier2" class="btn btn-sm btn-primary">Run Deep Tier 2 Analysis (AuraRouter)</button>
            <div id="tier2-output" class="tier2-output"></div>
          </div>
        </div>
      `;

      const t2Btn = document.getElementById("btn-run-tier2");
      if (t2Btn) {
        t2Btn.addEventListener("click", async function () {
          const out = document.getElementById("tier2-output");
          if (out) out.innerHTML = "<em>Running offline reasoning model...</em>";
          try {
            const t2Resp = await api.explainMatchTier2({
              query_text: queryText,
              doc_id: docId,
              chunk_id: chunkId,
            });
            const t2Data = t2Resp.ok ? t2Resp.data : t2Resp;
            if (out) {
              out.innerHTML = `
                <div class="t2-result">
                  <p><strong>Relevance Grade:</strong> ${api.escapeHtml(t2Data.relevance_grade || "N/A")}</p>
                  <p><strong>Rationale:</strong> ${api.escapeHtml(t2Data.rationale || "No rationale provided.")}</p>
                </div>
              `;
            }
          } catch (e) {
            if (out) out.innerHTML = `<span class="error">Tier 2 explanation unavailable: ${api.escapeHtml(e.message)}</span>`;
          }
        });
      }
    } catch (err) {
      body.innerHTML = `<div class="error-state">Error: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  async function showRelatedDrawer(docId, subject) {
    const drawer = document.getElementById("related-context-drawer") || document.getElementById("modal-cal-detail");
    const content = document.getElementById("related-drawer-content") || document.getElementById("cal-related-container");
    if (!drawer || !content) return;

    drawer.style.display = "flex";
    drawer.classList.add("open");
    content.innerHTML = '<div class="loading-state">Traversing related context graph...</div>';

    try {
      const resp = await api.getRelatedContext({ doc_id: docId, subject: subject, top_k: 6 });
      const data = resp.ok ? resp.data : resp;
      if (!data) {
        content.innerHTML = '<div class="empty-state">No related context items.</div>';
        return;
      }

      let html = "";
      const emails = data.related_emails || [];
      const chats = data.related_chats || [];
      const meetings = data.related_calendar || [];
      const files = data.related_files || [];

      if (emails.length > 0) {
        html += `<h5>📧 Related Emails (${emails.length})</h5><ul>`;
        emails.forEach((em) => {
          html += `<li><a href="#" class="rel-link" data-doc-id="${api.escapeHtml(em.doc_id)}">${api.escapeHtml(em.subject || "No subject")}</a></li>`;
        });
        html += `</ul>`;
      }

      if (chats.length > 0) {
        html += `<h5>💬 Related Chats (${chats.length})</h5><ul>`;
        chats.forEach((ch) => {
          html += `<li><strong>${api.escapeHtml(ch.sender || "User")}:</strong> ${api.escapeHtml(ch.text || ch.content || "")}</li>`;
        });
        html += `</ul>`;
      }

      if (meetings.length > 0) {
        html += `<h5>📅 Related Meetings (${meetings.length})</h5><ul>`;
        meetings.forEach((m) => {
          html += `<li>${api.escapeHtml(m.subject || "Meeting")} (${api.formatTimestamp(m.start_ts_utc)})</li>`;
        });
        html += `</ul>`;
      }

      if (files.length > 0) {
        html += `<h5>📄 Related Files (${files.length})</h5><ul>`;
        files.forEach((f) => {
          html += `<li>${api.escapeHtml(f.subject || f.original_path || "File")}</li>`;
        });
        html += `</ul>`;
      }

      if (!html) {
        html = '<div class="empty-state">No related items found for this document.</div>';
      }

      content.innerHTML = html;
    } catch (err) {
      content.innerHTML = `<div class="error-state">Failed to load related context: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  global.OldRAGSearchView = {
    init: initSearchView,
    executeSearch: executeGlobalSearch,
    showExplainModal: showExplainModal,
    showRelatedDrawer: showRelatedDrawer,
  };
})(window);
