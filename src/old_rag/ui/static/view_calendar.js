/* ==========================================================================
   OldRAG Workspace: Calendar & Agenda (Pillar 3)
   ========================================================================== */

(function (global) {
  "use strict";

  const api = global.OldRAGApi;

  function initCalendarView() {
    const newEventBtn = document.getElementById("btn-new-calendar-event");
    if (newEventBtn) {
      newEventBtn.addEventListener("click", showNewEventModal);
    }

    const exportBtn = document.getElementById("btn-export-ics");
    if (exportBtn) {
      exportBtn.addEventListener("click", exportCalendarIcs);
    }

    const importInput = document.getElementById("ics-file-input");
    if (importInput) {
      importInput.addEventListener("change", handleIcsFileImport);
    }

    loadCalendarEvents();
  }

  async function loadCalendarEvents() {
    const container = document.getElementById("calendar-events-list");
    if (!container) return;

    container.innerHTML = '<div class="loading-state">Loading calendar events...</div>';

    try {
      const resp = await api.listCalendarEvents();
      const data = resp.ok ? resp.data : resp;
      const events = (data && data.events) || [];
      renderAgenda(events);
    } catch (err) {
      container.innerHTML = `<div class="error-state">Failed to load calendar: ${api.escapeHtml(err.message)}</div>`;
    }
  }

  function renderAgenda(events) {
    const container = document.getElementById("calendar-events-list");
    if (!container) return;

    if (events.length === 0) {
      container.innerHTML = '<div class="empty-state">No upcoming meetings or events scheduled.</div>';
      return;
    }

    let html = "";
    events.forEach((evt) => {
      html += `
        <div class="calendar-event-card" data-event-id="${api.escapeHtml(evt.event_id)}">
          <div class="event-time-badge">
            <span class="event-date">${api.formatTimestamp(evt.start_ts_utc)}</span>
            <span class="event-duration">${evt.is_all_day ? "All Day" : ""}</span>
          </div>
          <div class="event-details">
            <h4 class="event-title">${api.escapeHtml(evt.subject || "Untitled Meeting")}</h4>
            <div class="event-meta">
              ${evt.location ? `<span class="event-location">📍 ${api.escapeHtml(evt.location)}</span>` : ""}
              ${evt.organizer ? `<span class="event-organizer">👤 ${api.escapeHtml(evt.organizer)}</span>` : ""}
            </div>
            ${evt.body_text ? `<div class="event-snippet">${api.escapeHtml(evt.body_text)}</div>` : ""}
            <div class="event-actions">
              <button class="btn btn-sm btn-outline btn-event-related" data-doc-id="${api.escapeHtml(evt.doc_id || evt.event_id)}" data-subject="${api.escapeHtml(evt.subject || "")}">
                🔗 Related Emails &amp; Chats
              </button>
            </div>
          </div>
        </div>
      `;
    });

    container.innerHTML = html;

    container.querySelectorAll(".btn-event-related").forEach((btn) => {
      btn.addEventListener("click", function () {
        const docId = this.dataset.docId;
        const subj = this.dataset.subject;
        if (global.OldRAGSearchView) {
          global.OldRAGSearchView.showRelatedDrawer(docId, subj);
        }
      });
    });
  }

  function showNewEventModal() {
    const modal = document.getElementById("calendar-event-modal");
    if (!modal) return;
    modal.classList.add("open");
  }

  async function exportCalendarIcs() {
    try {
      const resp = await api.exportIcs();
      const data = resp.ok ? resp.data : resp;
      const ics = (data && data.ics_content) || "";
      if (!ics) {
        alert("No calendar data to export.");
        return;
      }
      const blob = new Blob([ics], { type: "text/calendar;charset=utf-8;" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `oldrag_calendar_${Date.now()}.ics`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } catch (e) {
      alert("Failed to export ICS: " + e.message);
    }
  }

  function handleIcsFileImport(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = async function (evt) {
      const content = evt.target.result;
      try {
        const resp = await api.importIcs(content);
        const data = resp.ok ? resp.data : resp;
        alert(`Imported ${data.count || 0} events successfully!`);
        loadCalendarEvents();
      } catch (err) {
        alert("Import failed: " + err.message);
      }
    };
    reader.readAsText(file);
  }

  global.OldRAGCalendarView = {
    init: initCalendarView,
    loadEvents: loadCalendarEvents,
  };
})(window);
