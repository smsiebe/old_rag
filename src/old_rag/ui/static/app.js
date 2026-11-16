/**
 * app.js - Sovereign Mail Client, Calendar, Hybrid Search & Multi-Account Orchestrator
 */

document.addEventListener("DOMContentLoaded", function () {
    "use strict";

    // --- State ---
    var accountsList = [];
    var currentFolder = "ALL";
    var selectedAccountId = "ALL";
    var currentMessages = [];
    var selectedDocId = null;
    var calendarEvents = [];
    var calendarDate = new Date(); // Current viewing month
    var rawSearchResults = [];
    var currentSettings = null;
    var lastGlobalSearchQuery = "";
    var lastMailSearchQuery = "";
    var explainRegistry = {};
    var explainKeyCounter = 0;
    var explainKeyOrder = [];
    var activeExplainRequestId = 0;
    var lastExplainState = { payload: null, tier1: null };

    function registerExplainPayload(payload) {
        var key = "exp_" + (++explainKeyCounter);
        explainRegistry[key] = payload;
        explainKeyOrder.push(key);
        while (explainKeyOrder.length > 400) {
            var oldest = explainKeyOrder.shift();
            if (oldest) delete explainRegistry[oldest];
        }
        return key;
    }

    // --- Navigation & Admin Dropdown ---
    var navTabs = document.querySelectorAll(".nav-tab");
    var tabPanes = document.querySelectorAll(".tab-pane");
    var adminDropdownBtn = document.getElementById("admin-dropdown-btn");
    var adminDropdownMenu = document.getElementById("admin-dropdown-menu");

    function switchTab(targetTab) {
        navTabs.forEach(function (t) {
            t.classList.toggle("active", t.getAttribute("data-tab") === targetTab);
        });
        tabPanes.forEach(function (p) {
            p.classList.toggle("active", p.id === targetTab);
        });

        // Highlight Admin dropdown if an admin pane is active
        var isAdminTab = ["tab-overview", "tab-accounts", "tab-storage", "tab-metrics"].indexOf(targetTab) !== -1;
        if (adminDropdownBtn) {
            adminDropdownBtn.classList.toggle("active", isAdminTab);
        }

        document.querySelectorAll(".dropdown-item").forEach(function (di) {
            di.classList.toggle("active", di.getAttribute("data-tab") === targetTab);
        });

        if (targetTab === "tab-mail") loadMailbox();
        else if (targetTab === "tab-calendar") loadCalendar();
        else if (targetTab === "tab-overview") loadOverview();
        else if (targetTab === "tab-accounts") loadAccountsAndFolders();
        else if (targetTab === "tab-storage") loadStorage();
        else if (targetTab === "tab-metrics") loadMetrics();
        else if (targetTab === "tab-chat" && window.OldRAGChatView) window.OldRAGChatView.loadRooms();
        else if (targetTab === "tab-sync-quota" && window.OldRAGSyncQuotaView) window.OldRAGSyncQuotaView.loadData();
    }

    navTabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
            var targetTab = tab.getAttribute("data-tab");
            switchTab(targetTab);
        });
    });

    if (adminDropdownBtn && adminDropdownMenu) {
        adminDropdownBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            adminDropdownMenu.classList.toggle("show");
        });

        document.querySelectorAll(".dropdown-item").forEach(function (item) {
            item.addEventListener("click", function (e) {
                e.stopPropagation();
                var targetTab = item.getAttribute("data-tab");
                if (targetTab) switchTab(targetTab);
                adminDropdownMenu.classList.remove("show");
            });
        });

        document.addEventListener("click", function (e) {
            if (!adminDropdownBtn.contains(e.target) && !adminDropdownMenu.contains(e.target)) {
                adminDropdownMenu.classList.remove("show");
            }
        });
    }

    // Return to Mail buttons from admin screens
    document.querySelectorAll(".nav-return-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var target = btn.getAttribute("data-return") || "tab-mail";
            switchTab(target);
        });
    });

    // Daemon Status
    var daemonStatusDot = document.getElementById("daemon-status-dot");
    var daemonStatusText = document.getElementById("daemon-status-text");

    function updateDaemonStatus(isOnline) {
        if (isOnline) {
            daemonStatusDot.className = "status-dot online";
            daemonStatusText.textContent = "Daemon Standing";
        } else {
            daemonStatusDot.className = "status-dot offline";
            daemonStatusText.textContent = "In-Process Mode";
        }
    }

    // =========================================================================
    // SECTION 1: MAIL CLIENT
    // =========================================================================
    var mailAccountSelect = document.getElementById("mail-account-select");
    var mailComposeBtn = document.getElementById("mail-compose-btn");
    var mailFoldersNav = document.getElementById("mail-folders-nav");
    var customFoldersContainer = document.getElementById("custom-folders-container");
    var mailSearchFilter = document.getElementById("mail-search-filter");
    var mailRefreshBtn = document.getElementById("mail-refresh-btn");
    var mailItemsList = document.getElementById("mail-items-list");
    var mailReadingPane = document.getElementById("mail-reading-pane");

    // Compose Modal Elements
    var modalCompose = document.getElementById("modal-compose");
    var modalComposeClose = document.getElementById("modal-compose-close");
    var composeAccountSelect = document.getElementById("compose-account-select");
    var composeTo = document.getElementById("compose-to");
    var composeCc = document.getElementById("compose-cc");
    var composeBcc = document.getElementById("compose-bcc");
    var composeSubject = document.getElementById("compose-subject");
    var composeBody = document.getElementById("compose-body");
    var composeSendBtn = document.getElementById("compose-send-btn");
    var composeCancelBtn = document.getElementById("compose-cancel-btn");
    var composeStatus = document.getElementById("compose-status");

    function loadMailbox() {
        // Load accounts into dropdown
        window.SovereignBridge.listAccounts().then(function (res) {
            accountsList = res.accounts || [];
            updateAccountDropdowns();
            fetchMessages();
        }).catch(function () {
            fetchMessages();
        });
    }

    function updateAccountDropdowns() {
        var html = "<option value='ALL'>All Accounts (Unified)</option>";
        var composeHtml = "";

        accountsList.forEach(function (acc) {
            var label = acc.name + " (" + (acc.email_address || acc.account_type) + ")";
            html += "<option value='" + escapeHtml(acc.id) + "'>" + escapeHtml(label) + "</option>";
            composeHtml += "<option value='" + escapeHtml(acc.id) + "'>" + escapeHtml(label) + "</option>";
        });

        mailAccountSelect.innerHTML = html;
        mailAccountSelect.value = selectedAccountId;
        composeAccountSelect.innerHTML = composeHtml;
    }

    mailAccountSelect.addEventListener("change", function () {
        selectedAccountId = mailAccountSelect.value;
        fetchMessages();
    });

    // Folder selection
    mailFoldersNav.addEventListener("click", function (e) {
        var btn = e.target.closest(".folder-btn");
        if (!btn) return;
        document.querySelectorAll(".folder-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        currentFolder = btn.getAttribute("data-folder") || "ALL";
        fetchMessages();
    });

    var mailSearchClearBtn = document.getElementById("mail-search-clear-btn");
    mailRefreshBtn.addEventListener("click", function () {
        mailRefreshBtn.classList.add("rotating");
        var bridgeObj = window.OldRAGApi || window.SovereignBridge;
        if (bridgeObj && typeof bridgeObj.triggerSync === "function") {
            bridgeObj.triggerSync({ source_type: "EMAIL" }).then(function () {
                setTimeout(fetchMessages, 800);
            }).catch(function () {
                fetchMessages();
            }).finally(function () {
                setTimeout(function () { mailRefreshBtn.classList.remove("rotating"); }, 1500);
            });
        } else {
            fetchMessages();
            setTimeout(function () { mailRefreshBtn.classList.remove("rotating"); }, 500);
        }
    });
    mailSearchFilter.addEventListener("input", debounce(fetchMessages, 300));
    if (mailSearchClearBtn) {
        mailSearchClearBtn.addEventListener("click", function () {
            mailSearchFilter.value = "";
            mailSearchClearBtn.style.display = "none";
            fetchMessages();
        });
    }

    function fetchMessages() {
        var filterTerm = mailSearchFilter.value.trim();
        if (mailSearchClearBtn) {
            mailSearchClearBtn.style.display = filterTerm ? "block" : "none";
        }

        if (!filterTerm) {
            mailItemsList.innerHTML = "<div class='loading-placeholder'>Loading mail messages...</div>";
            window.SovereignBridge.listMessages({
                account_id: selectedAccountId !== "ALL" ? selectedAccountId : null,
                folder: currentFolder !== "ALL" ? currentFolder : null,
                limit: 60
            }).then(function (res) {
                currentMessages = res.messages || [];
                renderMessageList(currentMessages, false);
            }).catch(function (err) {
                mailItemsList.innerHTML = "<div class='alert-box alert-critical'>Failed to load mail: " + escapeHtml(err.message) + "</div>";
            });
        } else {
            var reqQuery = filterTerm;
            mailItemsList.innerHTML = "<div class='loading-placeholder'>⚡ Searching emails with hybrid AI retrieval...</div>";
            window.SovereignBridge.search({
                query_text: reqQuery,
                source_type: "EMAIL",
                folder: currentFolder !== "ALL" ? currentFolder : null,
                top_k: 50
            }).then(function (res) {
                lastMailSearchQuery = reqQuery;
                var results = res.results || [];
                renderMessageList(results, true);
            }).catch(function (err) {
                mailItemsList.innerHTML = "<div class='alert-box alert-critical'>Hybrid search error: " + escapeHtml(err.message) + "</div>";
            });
        }
    }

    function renderMessageList(messages, isSearchResult) {
        if (!messages || messages.length === 0) {
            var emptyMsg = isSearchResult ? "No matching emails found for this hybrid search." : "No messages in folder or matching active filters.";
            mailItemsList.innerHTML = "<div class='empty-state'><div class='empty-icon'>📭</div><h3>No emails found</h3><p>" + escapeHtml(emptyMsg) + "</p></div>";
            return;
        }

        var html = "";
        messages.forEach(function (msg) {
            var isSelected = msg.doc_id === selectedDocId ? "selected" : "";
            var senderDisplay = msg.sender ? escapeHtml(msg.sender) : "Unknown Sender";
            var subjectDisplay = msg.subject ? escapeHtml(msg.subject) : "(No Subject)";
            var dateDisplay = formatDate(msg.received_ts_utc);
            var folderDisplay = escapeHtml(msg.folder || "Inbox");

            html += "<div class='mail-item-row " + isSelected + "' data-id='" + escapeHtml(msg.doc_id) + "'>";
            html += "<div class='mail-item-header'>";
            html += "<span class='mail-item-sender'>" + senderDisplay + "</span>";
            html += "<span class='mail-item-date'>" + dateDisplay + "</span>";
            html += "</div>";
            html += "<div class='mail-item-subject'>" + subjectDisplay + "</div>";

            if (isSearchResult) {
                var confVal = Math.round((msg.confidence_score !== undefined && msg.confidence_score !== null ? msg.confidence_score : (msg.normalized_rrf || 0)) * 100);
                var tierClass = (msg.relevance_tier || "LOW").toLowerCase();
                var snippet = msg.content ? escapeHtml(msg.content.slice(0, 95)) + "..." : "";
                var expKey = registerExplainPayload({
                    item_kind: "search_hit",
                    query_text: lastMailSearchQuery,
                    doc_id: msg.doc_id,
                    chunk_id: msg.chunk_id,
                    source_context: { surface: "mail_search", folder: currentFolder },
                    score_snapshot: msg
                });

                html += "<div class='mail-item-snippet'>" + snippet + "</div>";
                html += "<div class='mail-item-badges'>";
                html += "<span class='badge-confidence " + tierClass + "'>" + confVal + "% " + escapeHtml(msg.relevance_tier || "MED") + "</span>";
                if (msg.exact_identifier_match) {
                    html += "<span class='badge-identifier'>Exact ID</span>";
                }
                html += "<span class='mail-folder-pill'>" + folderDisplay + "</span>";
                html += "<button type='button' class='btn btn-explain-xs' data-explain-key='" + expKey + "' title='Explain why this email matched and its confidence score'>✨ Why?</button>";
                html += "</div>";
            } else {
                html += "<div class='mail-item-snippet'>" + folderDisplay + "</div>";
            }

            html += "</div>";
        });

        mailItemsList.innerHTML = html;

        mailItemsList.querySelectorAll(".mail-item-row").forEach(function (row) {
            row.addEventListener("click", function (e) {
                if (e.target && e.target.closest("[data-explain-key]")) return;
                var docId = row.getAttribute("data-id");
                selectMessage(docId);
            });
        });
    }

    function selectMessage(docId) {
        selectedDocId = docId;
        document.querySelectorAll(".mail-item-row").forEach(function (r) {
            r.classList.toggle("selected", r.getAttribute("data-id") === docId);
        });

        mailReadingPane.innerHTML = "<div class='loading-placeholder'>Opening message...</div>";

        window.SovereignBridge.getMessageDetail(docId).then(function (res) {
            var msg = res.message;
            if (!msg) {
                mailReadingPane.innerHTML = "<div class='alert-box alert-warning'>Message body could not be opened.</div>";
                return;
            }
            renderReadingPane(msg);
        }).catch(function (err) {
            mailReadingPane.innerHTML = "<div class='alert-box alert-critical'>Error: " + escapeHtml(err.message) + "</div>";
        });
    }

    function renderReadingPane(msg) {
        var sender = msg.sender || "Unknown Sender";
        var initial = sender.charAt(0).toUpperCase();
        var dateFormatted = formatDateLong(msg.received_ts_utc);
        var subject = msg.subject || "(No Subject)";

        var html = "<div class='reading-view-container'>";
        html += "<div class='reading-header'>";
        html += "<div class='reading-title-area'>";
        html += "<h2>" + escapeHtml(subject) + "</h2>";
        html += "<div class='reading-meta'>";
        html += "<div class='avatar-circle'>" + initial + "</div>";
        html += "<div class='reading-sender-details'>";
        html += "<span class='reading-from'>" + escapeHtml(sender) + "</span>";
        html += "<span class='reading-to'>Folder: " + escapeHtml(msg.folder || "Inbox") + " | " + dateFormatted + "</span>";
        html += "</div>";
        html += "</div>";
        html += "</div>";

        // Action links
        html += "<div class='reading-actions-bar'>";
        html += "<button class='btn btn-secondary' id='read-reply-btn'>↩ Reply</button>";
        html += "<button class='btn btn-secondary' id='read-forward-btn'>➡ Forward</button>";
        if (msg.has_archived_msg) {
            html += "<button class='btn btn-secondary' onclick='openCachedMsg(\"" + escapeHtml(msg.doc_id) + "\")'>📄 Open Original (.msg)</button>";
        }
        html += "</div>";
        html += "</div>";

        // Attachments bar if any
        if (msg.attachments && msg.attachments.length > 0) {
            html += "<div class='attachments-section'>";
            html += "<strong>Attachments (" + msg.attachments.length + "):</strong>";
            msg.attachments.forEach(function (att) {
                html += "<span class='att-pill'>📎 " + escapeHtml(att.filename) + " (" + formatBytes(att.size_bytes) + ")</span>";
            });
            html += "</div>";
        }

        // Ambient Related Context Shelf
        html += "<div class='related-context-shelf' id='reading-related-shelf'>";
        html += "<div class='related-shelf-header' id='reading-related-header'>";
        html += "<div class='shelf-title-group'>";
        html += "<span class='shelf-sparkle'>✨</span>";
        html += "<span class='shelf-heading'>Related Context &amp; Attached Files</span>";
        html += "<span class='shelf-count-badge' id='reading-shelf-badge'>Searching archive...</span>";
        html += "</div>";
        html += "<span class='shelf-chevron' id='reading-shelf-chevron'>▾</span>";
        html += "</div>";
        html += "<div class='related-shelf-body' id='reading-shelf-body' style='display:none;'>";
        html += "<div class='loading-placeholder'>Retrieving ambient related files, emails, and meetings...</div>";
        html += "</div>";
        html += "</div>";

        // Email Body Rendering
        var bodyContent = "";
        if (msg.clean_body_text) {
            bodyContent = escapeHtml(msg.clean_body_text).replace(/\n/g, "<br>");
        } else {
            bodyContent = "<em>No body text content.</em>";
        }

        html += "<div class='reading-body-content'>" + bodyContent + "</div>";
        html += "</div>";

        mailReadingPane.innerHTML = html;

        // Wire ambient related context toggle
        var relatedHeader = document.getElementById("reading-related-header");
        var relatedBody = document.getElementById("reading-related-body");
        var relatedBadge = document.getElementById("reading-shelf-badge");

        if (relatedHeader && relatedBody) {
            relatedHeader.addEventListener("click", function () {
                var isOpen = relatedHeader.classList.toggle("open");
                relatedBody.style.display = isOpen ? "block" : "none";
            });
        }

        // Async ambient retrieval
        window.SovereignBridge.getRelatedContext(msg.doc_id, msg.subject, msg.clean_body_text).then(function (res) {
            renderRelatedShelfContent(res, relatedHeader, relatedBody, relatedBadge, {
                surface: "ambient_reading_pane",
                origin_kind: "email",
                origin_doc_id: msg.doc_id,
                origin_subject: msg.subject || ""
            });
        }).catch(function (err) {
            if (relatedBadge) {
                relatedBadge.textContent = "0 related";
                relatedBadge.className = "shelf-count-badge empty";
            }
            if (relatedBody) {
                relatedBody.innerHTML = "<div class='alert-box alert-warning'>Related search unavailable: " + escapeHtml(err.message) + "</div>";
            }
        });

        // Wire Reply & Forward buttons
        document.getElementById("read-reply-btn").addEventListener("click", function () {
            openComposeModal({
                to: msg.sender || "",
                subject: "Re: " + (msg.subject || ""),
                body: "\n\n--- Original Message ---\nFrom: " + (msg.sender || "") + "\nDate: " + dateFormatted + "\n\n" + (msg.clean_body_text || "")
            });
        });

        document.getElementById("read-forward-btn").addEventListener("click", function () {
            openComposeModal({
                to: "",
                subject: "Fwd: " + (msg.subject || ""),
                body: "\n\n--- Forwarded Message ---\nFrom: " + (msg.sender || "") + "\nDate: " + dateFormatted + "\nSubject: " + (msg.subject || "") + "\n\n" + (msg.clean_body_text || "")
            });
        });
    }

    // Compose Modal
    mailComposeBtn.addEventListener("click", function () { openComposeModal(); });
    modalComposeClose.addEventListener("click", closeComposeModal);
    composeCancelBtn.addEventListener("click", closeComposeModal);

    function openComposeModal(defaults) {
        defaults = defaults || {};
        composeTo.value = defaults.to || "";
        composeCc.value = defaults.cc || "";
        composeBcc.value = defaults.bcc || "";
        composeSubject.value = defaults.subject || "";
        composeBody.value = defaults.body || "";
        composeStatus.textContent = "";
        modalCompose.style.display = "flex";
        if (!defaults.to) composeTo.focus();
        else composeBody.focus();
    }

    function closeComposeModal() { modalCompose.style.display = "none"; }

    composeSendBtn.addEventListener("click", function () {
        var to = composeTo.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        var cc = composeCc.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        var bcc = composeBcc.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        var subject = composeSubject.value.trim();
        var body = composeBody.value;
        var accountId = composeAccountSelect.value;

        if (to.length === 0) {
            alert("Please provide at least one recipient in the 'To' field.");
            return;
        }

        composeSendBtn.disabled = true;
        composeStatus.textContent = "Sending email...";

        window.SovereignBridge.sendEmail({
            account_id: accountId,
            to: to,
            cc: cc,
            bcc: bcc,
            subject: subject,
            body_text: body,
            body_html: body.replace(/\n/g, "<br>")
        }).then(function (res) {
            composeSendBtn.disabled = false;
            if (res.success) {
                composeStatus.textContent = "Sent successfully!";
                setTimeout(function () {
                    closeComposeModal();
                    fetchMessages();
                }, 1000);
            } else {
                composeStatus.textContent = "Failed: " + (res.error || "Unknown error");
            }
        }).catch(function (err) {
            composeSendBtn.disabled = false;
            composeStatus.textContent = "Send error: " + err.message;
        });
    });

    // =========================================================================
    // SECTION 2: CALENDAR CLIENT
    // =========================================================================
    var calMonthTitle = document.getElementById("cal-month-title");
    var calCellsGrid = document.getElementById("cal-cells-grid");
    var calAgendaWrap = document.getElementById("cal-agenda-wrap");
    var calMonthGridWrap = document.getElementById("cal-month-grid-wrap");
    var calAgendaList = document.getElementById("cal-agenda-list");
    var calPrevBtn = document.getElementById("cal-prev-btn");
    var calTodayBtn = document.getElementById("cal-today-btn");
    var calNextBtn = document.getElementById("cal-next-btn");
    var calViewMonth = document.getElementById("cal-view-month");
    var calViewAgenda = document.getElementById("cal-view-agenda");
    var calNewEventBtn = document.getElementById("cal-new-event-btn");

    var modalEvent = document.getElementById("modal-event");
    var modalEventClose = document.getElementById("modal-event-close");
    var eventCancelBtn = document.getElementById("event-cancel-btn");
    var eventSaveBtn = document.getElementById("event-save-btn");
    var eventSubject = document.getElementById("event-subject");
    var eventStart = document.getElementById("event-start");
    var eventEnd = document.getElementById("event-end");
    var eventLocation = document.getElementById("event-location");
    var eventBody = document.getElementById("event-body");
    var eventStatus = document.getElementById("event-status");

    function loadCalendar() {
        window.SovereignBridge.listCalendarEvents().then(function (res) {
            calendarEvents = res.events || [];
            renderCalendar();
        }).catch(function (err) {
            console.error("Calendar load error:", err);
            renderCalendar();
        });
    }

    calPrevBtn.addEventListener("click", function () {
        calendarDate.setMonth(calendarDate.getMonth() - 1);
        renderCalendar();
    });

    calNextBtn.addEventListener("click", function () {
        calendarDate.setMonth(calendarDate.getMonth() + 1);
        renderCalendar();
    });

    calTodayBtn.addEventListener("click", function () {
        calendarDate = new Date();
        renderCalendar();
    });

    calViewMonth.addEventListener("click", function () {
        calViewMonth.classList.add("active");
        calViewAgenda.classList.remove("active");
        calMonthGridWrap.style.display = "block";
        calAgendaWrap.style.display = "none";
    });

    calViewAgenda.addEventListener("click", function () {
        calViewAgenda.classList.add("active");
        calViewMonth.classList.remove("active");
        calMonthGridWrap.style.display = "none";
        calAgendaWrap.style.display = "block";
    });

    function renderCalendar() {
        var year = calendarDate.getFullYear();
        var month = calendarDate.getMonth();
        var monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
        calMonthTitle.textContent = monthNames[month] + " " + year;

        // Month Grid calculations
        var firstDayIndex = new Date(year, month, 1).getDay();
        var daysInMonth = new Date(year, month + 1, 0).getDate();
        var today = new Date();

        var html = "";
        // Empty cells before start of month
        for (var i = 0; i < firstDayIndex; i++) {
            html += "<div class='cal-cell empty'></div>";
        }

        for (var day = 1; day <= daysInMonth; day++) {
            var isToday = (day === today.getDate() && month === today.getMonth() && year === today.getFullYear()) ? "today" : "";
            var cellDateStr = year + "-" + padZero(month + 1) + "-" + padZero(day);

            // Filter events on this day
            var dayEvents = calendarEvents.filter(function (e) {
                return (e.start_ts_utc || "").substring(0, 10) === cellDateStr;
            });

            html += "<div class='cal-cell " + isToday + "'>";
            html += "<span class='cal-day-num'>" + day + "</span>";
            dayEvents.forEach(function (e) {
                var color = e.calendar_color || "#339af0";
                var sys = e.source_system || "Outlook";
                html += "<div class='cal-event-pill' data-id='" + escapeHtml(e.event_id) + "' style='cursor:pointer; background:" + color + "33; border-left:3px solid " + color + "; color:#fff;' title='" + escapeHtml(e.subject + " (" + (e.location || "No location") + ") - " + sys) + "'>" + escapeHtml(e.subject) + "</div>";
            });
            html += "</div>";
        }

        calCellsGrid.innerHTML = html;

        calCellsGrid.querySelectorAll(".cal-event-pill").forEach(function (pill) {
            pill.addEventListener("click", function (ev) {
                ev.stopPropagation();
                var eid = pill.getAttribute("data-id");
                if (eid) showCalendarEventDetail(eid);
            });
        });

        // Render Agenda List
        var agendaHtml = "";
        if (calendarEvents.length === 0) {
            agendaHtml = "<div class='empty-state'><div class='empty-icon'>📅</div><h3>No calendar events</h3><p>Click 'New Event' or connect Outlook, Google, or Exchange Calendars to see meetings.</p></div>";
        } else {
            calendarEvents.forEach(function (e) {
                var color = e.calendar_color || "#339af0";
                var sys = e.source_system || "Outlook";
                var startD = new Date(e.start_ts_utc);
                var dateStr = isNaN(startD.getTime()) ? e.start_ts_utc : startD.toLocaleDateString() + " " + startD.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

                agendaHtml += "<div class='agenda-item' data-id='" + escapeHtml(e.event_id) + "' style='cursor:pointer; border-left:4px solid " + color + ";'>";
                agendaHtml += "<div>";
                agendaHtml += "<h4><span class='badge' style='background:" + color + "22; color:" + color + "; margin-right:6px; font-size:11px;'>" + escapeHtml(sys) + "</span>" + escapeHtml(e.subject) + "</h4>";
                agendaHtml += "<p style='color:var(--text-muted);font-size:12px;'>🕒 " + escapeHtml(dateStr);
                if (e.location) agendaHtml += " | 📍 " + escapeHtml(e.location);
                agendaHtml += "</p>";
                if (e.body_text) agendaHtml += "<p style='font-size:13px;margin-top:4px;'>" + escapeHtml(e.body_text) + "</p>";
                agendaHtml += "</div>";
                agendaHtml += "<span class='badge badge-info'>" + escapeHtml(e.status || "CONFIRMED") + "</span>";
                agendaHtml += "</div>";
            });
        }
        calAgendaList.innerHTML = agendaHtml;

        calAgendaList.querySelectorAll(".agenda-item").forEach(function (item) {
            item.addEventListener("click", function () {
                var eid = item.getAttribute("data-id");
                if (eid) showCalendarEventDetail(eid);
            });
        });
    }

    // =========================================================================
    // SECTION 2B: AMBIENT RELATED CONTEXT & CALENDAR EVENT DETAIL
    // =========================================================================
    var modalCalDetail = document.getElementById("modal-cal-detail");
    var modalCalDetailClose = document.getElementById("modal-cal-detail-close");
    var calDetailCloseBtn = document.getElementById("cal-detail-close-btn");
    var calDetailSubject = document.getElementById("cal-detail-subject");
    var calDetailTime = document.getElementById("cal-detail-time");
    var calDetailLoc = document.getElementById("cal-detail-loc");
    var calDetailBody = document.getElementById("cal-detail-body");
    var calRelatedContainer = document.getElementById("cal-related-container");
    var calRelatedCountBadge = document.getElementById("cal-related-count-badge");

    if (modalCalDetailClose) modalCalDetailClose.addEventListener("click", closeCalDetailModal);
    if (calDetailCloseBtn) calDetailCloseBtn.addEventListener("click", closeCalDetailModal);

    function closeCalDetailModal() {
        if (modalCalDetail) modalCalDetail.style.display = "none";
    }

    function showCalendarEventDetail(eventId) {
        var event = calendarEvents.find(function (e) { return e.event_id === eventId; });
        if (!event) return;

        if (calDetailSubject) calDetailSubject.textContent = event.subject || "Event";
        if (calDetailTime) calDetailTime.textContent = "🕒 " + formatDateLong(event.start_ts_utc) + (event.end_ts_utc ? " - " + formatDateLong(event.end_ts_utc) : "");
        if (calDetailLoc) calDetailLoc.textContent = event.location ? "📍 " + event.location : "📍 No location specified";
        if (calDetailBody) calDetailBody.textContent = event.body_text || "No notes or description provided.";
        if (calRelatedCountBadge) {
            calRelatedCountBadge.textContent = "Searching archive...";
            calRelatedCountBadge.className = "shelf-count-badge";
        }
        if (calRelatedContainer) {
            calRelatedContainer.innerHTML = "<div class='loading-placeholder'>Searching related documents and emails...</div>";
        }

        if (modalCalDetail) modalCalDetail.style.display = "flex";

        window.SovereignBridge.getRelatedContext(event.event_id, event.subject, event.body_text).then(function (res) {
            renderRelatedShelfContent(res, null, calRelatedContainer, calRelatedCountBadge, {
                surface: "ambient_calendar_detail",
                origin_kind: "calendar_event",
                origin_doc_id: event.event_id,
                origin_subject: event.subject || ""
            });
        }).catch(function (err) {
            if (calRelatedCountBadge) {
                calRelatedCountBadge.textContent = "0 related";
                calRelatedCountBadge.className = "shelf-count-badge empty";
            }
            if (calRelatedContainer) {
                calRelatedContainer.innerHTML = "<div class='alert-box alert-warning'>Related search error: " + escapeHtml(err.message) + "</div>";
            }
        });
    }

    function renderRelatedShelfContent(res, headerEl, bodyEl, badgeEl, originContext) {
        if (!bodyEl || !badgeEl) return;
        var files = res.related_files || [];
        var emails = res.related_emails || [];
        var events = res.related_calendar || [];
        var queryUsed = res.query_used || "";
        var originCtx = originContext || {};
        var total = files.length + emails.length + events.length;

        if (total === 0) {
            badgeEl.textContent = "0 related items";
            badgeEl.className = "shelf-count-badge empty";
            bodyEl.innerHTML = "<div class='empty-state' style='padding:12px;'><p class='form-hint'>No ambient related documents, emails, or calendar events found in archive for this item.</p></div>";
            return;
        }

        var parts = [];
        if (files.length > 0) parts.push(files.length + " file" + (files.length > 1 ? "s" : ""));
        if (emails.length > 0) parts.push(emails.length + " email" + (emails.length > 1 ? "s" : ""));
        if (events.length > 0) parts.push(events.length + " event" + (events.length > 1 ? "s" : ""));
        badgeEl.textContent = "✨ " + parts.join(", ");
        badgeEl.className = "shelf-count-badge";

        if (headerEl) headerEl.classList.add("open");
        bodyEl.style.display = "block";

        var html = "";
        if (queryUsed) {
            html += "<div class='related-query-hint'><span>Extracted ambient keywords:</span> <code>" + escapeHtml(queryUsed) + "</code></div>";
        }
        html += "<div class='related-columns-grid'>";

        // Column 1: Files
        if (files.length > 0) {
            html += "<div class='related-column'>";
            html += "<div class='related-column-title'><span>📁</span> Files &amp; Documents (" + files.length + ")</div>";
            html += "<div class='related-column-list'>";
            files.forEach(function (f) {
                var fname = f.original_path ? f.original_path.split(/[\\/]/).pop() : (f.doc_id || "Document");
                var confVal = Math.round((f.confidence_score !== undefined && f.confidence_score !== null ? f.confidence_score : (f.normalized_rrf || 0)) * 100);
                var tier = (f.relevance_tier || "LOW").toLowerCase();
                var pathStr = f.original_path || "";
                var safePath = escapeHtml(pathStr);
                var expKey = registerExplainPayload({
                    item_kind: "search_hit",
                    query_text: queryUsed,
                    doc_id: f.doc_id,
                    chunk_id: f.chunk_id,
                    source_context: Object.assign({ match_column: "related_file" }, originCtx),
                    score_snapshot: f
                });

                html += "<div class='related-item-card'>";
                html += "<div class='related-item-header'>";
                html += "<span class='related-item-title' title='" + safePath + "'>📄 " + escapeHtml(fname) + "</span>";
                html += "<span class='badge-confidence " + tier + "'>" + confVal + "%</span>";
                html += "</div>";
                if (pathStr) {
                    html += "<div class='related-item-sub' title='" + safePath + "'>" + safePath + "</div>";
                }
                if (f.content) {
                    html += "<div class='related-item-snippet'>" + escapeHtml(f.content.slice(0, 90)) + "...</div>";
                }
                html += "<div class='related-item-actions'>";
                html += "<button type='button' class='btn btn-explain-sm' data-explain-key='" + expKey + "' title='Explain why this file was surfaced'>✨ Why?</button>";
                html += "<button class='btn btn-secondary btn-open-sm' onclick='openDocument(\"" + escapeHtml(f.doc_id) + "\", \"" + escapeHtml(pathStr).replace(/\\/g, "\\\\") + "\")'>📂 Open File</button>";
                html += "</div>";
                html += "</div>";
            });
            html += "</div></div>";
        }

        // Column 2: Emails
        if (emails.length > 0) {
            html += "<div class='related-column'>";
            html += "<div class='related-column-title'><span>📧</span> Related Emails (" + emails.length + ")</div>";
            html += "<div class='related-column-list'>";
            emails.forEach(function (em) {
                var emSubj = em.subject ? escapeHtml(em.subject) : "(No Subject)";
                var emSender = em.sender ? escapeHtml(em.sender) : "Unknown Sender";
                var emDate = formatDate(em.received_ts_utc);
                var confVal = Math.round((em.confidence_score !== undefined && em.confidence_score !== null ? em.confidence_score : (em.normalized_rrf || 0)) * 100);
                var tier = (em.relevance_tier || "LOW").toLowerCase();
                var expKey = registerExplainPayload({
                    item_kind: "search_hit",
                    query_text: queryUsed,
                    doc_id: em.doc_id,
                    chunk_id: em.chunk_id,
                    source_context: Object.assign({ match_column: "related_email" }, originCtx),
                    score_snapshot: em
                });

                html += "<div class='related-item-card'>";
                html += "<div class='related-item-header'>";
                html += "<span class='related-item-title'>" + emSubj + "</span>";
                html += "<span class='badge-confidence " + tier + "'>" + confVal + "%</span>";
                html += "</div>";
                html += "<div class='related-item-sub'>" + emSender + " • " + emDate + "</div>";
                if (em.content) {
                    html += "<div class='related-item-snippet'>" + escapeHtml(em.content.slice(0, 90)) + "...</div>";
                }
                html += "<div class='related-item-actions'>";
                html += "<button type='button' class='btn btn-explain-sm' data-explain-key='" + expKey + "' title='Explain why this email was surfaced'>✨ Why?</button>";
                html += "<button class='btn btn-secondary btn-open-sm' onclick='selectMessage(\"" + escapeHtml(em.doc_id) + "\")'>✉️ Read Email</button>";
                html += "</div>";
                html += "</div>";
            });
            html += "</div></div>";
        }

        // Column 3: Calendar
        if (events.length > 0) {
            html += "<div class='related-column'>";
            html += "<div class='related-column-title'><span>📅</span> Related Events (" + events.length + ")</div>";
            html += "<div class='related-column-list'>";
            events.forEach(function (ev) {
                var evSubj = escapeHtml(ev.subject || "Event");
                var evStart = formatDateLong(ev.start_ts_utc);
                var evLoc = ev.location ? escapeHtml(ev.location) : "No location";
                var expKey = registerExplainPayload({
                    item_kind: "calendar_event",
                    query_text: queryUsed,
                    event_id: ev.event_id,
                    source_context: Object.assign({ match_column: "related_calendar" }, originCtx),
                    score_snapshot: ev
                });

                html += "<div class='related-item-card'>";
                html += "<div class='related-item-header'>";
                html += "<span class='related-item-title'>" + evSubj + "</span>";
                html += "<span class='badge badge-info'>Event</span>";
                html += "</div>";
                html += "<div class='related-item-sub'>🕒 " + evStart + "</div>";
                if (ev.location) {
                    html += "<div class='related-item-sub'>📍 " + evLoc + "</div>";
                }
                html += "<div class='related-item-actions'>";
                html += "<button type='button' class='btn btn-explain-sm' data-explain-key='" + expKey + "' title='Explain why this calendar event matched'>✨ Why?</button>";
                html += "<button class='btn btn-secondary btn-open-sm' onclick='openCalendarEventDetail(\"" + escapeHtml(ev.event_id) + "\")'>📅 View Event</button>";
                html += "</div>";
                html += "</div>";
            });
            html += "</div></div>";
        }

        html += "</div>";
        bodyEl.innerHTML = html;
    }

    // Global Action Helpers for Inline Buttons
    window.openCachedMsg = function (docId) {
        window.SovereignBridge.openCachedMsg(docId).catch(function (err) {
            alert("Could not open original .msg: " + err.message);
        });
    };

    window.openDocument = function (docId, originalPath) {
        window.SovereignBridge.openDocument(docId, originalPath).then(function (res) {
            if (!res.success) {
                alert("Could not open document: " + (res.error || "File inaccessible"));
            }
        }).catch(function (err) {
            alert("Failed to open document: " + err.message);
        });
    };

    window.selectMessage = function (docId) {
        closeCalDetailModal();
        switchTab("tab-mail");
        selectMessage(docId);
    };

    window.openCalendarEventDetail = function (eventId) {
        showCalendarEventDetail(eventId);
    };

    calNewEventBtn.addEventListener("click", function () {
        var now = new Date();
        var later = new Date(now.getTime() + 60 * 60 * 1000);
        eventStart.value = now.toISOString().slice(0, 16);
        eventEnd.value = later.toISOString().slice(0, 16);
        eventSubject.value = "";
        eventLocation.value = "";
        eventBody.value = "";
        eventStatus.textContent = "";
        modalEvent.style.display = "flex";
    });

    modalEventClose.addEventListener("click", function () { modalEvent.style.display = "none"; });
    eventCancelBtn.addEventListener("click", function () { modalEvent.style.display = "none"; });

    eventSaveBtn.addEventListener("click", function () {
        var subject = eventSubject.value.trim();
        var start = eventStart.value;
        var end = eventEnd.value;
        if (!subject || !start || !end) {
            alert("Subject and Start/End times are required.");
            return;
        }

        eventSaveBtn.disabled = true;
        eventStatus.textContent = "Saving event...";

        window.SovereignBridge.createCalendarEvent({
            subject: subject,
            start_ts_utc: new Date(start).toISOString(),
            end_ts_utc: new Date(end).toISOString(),
            location: eventLocation.value.trim(),
            body_text: eventBody.value.trim(),
            is_all_day: false
        }).then(function (res) {
            eventSaveBtn.disabled = false;
            modalEvent.style.display = "none";
            loadCalendar();
        }).catch(function (err) {
            eventSaveBtn.disabled = false;
            eventStatus.textContent = "Error: " + err.message;
        });
    });

    // =========================================================================
    // SECTION 3: ACCOUNTS & SHARED FOLDERS
    // =========================================================================
    var addAccountBtn = document.getElementById("add-account-btn");
    var accountsCardsContainer = document.getElementById("accounts-cards-container");
    var sharedFoldersContainer = document.getElementById("shared-folders-container");
    var fsAddPathInput = document.getElementById("fs-add-path-input");
    var fsAddAndScanBtn = document.getElementById("fs-add-and-scan-btn");

    var modalAccount = document.getElementById("modal-account");
    var modalAccountClose = document.getElementById("modal-account-close");
    var accName = document.getElementById("acc-name");
    var accType = document.getElementById("acc-type");
    var accEmail = document.getElementById("acc-email");
    var accImapHost = document.getElementById("acc-imap-host");
    var accImapPort = document.getElementById("acc-imap-port");
    var accSmtpHost = document.getElementById("acc-smtp-host");
    var accSmtpPort = document.getElementById("acc-smtp-port");
    var accPass = document.getElementById("acc-pass");
    var accRange = document.getElementById("acc-range");
    var accHelpText = document.getElementById("acc-help-text");
    var accTestBtn = document.getElementById("acc-test-btn");
    var accSaveBtn = document.getElementById("acc-save-btn");
    var accountTestStatus = document.getElementById("account-test-status");

    var chatAccountsList = [];
    var calendarAccountsList = [];
    var chatAccountsCardsContainer = document.getElementById("chat-accounts-cards-container");
    var calendarAccountsCardsContainer = document.getElementById("calendar-accounts-cards-container");

    var activePurgeTarget = { id: "", name: "", sourceType: "EMAIL" };
    var activeDeleteTarget = { id: "", name: "", connectorType: "email", purge: false };

    function loadAccountsAndFolders() {
        // 1. Load Email Accounts
        window.SovereignBridge.listAccounts().then(function (res) {
            accountsList = res.accounts || [];
            renderAccountsList();
        });

        // 2. Load Chat Accounts
        if (window.SovereignBridge.listChatAccounts) {
            window.SovereignBridge.listChatAccounts().then(function (res) {
                chatAccountsList = res.accounts || [];
                renderChatAccountsList();
            });
        }

        // 3. Load Calendar Accounts
        if (window.SovereignBridge.listCalendarAccounts) {
            window.SovereignBridge.listCalendarAccounts().then(function (res) {
                calendarAccountsList = res.accounts || [];
                renderCalendarAccountsList();
            });
        }

        // 4. Load Monitored Folders & Head-of-Folder Cache Status
        window.SovereignBridge.getSettings().then(function (data) {
            currentSettings = data.settings || {};
            var fsPaths = (currentSettings.connectors && currentSettings.connectors.filesystem && currentSettings.connectors.filesystem.paths) || [];
            renderSharedFoldersList(fsPaths);
        });
    }

    function renderAccountsList() {
        if (!accountsCardsContainer) return;
        if (accountsList.length === 0) {
            accountsCardsContainer.innerHTML = "<div class='empty-state'><div class='empty-icon'>✉️</div><h3>No Email Connections</h3><p>Click 'Add Email' to connect Outlook, Gmail, or IMAP mailboxes.</p></div>";
            return;
        }

        var html = "";
        accountsList.forEach(function (acc) {
            var typeLabel = acc.account_type === "outlook_mapi" ? "Classic Outlook (MAPI)" : (acc.account_type === "exchange_owa" ? "Exchange OWA" : "IMAP / SMTP");
            var statusBadge = acc.enabled ? "<span class='badge badge-success'>Active</span>" : "<span class='badge badge-warning'>Disabled</span>";

            html += "<div class='acc-card'>";
            html += "<div class='acc-card-top'>";
            html += "<div>";
            html += "<div class='acc-card-title'>📧 " + escapeHtml(acc.name) + "</div>";
            html += "<div class='acc-card-email'>" + escapeHtml(acc.email_address || acc.username || "Local MAPI Profile") + "</div>";
            html += "</div>";
            html += statusBadge;
            html += "</div>";

            html += "<div style='font-size:12px;color:var(--text-muted);'>";
            html += "<span>Type: <strong>" + typeLabel + "</strong></span> | ";
            html += "<span>Range: <strong>" + escapeHtml(acc.date_range || "1yr") + "</strong></span>";
            html += "</div>";

            html += "<div class='card-actions-row'>";
            html += "<button type='button' class='btn btn-sm btn-secondary' onclick='syncConnector(\"" + escapeHtml(acc.id) + "\", \"EMAIL\")' title='Trigger immediate synchronization'>🔄 Sync</button>";
            html += "<button type='button' class='btn btn-sm btn-secondary' onclick='testAccountLogin(\"" + escapeHtml(acc.id) + "\")'>Test</button>";
            html += "<button type='button' class='btn btn-sm btn-warning' onclick='openPurgeConfirmModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name) + "\", \"EMAIL\")' title='Purge indexed messages and vectors without deleting connection'>🧹 Purge Data</button>";
            html += "<button type='button' class='btn btn-sm btn-danger' onclick='openDeleteConnectorModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name) + "\", \"email\")' title='Delete connection with optional data purge'>🗑️ Delete</button>";
            html += "</div>";
            html += "</div>";
        });

        accountsCardsContainer.innerHTML = html;
    }

    function renderChatAccountsList() {
        if (!chatAccountsCardsContainer) return;
        if (chatAccountsList.length === 0) {
            chatAccountsCardsContainer.innerHTML = "<div class='empty-state'><div class='empty-icon'>💬</div><h3>No Chat Connections</h3><p>Click 'Add Chat' to connect Rocket.Chat, Google Chat, or Slack.</p></div>";
            return;
        }

        var html = "";
        chatAccountsList.forEach(function (acc) {
            var adapter = acc.adapter_type || "chat";
            var icon = adapter === "rocketchat" ? "🚀" : (adapter === "google_chat" ? "🌐" : (adapter === "teams" ? "👥" : "💬"));
            var statusBadge = acc.enabled !== false ? "<span class='badge badge-success'>Active</span>" : "<span class='badge badge-warning'>Disabled</span>";

            html += "<div class='acc-card'>";
            html += "<div class='acc-card-top'>";
            html += "<div>";
            html += "<div class='acc-card-title'>" + icon + " " + escapeHtml(acc.name || acc.display_name || adapter) + "</div>";
            html += "<div class='acc-card-email'>" + escapeHtml(acc.server_url || acc.username || "Configured Adapter") + "</div>";
            html += "</div>";
            html += statusBadge;
            html += "</div>";

            html += "<div style='font-size:12px;color:var(--text-muted);'>";
            html += "<span>Adapter: <strong>" + escapeHtml(adapter.toUpperCase()) + "</strong></span> | ";
            html += "<span>Lookback: <strong>" + (acc.lookback_days || 30) + " days</strong></span>";
            html += "</div>";

            html += "<div class='card-actions-row'>";
            html += "<button type='button' class='btn btn-sm btn-secondary' onclick='syncConnector(\"" + escapeHtml(acc.id) + "\", \"CHAT\")' title='Trigger immediate synchronization'>🔄 Sync</button>";
            html += "<button type='button' class='btn btn-sm btn-warning' onclick='openPurgeConfirmModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name || acc.display_name || adapter) + "\", \"CHAT\")' title='Purge indexed messages and vectors without deleting connection'>🧹 Purge Data</button>";
            html += "<button type='button' class='btn btn-sm btn-danger' onclick='openDeleteConnectorModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name || acc.display_name || adapter) + "\", \"chat\")' title='Delete connection with optional data purge'>🗑️ Delete</button>";
            html += "</div>";
            html += "</div>";
        });

        chatAccountsCardsContainer.innerHTML = html;
    }

    function renderCalendarAccountsList() {
        if (!calendarAccountsCardsContainer) return;
        if (calendarAccountsList.length === 0) {
            calendarAccountsCardsContainer.innerHTML = "<div class='empty-state'><div class='empty-icon'>📅</div><h3>No Calendar Connections</h3><p>Click 'Add Calendar' to connect Classic Outlook, Google Calendar, Exchange, or Confluence.</p></div>";
            return;
        }

        var html = "";
        calendarAccountsList.forEach(function (acc) {
            var color = acc.color || "#2563eb";
            var typeLabel = acc.connector_type === "outlook_mapi" ? "Classic Outlook (MAPI)" : (acc.connector_type === "google_calendar" ? "Google Calendar" : (acc.connector_type === "confluence" ? "Confluence Team Calendar" : (acc.connector_type === "exchange" ? "Microsoft Exchange" : "CalDAV / ICS")));
            var statusBadge = acc.enabled !== false ? "<span class='badge badge-success'>Active</span>" : "<span class='badge badge-warning'>Disabled</span>";

            html += "<div class='acc-card' style='border-left: 4px solid " + color + ";'>";
            html += "<div class='acc-card-top'>";
            html += "<div>";
            html += "<div class='acc-card-title'><span class='color-indicator-swatch' style='background:" + color + ";'></span>" + escapeHtml(acc.name) + "</div>";
            html += "<div class='acc-card-email'>" + escapeHtml(acc.url || (acc.connector_type === "outlook_mapi" ? "Local MAPI Store" : "Configured Feed")) + "</div>";
            html += "</div>";
            html += statusBadge;
            html += "</div>";

            html += "<div style='font-size:12px;color:var(--text-muted);'>";
            html += "<span>Type: <strong>" + escapeHtml(typeLabel) + "</strong></span>";
            html += "</div>";

            html += "<div class='card-actions-row'>";
            html += "<button type='button' class='btn btn-sm btn-secondary' onclick='syncConnector(\"" + escapeHtml(acc.id) + "\", \"CALENDAR\")' title='Trigger immediate synchronization'>🔄 Sync</button>";
            html += "<button type='button' class='btn btn-sm btn-secondary' onclick='popoutCalendarAccount(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name) + "\")' title='Pop out this calendar into a dashboard window'>⧉ Pop Out</button>";
            html += "<button type='button' class='btn btn-sm btn-warning' onclick='openPurgeConfirmModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name) + "\", \"CALENDAR\")' title='Purge indexed events and vectors without deleting connection'>🧹 Purge Data</button>";
            html += "<button type='button' class='btn btn-sm btn-danger' onclick='openDeleteConnectorModal(\"" + escapeHtml(acc.id) + "\", \"" + escapeHtml(acc.name) + "\", \"calendar\")' title='Delete connection with optional data purge'>🗑️ Delete</button>";
            html += "</div>";
            html += "</div>";
        });

        calendarAccountsCardsContainer.innerHTML = html;
    }

    window.syncConnector = function (accId, sourceType) {
        if (!window.SovereignBridge) return;
        window.SovereignBridge.triggerSync({ account_id: accId, source_type: sourceType }).then(function () {
            alert("✔ Synchronization triggered for " + sourceType.toLowerCase() + " connector " + accId);
            if (sourceType === "EMAIL") loadMailbox();
            if (sourceType === "CALENDAR") loadCalendarEvents();
            if (sourceType === "CHAT" && global.OldRAGChatView) global.OldRAGChatView.loadRooms();
        }).catch(function (err) {
            alert("Sync error: " + err.message);
        });
    };

    window.openPurgeConfirmModal = function (accId, name, sourceType) {
        activePurgeTarget = { id: accId, name: name, sourceType: sourceType };
        var nameEl = document.getElementById("purge-target-name");
        if (nameEl) nameEl.textContent = name || accId;
        var modal = document.getElementById("modal-purge-confirm");
        if (modal) modal.style.display = "flex";
    };

    window.openDeleteConnectorModal = function (accId, name, connectorType) {
        activeDeleteTarget = { id: accId, name: name, connectorType: connectorType, purge: false };
        var nameEl = document.getElementById("delete-connector-name");
        if (nameEl) nameEl.textContent = name || accId;

        // Reset choice cards selection
        var choiceOnly = document.getElementById("choice-delete-only");
        var choicePurge = document.getElementById("choice-delete-purge");
        if (choiceOnly && choicePurge) {
            choiceOnly.classList.add("selected");
            choicePurge.classList.remove("selected");
        }

        var modal = document.getElementById("modal-delete-connector");
        if (modal) modal.style.display = "flex";
    };

    window.popoutCalendarAccount = function (accId, accName) {
        if (global.OldRAGWindowManager) {
            global.OldRAGWindowManager.openWindow({
                id: "cal_" + accId,
                type: "calendar",
                target_id: accId,
                title: "Calendar: " + (accName || "View"),
                subBadge: accName,
            });
        }
    };

    // Purge Modal Listeners
    var modalPurgeClose = document.getElementById("modal-purge-close");
    var btnPurgeCancel = document.getElementById("btn-purge-cancel");
    var btnPurgeConfirmAction = document.getElementById("btn-purge-confirm-action");

    if (modalPurgeClose) modalPurgeClose.addEventListener("click", function () {
        var m = document.getElementById("modal-purge-confirm");
        if (m) m.style.display = "none";
    });
    if (btnPurgeCancel) btnPurgeCancel.addEventListener("click", function () {
        var m = document.getElementById("modal-purge-confirm");
        if (m) m.style.display = "none";
    });
    if (btnPurgeConfirmAction) {
        btnPurgeConfirmAction.addEventListener("click", function () {
            btnPurgeConfirmAction.disabled = true;
            btnPurgeConfirmAction.textContent = "Purging...";
            window.SovereignBridge.purgeAccountData({
                account_id: activePurgeTarget.id,
                source_type: activePurgeTarget.sourceType
            }).then(function (res) {
                btnPurgeConfirmAction.disabled = false;
                btnPurgeConfirmAction.textContent = "🧹 Purge Data Now";
                var m = document.getElementById("modal-purge-confirm");
                if (m) m.style.display = "none";
                alert("✔ Purged " + (res.deleted_docs_count || res.deleted_docs || 0) + " documents and indexed chunks for " + activePurgeTarget.name);
                loadAccountsAndFolders();
                loadMailbox();
                loadCalendarEvents();
            }).catch(function (err) {
                btnPurgeConfirmAction.disabled = false;
                btnPurgeConfirmAction.textContent = "🧹 Purge Data Now";
                alert("Purge error: " + err.message);
            });
        });
    }

    // Delete Connector Modal Listeners
    var modalDeleteClose = document.getElementById("modal-delete-close");
    var btnDeleteCancel = document.getElementById("btn-delete-cancel");
    var btnDeleteConfirmAction = document.getElementById("btn-delete-confirm-action");
    var choiceDeleteOnly = document.getElementById("choice-delete-only");
    var choiceDeletePurge = document.getElementById("choice-delete-purge");

    if (choiceDeleteOnly) {
        choiceDeleteOnly.addEventListener("click", function () {
            activeDeleteTarget.purge = false;
            choiceDeleteOnly.classList.add("selected");
            if (choiceDeletePurge) choiceDeletePurge.classList.remove("selected");
        });
    }
    if (choiceDeletePurge) {
        choiceDeletePurge.addEventListener("click", function () {
            activeDeleteTarget.purge = true;
            choiceDeletePurge.classList.add("selected");
            if (choiceDeleteOnly) choiceDeleteOnly.classList.remove("selected");
        });
    }
    if (modalDeleteClose) modalDeleteClose.addEventListener("click", function () {
        var m = document.getElementById("modal-delete-connector");
        if (m) m.style.display = "none";
    });
    if (btnDeleteCancel) btnDeleteCancel.addEventListener("click", function () {
        var m = document.getElementById("modal-delete-connector");
        if (m) m.style.display = "none";
    });
    if (btnDeleteConfirmAction) {
        btnDeleteConfirmAction.addEventListener("click", function () {
            var actionPromise = null;
            var accId = activeDeleteTarget.id;
            var purge = Boolean(activeDeleteTarget.purge);

            if (activeDeleteTarget.connectorType === "email") {
                actionPromise = window.SovereignBridge.deleteAccount({ account_id: accId, purge_data: purge });
            } else if (activeDeleteTarget.connectorType === "chat") {
                actionPromise = window.SovereignBridge.deleteChatAccount({ account_id: accId, purge_data: purge });
            } else if (activeDeleteTarget.connectorType === "calendar") {
                actionPromise = window.SovereignBridge.deleteCalendarAccount({ account_id: accId, purge_data: purge });
            }

            if (actionPromise) {
                btnDeleteConfirmAction.disabled = true;
                actionPromise.then(function () {
                    btnDeleteConfirmAction.disabled = false;
                    var m = document.getElementById("modal-delete-connector");
                    if (m) m.style.display = "none";
                    loadAccountsAndFolders();
                    loadMailbox();
                    loadCalendarEvents();
                    if (global.OldRAGChatView) global.OldRAGChatView.loadRooms();
                }).catch(function (err) {
                    btnDeleteConfirmAction.disabled = false;
                    alert("Delete error: " + err.message);
                });
            }
        });
    }

    // Admin Add Chat Button
    var adminAddChatBtn = document.getElementById("admin-add-chat-btn");
    if (adminAddChatBtn) {
        adminAddChatBtn.addEventListener("click", function () {
            var m = document.getElementById("chat-account-modal");
            if (m) {
                m.style.display = "flex";
                m.classList.add("open");
            }
        });
    }

    var btnSaveChatAccount = document.getElementById("btn-save-chat-account");
    if (btnSaveChatAccount) {
        btnSaveChatAccount.addEventListener("click", function () {
            var adapter = document.getElementById("chat-adapter-select") ? document.getElementById("chat-adapter-select").value : "rocketchat";
            var name = document.getElementById("chat-acc-name") ? document.getElementById("chat-acc-name").value.trim() : "";
            var url = document.getElementById("chat-acc-url") ? document.getElementById("chat-acc-url").value.trim() : "";
            var user = document.getElementById("chat-acc-user") ? document.getElementById("chat-acc-user").value.trim() : "";
            var token = document.getElementById("chat-acc-token") ? document.getElementById("chat-acc-token").value.trim() : "";
            var lookback = document.getElementById("chat-acc-lookback") ? parseInt(document.getElementById("chat-acc-lookback").value, 10) : 30;

            var acc = {
                adapter_type: adapter,
                name: name || (adapter.toUpperCase() + " Connection"),
                server_url: url,
                username: user,
                credentials: { auth_token: token, password: token },
                auth_token: token,
                lookback_days: lookback || 30,
                enabled: true
            };
            btnSaveChatAccount.disabled = true;
            window.SovereignBridge.saveChatAccount(acc).then(function () {
                btnSaveChatAccount.disabled = false;
                var m = document.getElementById("chat-account-modal");
                if (m) {
                    m.style.display = "none";
                    m.classList.remove("open");
                }
                loadAccountsAndFolders();
                if (global.OldRAGChatView) global.OldRAGChatView.loadRooms();
            }).catch(function (err) {
                btnSaveChatAccount.disabled = false;
                alert("Save chat account failed: " + err.message);
            });
        });
    }

    // Admin Add Calendar Button & Modal Handling
    var adminAddCalBtn = document.getElementById("admin-add-cal-btn");
    var modalCalAccount = document.getElementById("modal-cal-account");
    var modalCalClose = document.getElementById("modal-cal-close");
    var calAccCancelBtn = document.getElementById("cal-acc-cancel-btn");
    var calAccSaveBtn = document.getElementById("cal-acc-save-btn");
    var calAccType = document.getElementById("cal-acc-type");
    var calAccName = document.getElementById("cal-acc-name");
    var calAccUrl = document.getElementById("cal-acc-url");
    var calAccUser = document.getElementById("cal-acc-user");
    var calAccPass = document.getElementById("cal-acc-pass");
    var calUrlRow = document.getElementById("cal-url-row");
    var calCredsRow = document.getElementById("cal-creds-row");
    var calSelectedColor = "#2563eb";

    if (adminAddCalBtn && modalCalAccount) {
        adminAddCalBtn.addEventListener("click", function () {
            calAccName.value = "";
            calAccUrl.value = "";
            calAccUser.value = "";
            calAccPass.value = "";
            calSelectedColor = "#2563eb";
            modalCalAccount.style.display = "flex";
        });
    }
    if (modalCalClose && modalCalAccount) {
        modalCalClose.addEventListener("click", function () { modalCalAccount.style.display = "none"; });
    }
    if (calAccCancelBtn && modalCalAccount) {
        calAccCancelBtn.addEventListener("click", function () { modalCalAccount.style.display = "none"; });
    }

    // Swatches selection
    document.querySelectorAll(".color-swatch-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.querySelectorAll(".color-swatch-btn").forEach(function (b) { b.classList.remove("active"); });
            btn.classList.add("active");
            calSelectedColor = btn.getAttribute("data-color");
        });
    });
    var customColorPicker = document.getElementById("cal-custom-color");
    if (customColorPicker) {
        customColorPicker.addEventListener("input", function () {
            calSelectedColor = customColorPicker.value;
            document.querySelectorAll(".color-swatch-btn").forEach(function (b) { b.classList.remove("active"); });
        });
    }

    if (calAccType) {
        calAccType.addEventListener("change", function () {
            var val = calAccType.value;
            if (val === "outlook_mapi") {
                if (calUrlRow) calUrlRow.style.display = "none";
                if (calCredsRow) calCredsRow.style.display = "none";
            } else if (val === "google_calendar" || val === "confluence") {
                if (calUrlRow) calUrlRow.style.display = "block";
                if (calCredsRow) calCredsRow.style.display = "none";
            } else {
                if (calUrlRow) calUrlRow.style.display = "block";
                if (calCredsRow) calCredsRow.style.display = "flex";
            }
        });
    }

    if (calAccSaveBtn) {
        calAccSaveBtn.addEventListener("click", function () {
            var acc = {
                name: calAccName.value.trim() || "Calendar Connection",
                connector_type: calAccType.value,
                color: calSelectedColor,
                url: calAccUrl.value.trim(),
                username: calAccUser.value.trim(),
                password: calAccPass.value.trim(),
                enabled: true
            };
            calAccSaveBtn.disabled = true;
            window.SovereignBridge.saveCalendarAccount(acc).then(function () {
                calAccSaveBtn.disabled = false;
                if (modalCalAccount) modalCalAccount.style.display = "none";
                loadAccountsAndFolders();
                loadCalendarEvents();
            }).catch(function (err) {
                calAccSaveBtn.disabled = false;
                alert("Save calendar failed: " + err.message);
            });
        });
    }

    // Popout Component Triggers
    var calPopoutBtn = document.getElementById("cal-popout-btn");
    if (calPopoutBtn) {
        calPopoutBtn.addEventListener("click", function () {
            if (global.OldRAGWindowManager) {
                global.OldRAGWindowManager.openWindow({
                    id: "cal_unified",
                    type: "calendar",
                    target_id: "unified",
                    title: "Calendar: Unified View",
                });
            }
        });
    }

    var searchPopoutBtn = document.getElementById("search-popout-btn");
    if (searchPopoutBtn) {
        searchPopoutBtn.addEventListener("click", function () {
            if (global.OldRAGWindowManager) {
                global.OldRAGWindowManager.openWindow({
                    id: "search_quick",
                    type: "search",
                    target_id: "quick_search",
                    title: "Global Search",
                });
            }
        });
    }

    var navBtnSaveLayout = document.getElementById("nav-btn-save-layout");
    if (navBtnSaveLayout) {
        navBtnSaveLayout.addEventListener("click", function () {
            if (global.OldRAGWindowManager) {
                global.OldRAGWindowManager.saveLayout();
                var origText = navBtnSaveLayout.textContent;
                navBtnSaveLayout.textContent = "✔ Saved!";
                setTimeout(function () { navBtnSaveLayout.textContent = origText; }, 1500);
            }
        });
    }

    function renderSharedFoldersList(paths) {
        if (paths.length === 0) {
            sharedFoldersContainer.innerHTML = "<p style='color:var(--text-muted);font-size:13px;'>No shared filesystem folders configured yet.</p>";
            return;
        }

        var html = "";
        paths.forEach(function (p) {
            html += "<div class='shared-folder-item'>";
            html += "<div class='shared-folder-meta'>";
            html += "<span class='shared-folder-path'>📁 " + escapeHtml(p) + "</span>";
            html += "<span class='shared-folder-cache'>Index: " + escapeHtml(p) + "\\.oldrag\\index.db (Cooperative Locking Active)</span>";
            html += "</div>";
            html += "<button class='btn btn-secondary' onclick='scanHeadOfFolderNow(\"" + escapeHtml(p) + "\")'>🔄 Re-Scan Head Index</button>";
            html += "</div>";
        });
        sharedFoldersContainer.innerHTML = html;
    }

    window.testAccountLogin = function (accId) {
        var acc = accountsList.find(function (a) { return a.id === accId; });
        if (!acc) return;
        if (acc.account_type === "outlook_mapi") {
            window.SovereignBridge.getStatus().then(function (s) {
                alert(s.classic_outlook_active ? "✔ Classic Outlook COM is running and accessible!" : "✖ Classic Outlook is closed or inaccessible.");
            });
        } else {
            window.SovereignBridge.testImapConnection({
                host: acc.imap_host,
                port: acc.imap_port,
                ssl: acc.imap_ssl,
                username: acc.username,
                password: acc.password
            }).then(function (res) {
                if (res.success) alert("✔ IMAP connection verified! Discovered " + (res.folders ? res.folders.length : 0) + " mailbox folders.");
                else alert("✖ Connection failed: " + res.error);
            });
        }
    };

    window.deleteAccountById = function (accId) {
        if (!confirm("Are you sure you want to remove this email connection?")) return;
        window.SovereignBridge.deleteAccount(accId).then(function () {
            loadAccountsAndFolders();
            loadMailbox();
        });
    };

    window.scanHeadOfFolderNow = function (folderPath) {
        window.SovereignBridge.scanHeadOfFolder(folderPath, true).then(function (res) {
            alert("✔ Head-of-Folder Index updated at " + folderPath + "\\.oldrag\\index.db! Indexed " + res.indexed_count + " new/modified files.");
        }).catch(function (err) {
            alert("Folder scan error: " + err.message);
        });
    };

    fsAddAndScanBtn.addEventListener("click", function () {
        var p = fsAddPathInput.value.trim();
        if (!p) return;
        if (!currentSettings) currentSettings = { connectors: {} };
        if (!currentSettings.connectors.filesystem) currentSettings.connectors.filesystem = { paths: [] };
        currentSettings.connectors.filesystem.paths.push(p);

        window.SovereignBridge.saveSettings(currentSettings).then(function () {
            fsAddPathInput.value = "";
            window.scanHeadOfFolderNow(p);
            loadAccountsAndFolders();
        });
    });

    addAccountBtn.addEventListener("click", function () {
        accName.value = "";
        accEmail.value = "";
        accPass.value = "";
        accountTestStatus.textContent = "";
        modalAccount.style.display = "flex";
    });

    modalAccountClose.addEventListener("click", function () { modalAccount.style.display = "none"; });

    // Presets in Account Modal
    document.querySelectorAll(".preset-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.querySelectorAll(".preset-btn").forEach(function (b) { b.classList.remove("active"); });
            btn.classList.add("active");
            var key = btn.getAttribute("data-preset");

            if (key === "gmail") {
                accType.value = "imap_smtp";
                accImapHost.value = "imap.gmail.com";
                accImapPort.value = 993;
                accSmtpHost.value = "smtp.gmail.com";
                accSmtpPort.value = 587;
                accHelpText.textContent = "Requires a 16-character Google App Password (myaccount.google.com/apppasswords).";
            } else if (key === "yahoo") {
                accType.value = "imap_smtp";
                accImapHost.value = "imap.mail.yahoo.com";
                accImapPort.value = 993;
                accSmtpHost.value = "smtp.mail.yahoo.com";
                accSmtpPort.value = 587;
                accHelpText.textContent = "Requires a Yahoo App Password from Account Security.";
            } else if (key === "fastmail") {
                accType.value = "imap_smtp";
                accImapHost.value = "imap.fastmail.com";
                accImapPort.value = 993;
                accSmtpHost.value = "smtp.fastmail.com";
                accSmtpPort.value = 587;
                accHelpText.textContent = "Requires a Fastmail App Password.";
            } else if (key === "outlook") {
                accType.value = "outlook_mapi";
                accName.value = "Classic Outlook (MAPI)";
                accEmail.value = "Default Outlook Profile";
                accHelpText.textContent = "Direct local STA integration with running Classic Outlook profile.";
            }
        });
    });

    accSaveBtn.addEventListener("click", function () {
        var newAcc = {
            name: accName.value.trim() || "Email Connection",
            account_type: accType.value,
            email_address: accEmail.value.trim(),
            imap_host: accImapHost.value.trim(),
            imap_port: parseInt(accImapPort.value, 10) || 993,
            imap_ssl: true,
            smtp_host: accSmtpHost.value.trim(),
            smtp_port: parseInt(accSmtpPort.value, 10) || 587,
            smtp_ssl: false,
            smtp_starttls: true,
            username: accEmail.value.trim(),
            password: accPass.value.trim(),
            date_range: accRange.value,
            enabled: true
        };

        window.SovereignBridge.saveAccount(newAcc).then(function () {
            modalAccount.style.display = "none";
            loadAccountsAndFolders();
            loadMailbox();
        }).catch(function (err) {
            alert("Save failed: " + err.message);
        });
    });

    // =========================================================================
    // SECTION 4: HYBRID SEARCH
    // =========================================================================
    var searchInput = document.getElementById("search-input");
    var searchBtn = document.getElementById("search-btn");
    var searchClearBtn = document.getElementById("search-clear-btn");
    var resultsList = document.getElementById("results-list");
    var resultsCountText = document.getElementById("results-count-text");
    var sortSelect = document.getElementById("sort-select");
    var sourceFilter = document.getElementById("source-filter");
    var confidenceSlider = document.getElementById("confidence-slider");
    var confidenceVal = document.getElementById("confidence-val");
    var selectedTier = "ALL";

    searchInput.addEventListener("input", function () {
        searchClearBtn.style.display = searchInput.value.length > 0 ? "block" : "none";
    });

    searchClearBtn.addEventListener("click", function () {
        searchInput.value = "";
        searchClearBtn.style.display = "none";
        searchInput.focus();
    });

    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") doSearch();
    });

    confidenceSlider.addEventListener("input", function () {
        confidenceVal.textContent = confidenceSlider.value + "%";
        applyFiltersAndRender();
    });

    sortSelect.addEventListener("change", applyFiltersAndRender);
    sourceFilter.addEventListener("change", applyFiltersAndRender);

    document.querySelectorAll(".pill-btn").forEach(function (pill) {
        pill.addEventListener("click", function () {
            document.querySelectorAll(".pill-btn").forEach(function (p) { p.classList.remove("active"); });
            pill.classList.add("active");
            selectedTier = pill.getAttribute("data-tier");
            applyFiltersAndRender();
        });
    });

    function doSearch() {
        var q = searchInput.value.trim();
        if (!q) return;

        var reqQuery = q;
        resultsList.innerHTML = "<div class='loading-placeholder'>Querying FTS5 Porter stemmer and LanceDB vectors...</div>";
        resultsCountText.textContent = "Searching...";

        window.SovereignBridge.search({ query_text: reqQuery, top_k: 50 }).then(function (res) {
            lastGlobalSearchQuery = reqQuery;
            rawSearchResults = res.results || [];
            applyFiltersAndRender();
        }).catch(function (err) {
            resultsList.innerHTML = "<div class='alert-box alert-critical'>Search error: " + escapeHtml(err.message) + "</div>";
            resultsCountText.textContent = "Error";
        });
    }

    function applyFiltersAndRender() {
        if (!rawSearchResults || rawSearchResults.length === 0) {
            resultsList.innerHTML = "<div class='empty-state'><div class='empty-icon'>🔍</div><h3>No matching results</h3><p>Try different keywords, stem variations, or lower the confidence slider.</p></div>";
            resultsCountText.textContent = "0 results";
            return;
        }

        var minConf = parseInt(confidenceSlider.value, 10) / 100.0;
        var selectedSource = sourceFilter.value;
        var sortBy = sortSelect.value;

        var filtered = rawSearchResults.filter(function (item) {
            var conf = (item.confidence_score !== null && item.confidence_score !== undefined) ? item.confidence_score : item.normalized_rrf;
            if (conf < minConf) return false;
            if (selectedTier !== "ALL" && item.relevance_tier !== selectedTier) return false;
            if (selectedSource !== "ALL" && item.source_type !== selectedSource) return false;
            return true;
        });

        filtered.sort(function (a, b) {
            if (sortBy === "date-desc") {
                return (new Date(b.received_ts_utc || 0).getTime()) - (new Date(a.received_ts_utc || 0).getTime());
            } else if (sortBy === "date-asc") {
                return (new Date(a.received_ts_utc || 0).getTime()) - (new Date(b.received_ts_utc || 0).getTime());
            } else if (sortBy === "confidence") {
                var ca = a.confidence_score !== null ? a.confidence_score : a.normalized_rrf;
                var cb = b.confidence_score !== null ? b.confidence_score : b.normalized_rrf;
                return cb - ca;
            } else {
                if (a.exact_identifier_match !== b.exact_identifier_match) return a.exact_identifier_match ? -1 : 1;
                return b.rrf_score - a.rrf_score;
            }
        });

        resultsCountText.textContent = "Showing " + filtered.length + " of " + rawSearchResults.length + " results";
        renderSearchResults(filtered);
    }

    function renderSearchResults(items) {
        if (items.length === 0) {
            resultsList.innerHTML = "<div class='empty-state'><div class='empty-icon'>🎯</div><h3>No items match active filters</h3><p>Lower the confidence slider to view lower-confidence hits.</p></div>";
            return;
        }

        var html = "";
        items.forEach(function (item) {
            var tierClass = item.relevance_tier === "HIGH" ? "tier-high" : (item.relevance_tier === "MEDIUM" ? "tier-med" : "tier-low");
            var conf = (item.confidence_score !== null && item.confidence_score !== undefined) ? item.confidence_score : item.normalized_rrf;
            var confPct = Math.round(conf * 100) + "%";
            var badgeText = confPct + " " + item.relevance_tier;

            html += "<div class='result-card'>";
            html += "<div class='card-header-row'>";
            html += "<div class='card-title-group'>";
            html += "<span class='source-badge'>" + (item.source_type === "EMAIL" ? "📧 Outlook" : "📁 File") + "</span>";
            if (item.exact_identifier_match) html += "<span class='id-match-badge'>Exact ID Match</span>";
            html += "<span class='card-title'>" + escapeHtml(item.subject || item.folder || "Document Hit") + "</span>";
            html += "</div>";
            html += "<div class='badges-right'><span class='tier-badge " + tierClass + "'>" + badgeText + "</span></div>";
            html += "</div>";

            html += "<div class='card-meta-row'>";
            if (item.sender) html += "<span class='card-meta-item'>From: <strong>" + escapeHtml(item.sender) + "</strong></span>";
            if (item.folder) html += "<span class='card-meta-item'>Folder: <strong>" + escapeHtml(item.folder) + "</strong></span>";
            if (item.received_ts_utc) html += "<span class='card-meta-item'>Date: <strong>" + formatDate(item.received_ts_utc) + "</strong></span>";
            html += "</div>";

            html += "<div class='card-snippet'>" + formatSnippet(item.content) + "</div>";

            html += "<div class='card-footer-row'>";
            html += "<div class='metrics-debug'>";
            if (item.lexical_rank) html += "<span>FTS: #" + item.lexical_rank + "</span>";
            if (item.vector_rank) html += "<span>Vec: #" + item.vector_rank + "</span>";
            if (item.cosine_similarity !== null && item.cosine_similarity !== undefined) html += "<span>Cos: " + item.cosine_similarity.toFixed(3) + "</span>";
            html += "</div>";

            var expKey = registerExplainPayload({
                item_kind: "search_hit",
                query_text: lastGlobalSearchQuery || searchInput.value.trim(),
                doc_id: item.doc_id,
                chunk_id: item.chunk_id,
                source_context: { surface: "global_search" },
                score_snapshot: item
            });
            html += "<div class='card-actions-right'>";
            html += "<button type='button' class='btn btn-explain-sm' data-explain-key='" + expKey + "' title='Explain why this result matched and its scoring'>✨ Why?</button>";
            if (item.has_archived_msg) {
                html += "<a class='action-link' onclick='openCachedMsg(\"" + escapeHtml(item.doc_id) + "\")'>📄 Open Cached .msg Copy</a>";
            }
            html += "</div>";
            html += "</div>";
            html += "</div>";
        });

        resultsList.innerHTML = html;
    }

    window.openCachedMsg = function (docId) {
        window.SovereignBridge.openCachedMsg(docId).then(function (res) {
            if (!res.success) alert("Could not open cached file: " + res.error);
        });
    };

    // =========================================================================
    // SECTION 5: OVERVIEW, STORAGE & METRICS
    // =========================================================================
    function loadOverview() {
        var grid = document.getElementById("connection-grid");
        grid.innerHTML = "<div class='loading-placeholder'>Loading connections...</div>";

        window.SovereignBridge.getAdminOverview().then(function (data) {
            updateDaemonStatus(data.daemon_running);
            document.getElementById("items-indexed-stat").textContent = (data.items_indexed || 0) + " Items Ingested";

            var html = "";
            (data.connections || []).forEach(function (c) {
                var statusClass = c.status === "CONNECTED" || c.status === "ONLINE" || c.status === "ACTIVE" ? "badge-success" : (c.status === "CONFIGURED" ? "badge-info" : "badge-warning");
                html += "<div class='node-card'>";
                html += "<div class='node-card-top'><span class='node-name'>" + escapeHtml(c.name) + "</span><span class='badge " + statusClass + "'>" + escapeHtml(c.status) + "</span></div>";
                html += "<div class='node-details'>" + escapeHtml(c.details) + "</div>";
                html += "<div class='node-footer'><span>Sync: " + escapeHtml(c.last_sync) + "</span><span>" + (c.enabled ? "Enabled" : "Disabled") + "</span></div>";
                html += "</div>";
            });
            grid.innerHTML = html;
        });
    }

    document.getElementById("overview-refresh-btn").addEventListener("click", loadOverview);
    document.getElementById("reindex-btn").addEventListener("click", function () {
        var btn = document.getElementById("reindex-btn");
        var st = document.getElementById("reindex-status");
        btn.disabled = true;
        st.textContent = "Reindexing CAS archive...";
        window.SovereignBridge.reindex().then(function (res) {
            btn.disabled = false;
            st.textContent = "Done! Reindexed " + (res.count || 0) + " docs.";
            loadOverview();
        });
    });

    function loadStorage() {
        window.SovereignBridge.getAdminOverview().then(function (data) {
            var st = data.storage || {};
            var pr = data.privacy || {};
            document.getElementById("storage-path-input").value = st.root_path || "";
            document.getElementById("storage-total-bytes").textContent = st.total_formatted || "0 MB";

            var alertBox = document.getElementById("privacy-alert-box");
            if (pr.warning) {
                alertBox.className = pr.is_critical ? "alert-box alert-critical" : "alert-box alert-warning";
                alertBox.innerHTML = "<strong>⚠️ Privacy Warning:</strong> " + escapeHtml(pr.warning);
            } else {
                alertBox.className = "alert-box alert-success";
                alertBox.innerHTML = "<strong>🛡️ Storage Boundary Safe:</strong> Archive resides strictly within the local private user profile.";
            }

            var grid = document.getElementById("storage-breakdown-grid");
            var html = "";
            for (var k in (st.breakdown || {})) {
                var it = st.breakdown[k];
                html += "<div class='storage-item-card'><div class='storage-item-label'>" + escapeHtml(it.label) + "</div><div class='storage-item-val'>" + escapeHtml(it.formatted) + "</div></div>";
            }
            grid.innerHTML = html;
        });
    }

    document.getElementById("sweep-views-btn").addEventListener("click", function () {
        window.SovereignBridge.sweepTempViews().then(function (res) {
            document.getElementById("sweep-status").textContent = "Freed " + (res.freed_formatted || "0 B");
            loadStorage();
        });
    });

    function loadMetrics() {
        window.SovereignBridge.getStatistics().then(function (s) {
            var tot = s.total_queries || 0;
            document.getElementById("metric-total-queries").textContent = tot;
            document.getElementById("metric-hybrid-pct").textContent = tot > 0 ? Math.round((s.resolved_by_hybrid / tot) * 100) + "%" : "0%";
            document.getElementById("metric-hybrid-sub").textContent = (s.resolved_by_hybrid || 0) + " resolved by dual synergy";
            document.getElementById("metric-vector-pct").textContent = tot > 0 ? Math.round((s.resolved_by_vector / tot) * 100) + "%" : "0%";
            document.getElementById("metric-vector-sub").textContent = (s.resolved_by_vector || 0) + " resolved by semantic meaning";
            document.getElementById("metric-lexical-pct").textContent = tot > 0 ? Math.round((s.resolved_by_lexical / tot) * 100) + "%" : "0%";
            document.getElementById("metric-lexical-sub").textContent = (s.resolved_by_lexical || 0) + " resolved by keyword";
            document.getElementById("metric-storage-saved").textContent = formatBytes(s.storage_saved_bytes || 0);
            document.getElementById("metric-total-docs").textContent = s.total_documents || 0;
            document.getElementById("metric-docs-sub").textContent = (s.total_chunks || 0) + " vector chunks in LanceDB";

            var tbody = document.getElementById("recent-queries-tbody");
            var rec = s.recent_queries || [];
            if (rec.length === 0) {
                tbody.innerHTML = "<tr><td colspan='5' class='text-center'>No queries logged yet.</td></tr>";
            } else {
                var html = "";
                rec.forEach(function (q) {
                    var cls = q.resolved_by === "hybrid" ? "badge-success" : (q.resolved_by === "vector" ? "badge-info" : "badge-warning");
                    var conf = q.top_confidence ? Math.round(q.top_confidence * 100) + "%" : "N/A";
                    html += "<tr><td><strong>" + escapeHtml(q.query_text) + "</strong></td><td><span class='badge " + cls + "'>" + escapeHtml(q.resolved_by) + "</span></td><td>" + q.hit_count + "</td><td>" + conf + "</td><td>" + escapeHtml((q.executed_at_utc || "").substring(0, 19).replace("T", " ")) + "</td></tr>";
                });
                tbody.innerHTML = html;
            }
        });
    }

    document.getElementById("metrics-refresh-btn").addEventListener("click", loadMetrics);

    // =========================================================================
    // SECTION 6: TWO-STAGE RELEVANCE EXPLANATION MODAL (TIER 1 + TIER 2)
    // =========================================================================
    var modalExplain = document.getElementById("modal-explain");
    var modalExplainClose = document.getElementById("modal-explain-close");
    var explainCloseBtn = document.getElementById("explain-close-btn");
    var explainRetryBtn = document.getElementById("explain-retry-btn");
    var explainItemBanner = document.getElementById("explain-item-banner");
    var explainTier1Badge = document.getElementById("explain-tier1-badge");
    var explainTier1Content = document.getElementById("explain-tier1-content");
    var explainTier2Meta = document.getElementById("explain-tier2-meta");
    var explainTier2Body = document.getElementById("explain-tier2-body");

    if (modalExplainClose) modalExplainClose.addEventListener("click", closeExplainModal);
    if (explainCloseBtn) explainCloseBtn.addEventListener("click", closeExplainModal);
    if (modalExplain) {
        modalExplain.addEventListener("click", function (e) {
            if (e.target === modalExplain) closeExplainModal();
        });
    }
    if (explainRetryBtn) {
        explainRetryBtn.addEventListener("click", function () {
            if (!lastExplainState.payload) return;
            var retryReqId = ++activeExplainRequestId;
            triggerTier2Synthesis(lastExplainState.payload, lastExplainState.tier1, retryReqId, true);
        });
    }

    document.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest("[data-explain-key]") : null;
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        var key = btn.getAttribute("data-explain-key");
        var payload = explainRegistry[key];
        if (payload) openExplainModal(payload);
    });

    document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        if (modalExplain && modalExplain.style.display === "flex") {
            closeExplainModal();
        } else if (modalCalDetail && modalCalDetail.style.display === "flex") {
            closeCalDetailModal();
        }
    });

    function closeExplainModal() {
        ++activeExplainRequestId;
        if (modalExplain) modalExplain.style.display = "none";
    }

    function renderExplainLoadingProgress(isRetry) {
        var statusTxt = isRetry
            ? "Retrying AuraRouter connection & synthesizing explanation..."
            : "Requesting grounded AI reasoning from AuraRouter...";
        return (
            "<div class='explain-progress-wrap'>" +
            "<div class='explain-progress-track'><div class='explain-progress-fill'></div></div>" +
            "<div class='explain-progress-status'>" + escapeHtml(statusTxt) + "</div>" +
            "<div class='explain-progress-hint'>Tier 1 local mathematical evidence above is already complete and stays available if AuraRouter is offline.</div>" +
            "</div>"
        );
    }

    function renderExplainBanner(docOrSnap, payload, scoring) {
        if (!explainItemBanner) return;
        var snap = (payload && payload.score_snapshot) || {};
        var srcCtx = (payload && payload.source_context) || {};
        var isCal = (payload && payload.item_kind) === "calendar_event";
        var srcType = (docOrSnap && docOrSnap.source_type) || snap.source_type || (isCal ? "CALENDAR" : "EMAIL");
        var typeIcon = srcType === "FILE" ? "📁 File" : (srcType === "CALENDAR" ? "📅 Calendar" : "📧 Email");

        var rawPath = (docOrSnap && docOrSnap.original_path) || snap.original_path || "";
        var pathLeaf = rawPath ? String(rawPath).split(/[\\/]/).pop() : "";
        var title = (docOrSnap && (docOrSnap.title || docOrSnap.subject)) || snap.subject || pathLeaf || payload.doc_id || "Archived Item";

        var tierBadgeHtml = "";
        if (scoring && scoring.relevance_tier) {
            var tc = scoring.relevance_tier === "HIGH" ? "tier-high" : (scoring.relevance_tier === "MEDIUM" ? "tier-med" : "tier-low");
            tierBadgeHtml = "<span class='tier-badge " + tc + "'>" + escapeHtml(String(scoring.confidence_pct)) + "% " + escapeHtml(scoring.relevance_tier) + "</span>";
        } else if (!isCal && (snap.confidence_score !== undefined || snap.normalized_rrf !== undefined)) {
            var rawC = (snap.confidence_score !== null && snap.confidence_score !== undefined) ? snap.confidence_score : (snap.normalized_rrf || 0);
            var tierName = snap.relevance_tier || "LOW";
            var tc2 = tierName === "HIGH" ? "tier-high" : (tierName === "MEDIUM" ? "tier-med" : "tier-low");
            tierBadgeHtml = "<span class='tier-badge " + tc2 + "'>" + Math.round(rawC * 100) + "% " + escapeHtml(tierName) + "</span>";
        }

        var metaParts = [];
        var senderVal = (docOrSnap && docOrSnap.sender) || snap.sender;
        var folderVal = (docOrSnap && docOrSnap.folder) || snap.folder;
        var locVal = (docOrSnap && docOrSnap.location) || snap.location;
        var tsVal = (docOrSnap && (docOrSnap.received_ts_utc || docOrSnap.start_ts_utc)) || snap.received_ts_utc || snap.start_ts_utc;
        if (senderVal) metaParts.push("<span>From: <strong>" + escapeHtml(senderVal) + "</strong></span>");
        if (folderVal) metaParts.push("<span>Folder: <strong>" + escapeHtml(folderVal) + "</strong></span>");
        if (locVal) metaParts.push("<span>Location: <strong>" + escapeHtml(locVal) + "</strong></span>");
        if (tsVal) metaParts.push("<span>Date: <strong>" + escapeHtml(formatDateLong(tsVal)) + "</strong></span>");
        if (payload && payload.query_text) {
            var qPrefix = srcCtx.origin_subject ? "Ambient Keywords" : "Query";
            metaParts.push("<span>" + qPrefix + ": <code>" + escapeHtml(payload.query_text) + "</code></span>");
        }

        explainItemBanner.innerHTML =
            "<div class='explain-banner-top'>" +
            "<div class='card-title-group'><span class='source-badge'>" + typeIcon + "</span><span class='explain-banner-title'>" + escapeHtml(title) + "</span></div>" +
            tierBadgeHtml +
            "</div>" +
            "<div class='explain-banner-meta'>" + metaParts.join("") + "</div>";
    }

    function openExplainModal(payload) {
        var reqId = ++activeExplainRequestId;
        lastExplainState = { payload: payload, tier1: null };

        renderExplainBanner(null, payload, null);

        if (explainTier1Badge) {
            explainTier1Badge.className = "badge badge-success";
            explainTier1Badge.textContent = "Instant Local Analysis";
        }
        if (explainTier1Content) {
            explainTier1Content.innerHTML = "<div class='loading-placeholder'>Computing deterministic scoring breakdown...</div>";
        }
        if (explainTier2Meta) {
            explainTier2Meta.className = "badge badge-info";
            explainTier2Meta.textContent = "Initializing...";
        }
        if (explainTier2Body) {
            explainTier2Body.innerHTML = renderExplainLoadingProgress(false);
        }
        if (explainRetryBtn) {
            explainRetryBtn.style.display = "none";
        }
        if (modalExplain) {
            modalExplain.style.display = "flex";
        }

        window.SovereignBridge.explainMatchTier1(payload).then(function (tier1Res) {
            if (reqId !== activeExplainRequestId) return;
            lastExplainState.tier1 = tier1Res;

            if (!tier1Res || !tier1Res.success) {
                if (explainTier1Badge) {
                    explainTier1Badge.className = "badge badge-warning";
                    explainTier1Badge.textContent = "Unavailable";
                }
                if (explainTier1Content) {
                    explainTier1Content.innerHTML = "<div class='alert-box alert-warning'>Deterministic explanation unavailable: " + escapeHtml((tier1Res && tier1Res.error) || "Unknown error") + "</div>";
                }
                triggerTier2Synthesis(payload, null, reqId, false);
                return;
            }

            renderExplainBanner(tier1Res.document, payload, tier1Res.scoring);
            renderExplainTier1(tier1Res);
            triggerTier2Synthesis(payload, tier1Res, reqId, false);
        }).catch(function (err) {
            if (reqId !== activeExplainRequestId) return;
            if (explainTier1Badge) {
                explainTier1Badge.className = "badge badge-warning";
                explainTier1Badge.textContent = "Error";
            }
            if (explainTier1Content) {
                explainTier1Content.innerHTML = "<div class='alert-box alert-warning'>Tier 1 error: " + escapeHtml(err && err.message ? err.message : String(err)) + "</div>";
            }
            triggerTier2Synthesis(payload, null, reqId, false);
        });
    }

    function renderExplainTier1(res) {
        if (!explainTier1Content) return;
        var scoring = res.scoring || {};
        var lex = res.lexical_evidence || {};
        var qa = res.query_analysis || {};
        var passage = res.passage || {};
        var doc = res.document || {};
        var channels = res.channels_matched || [];
        var bullets = res.summary_bullets || [];

        if (explainTier1Badge) {
            explainTier1Badge.className = "badge badge-success";
            explainTier1Badge.textContent = "Instant Local Analysis";
        }

        var html = "";

        // 1. Summary Bullets
        if (bullets.length > 0) {
            html += "<ul class='explain-bullets'>";
            bullets.forEach(function (b) {
                html += "<li>" + escapeHtml(b) + "</li>";
            });
            html += "</ul>";
        }

        // 2. Channels & Stopwords
        html += "<div class='explain-chips-row'>";
        html += "<span style='color:var(--text-muted);font-weight:600;'>Signals:</span>";
        if (channels.length > 0) {
            channels.forEach(function (ch) {
                html += "<span class='explain-chip explain-chip-channel'>" + escapeHtml(ch) + "</span>";
            });
        } else {
            html += "<span class='explain-chip explain-chip-miss'>No active channel recorded</span>";
        }
        if (qa.stopwords_removed && qa.stopwords_removed.length > 0) {
            html += "<span style='font-size:11px;color:var(--text-muted);margin-left:6px;'>Stopwords ignored: " + escapeHtml(qa.stopwords_removed.join(", ")) + "</span>";
        }
        html += "</div>";

        // 3. Token & Identifier Matches
        var matchedIds = lex.matched_identifiers || [];
        var matchedToks = lex.matched_tokens || [];
        var unmatchedToks = lex.unmatched_tokens || [];
        if (matchedIds.length > 0 || matchedToks.length > 0 || unmatchedToks.length > 0) {
            html += "<div class='explain-chips-row'>";
            html += "<span style='color:var(--text-muted);font-weight:600;'>Terms:</span>";
            matchedIds.forEach(function (idTok) {
                html += "<span class='explain-chip explain-chip-match'>ID: " + escapeHtml(idTok) + "</span>";
            });
            matchedToks.forEach(function (tok) {
                html += "<span class='explain-chip explain-chip-match'>✔ " + escapeHtml(tok) + "</span>";
            });
            unmatchedToks.forEach(function (tok) {
                html += "<span class='explain-chip explain-chip-miss'>✕ " + escapeHtml(tok) + "</span>";
            });
            html += "</div>";
        }

        // Calendar field breakdown if applicable
        if (res.item_kind === "calendar_event" && lex.matched_fields) {
            var fieldParts = [];
            for (var fKey in lex.matched_fields) {
                if (lex.matched_fields[fKey] && lex.matched_fields[fKey].length > 0) {
                    fieldParts.push(fKey + ": " + lex.matched_fields[fKey].join(", "));
                }
            }
            if (fieldParts.length > 0) {
                html += "<div style='font-size:12px;color:var(--text-secondary);'><strong>Calendar Fields Matched:</strong> " + escapeHtml(fieldParts.join(" | ")) + "</div>";
            }
        }

        // 4. Math / Scoring Grid
        html += "<div class='explain-math-grid'>";

        html += "<div class='explain-math-cell'>";
        html += "<div class='explain-math-label'>Calibrated Confidence</div>";
        html += "<div class='explain-math-val'>" + escapeHtml(String(scoring.confidence_pct || 0)) + "% (" + escapeHtml(scoring.relevance_tier || "LOW") + ")</div>";
        html += "<div class='explain-math-sub'>" + escapeHtml(scoring.calibration_rule || "RRF") + "</div>";
        html += "</div>";

        if (res.item_kind === "calendar_event") {
            html += "<div class='explain-math-cell'>";
            html += "<div class='explain-math-label'>Token Overlap</div>";
            html += "<div class='explain-math-val'>" + matchedToks.length + " / " + (matchedToks.length + unmatchedToks.length) + "</div>";
            html += "<div class='explain-math-sub'>" + escapeHtml(lex.strategy || "Calendar LIKE") + "</div>";
            html += "</div>";
        } else {
            var lexVal = scoring.lexical_rank !== null && scoring.lexical_rank !== undefined ? ("Rank #" + scoring.lexical_rank) : "No Rank";
            var lexSubParts = [];
            if (lex.bm25_score !== null && lex.bm25_score !== undefined) lexSubParts.push("BM25 " + lex.bm25_score);
            if (scoring.lexical_rrf_contribution !== undefined) lexSubParts.push("RRF +" + Number(scoring.lexical_rrf_contribution).toFixed(5));
            html += "<div class='explain-math-cell'>";
            html += "<div class='explain-math-label'>Lexical (FTS5 Porter)</div>";
            html += "<div class='explain-math-val'>" + escapeHtml(lexVal) + "</div>";
            html += "<div class='explain-math-sub'>" + escapeHtml(lexSubParts.join(" • ") || lex.strategy || "FTS5") + "</div>";
            html += "</div>";

            var vecVal = scoring.vector_rank !== null && scoring.vector_rank !== undefined ? ("Rank #" + scoring.vector_rank) : "No Rank";
            var vecSubParts = [];
            if (scoring.cosine_similarity !== null && scoring.cosine_similarity !== undefined) {
                vecSubParts.push("Cos " + Number(scoring.cosine_similarity).toFixed(3));
            }
            if (scoring.semantic_strength !== null && scoring.semantic_strength !== undefined) {
                vecSubParts.push("Strength " + Math.round(Number(scoring.semantic_strength) * 100) + "%");
            }
            if (scoring.vector_rrf_contribution) {
                vecSubParts.push("RRF +" + Number(scoring.vector_rrf_contribution).toFixed(5));
            }
            html += "<div class='explain-math-cell'>";
            html += "<div class='explain-math-label'>Semantic (LanceDB)</div>";
            html += "<div class='explain-math-val'>" + escapeHtml(vecVal) + "</div>";
            html += "<div class='explain-math-sub'>" + escapeHtml(vecSubParts.join(" • ") || "Dense Nomic v1.5") + "</div>";
            html += "</div>";
        }
        html += "</div>";

        // 5. Formula Box
        if (scoring.calibration_formula) {
            var formulaLine = scoring.calibration_formula;
            if (scoring.rrf_score !== null && scoring.rrf_score !== undefined) {
                formulaLine += " | total_rrf=" + scoring.rrf_score + " (k=" + (scoring.rrf_k || 60) + ")";
            }
            if (scoring.tier_thresholds) {
                formulaLine += " | " + scoring.tier_thresholds;
            }
            html += "<div class='explain-formula-box'>" + escapeHtml(formulaLine) + "</div>";
        }

        // 6. Full Highlighted Passage
        var passageHtml = passage.highlighted_html || passage.raw_text || "";
        var passageLen = passage.char_length || 0;
        var strategyTag = lex.strategy ? (" • " + lex.strategy) : "";
        html += "<div class='explain-passage-wrap'>";
        html += "<div class='explain-passage-label'><span>Matched Evidence Passage" + escapeHtml(strategyTag) + "</span><span>" + escapeHtml(String(passageLen)) + " chars</span></div>";
        html += "<div class='explain-passage-box'>" + (passageHtml ? formatSnippet(passageHtml) : "<em>No passage text stored in index.</em>") + "</div>";
        html += "</div>";

        if (doc.attachments && doc.attachments.length > 0) {
            html += "<div style='font-size:11px;color:var(--text-muted);'><strong>Attachments in Document:</strong> " + escapeHtml(doc.attachments.join(", ")) + "</div>";
        }

        explainTier1Content.innerHTML = html;
    }

    function triggerTier2Synthesis(payload, tier1Res, reqId, forceRefresh) {
        if (!explainTier2Body || !explainTier2Meta) return;

        explainTier2Meta.className = "badge badge-info";
        explainTier2Meta.textContent = forceRefresh ? "Refreshing..." : "Synthesizing...";
        if (explainRetryBtn) explainRetryBtn.style.display = "none";
        explainTier2Body.innerHTML = renderExplainLoadingProgress(!!forceRefresh);

        var t2Payload = Object.assign({}, payload, {
            tier1: tier1Res || null,
            force_refresh: !!forceRefresh
        });

        window.SovereignBridge.explainMatchTier2(t2Payload).then(function (t2) {
            if (reqId !== activeExplainRequestId) return;

            if (t2 && t2.success) {
                var modelLabel = t2.model_id || "AuraRouter";
                var timingLabel = t2.cached ? "Cached (0ms)" : ((t2.latency_ms || 0) + "ms");
                explainTier2Meta.className = "badge badge-success";
                explainTier2Meta.textContent = modelLabel + " • " + timingLabel;

                var metaItems = [];
                if (t2.provider) metaItems.push("<span>Provider: <strong>" + escapeHtml(t2.provider) + "</strong></span>");
                if (t2.role) metaItems.push("<span>Role: <strong>" + escapeHtml(t2.role) + "</strong></span>");
                if (t2.transport) metaItems.push("<span>Transport: <strong>" + escapeHtml(t2.transport) + "</strong></span>");
                metaItems.push("<span>Boundary: <strong>" + (t2.local_only !== false ? "🛡️ Local Sovereign Only" : "Hybrid Router") + "</strong></span>");
                if (t2.output_tokens) metaItems.push("<span>Tokens: " + escapeHtml(String(t2.input_tokens || 0)) + " in / " + escapeHtml(String(t2.output_tokens)) + " out</span>");

                explainTier2Body.innerHTML =
                    "<div class='explain-tier2-success'>" + escapeHtml(t2.explanation || "") + "</div>" +
                    "<div class='explain-tier2-footer-meta'>" + metaItems.join("") + "</div>";

                if (explainRetryBtn) {
                    explainRetryBtn.textContent = "🔄 Regenerate";
                    explainRetryBtn.style.display = "inline-flex";
                }
            } else {
                var errCode = (t2 && t2.error_code) ? t2.error_code : "OFFLINE";
                var errMsg = (t2 && t2.error) ? t2.error : "Could not reach AuraRouter inference service.";
                explainTier2Meta.className = "badge badge-warning";
                explainTier2Meta.textContent = "Unavailable (" + errCode + ")";

                explainTier2Body.innerHTML =
                    "<div class='explain-tier2-error'>" +
                    "<strong>⚠️ AuraRouter reasoning unavailable</strong>" +
                    "<span>" + escapeHtml(errMsg) + "</span>" +
                    "<span style='color:var(--text-muted);'>Tier 1 deterministic evidence and highlighted passage above remain complete and accurate.</span>" +
                    "</div>";

                if (explainRetryBtn) {
                    explainRetryBtn.textContent = "🔄 Retry Connection";
                    explainRetryBtn.style.display = "inline-flex";
                }
            }
        }).catch(function (err) {
            if (reqId !== activeExplainRequestId) return;
            var msg = err && err.message ? err.message : String(err);
            explainTier2Meta.className = "badge badge-warning";
            explainTier2Meta.textContent = "Connection Error";
            explainTier2Body.innerHTML =
                "<div class='explain-tier2-error'>" +
                "<strong>⚠️ AuraRouter request failed</strong>" +
                "<span>" + escapeHtml(msg) + "</span>" +
                "<span style='color:var(--text-muted);'>Tier 1 deterministic evidence above remains intact.</span>" +
                "</div>";
            if (explainRetryBtn) {
                explainRetryBtn.textContent = "🔄 Retry Connection";
                explainRetryBtn.style.display = "inline-flex";
            }
        });
    }

    // =========================================================================
    // UTILITY HELPERS
    // =========================================================================
    function formatDate(isoStr) {
        if (!isoStr) return "";
        var d = new Date(isoStr);
        if (isNaN(d.getTime())) return isoStr.substring(0, 10);
        return d.toLocaleDateString([], { month: "short", day: "numeric" });
    }

    function formatDateLong(isoStr) {
        if (!isoStr) return "";
        var d = new Date(isoStr);
        if (isNaN(d.getTime())) return isoStr;
        return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function formatBytes(bytes) {
        if (!bytes || bytes === 0) return "0 MB";
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    }

    function escapeHtml(text) {
        if (!text) return "";
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
    }

    function formatSnippet(text) {
        if (!text) return "";
        return escapeHtml(text).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
    }

    function padZero(num) { return num < 10 ? "0" + num : num; }

    function debounce(func, wait) {
        var timeout;
        return function () {
            var context = this, args = arguments;
            clearTimeout(timeout);
            timeout = setTimeout(function () { func.apply(context, args); }, wait);
        };
    }

    // Initial load
    loadMailbox();
    if (window.OldRAGChatView) window.OldRAGChatView.init();
    if (window.OldRAGSyncQuotaView) window.OldRAGSyncQuotaView.init();
    if (window.OldRAGSearchView) window.OldRAGSearchView.init();

    // Background event polling loop
    var lastEventSeq = 0;
    function pollServerEvents() {
        if (window.OldRAGApi && typeof window.OldRAGApi.pollEvents === "function") {
            window.OldRAGApi.pollEvents(lastEventSeq).then(function (resp) {
                var data = resp.ok ? resp.data : resp;
                if (data && data.latest_seq) {
                    lastEventSeq = data.latest_seq;
                }
            }).catch(function () {}).finally(function () {
                setTimeout(pollServerEvents, 4000);
            });
        } else {
            setTimeout(pollServerEvents, 4000);
        }
    }
    setTimeout(pollServerEvents, 2000);

    window.SovereignBridge.getStatus().then(function (s) {
        var running = s && s.daemon_running !== undefined ? s.daemon_running : (s.data && s.data.daemon_running);
        updateDaemonStatus(Boolean(running));
    }).catch(function () {
        updateDaemonStatus(false);
    });
});
