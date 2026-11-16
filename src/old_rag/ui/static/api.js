/* ==========================================================================
   OldRAG Sovereign Bridge API Client & Shared Formatting Helpers
   Pure Vanilla JS | Zero External CDNs | 100% Offline Desktop Bridge
   ========================================================================== */

(function (global) {
  "use strict";

  let customTransport = null;

  function setTransport(transportFn) {
    customTransport = typeof transportFn === "function" ? transportFn : null;
  }

  async function waitForPyWebViewApi(timeoutMs = 2000) {
    if (global.pywebview?.api?.dispatch) return global.pywebview.api;
    if (!global.__OLDRAG_DESKTOP_MODE__ && !global.__BIJOU_DESKTOP_MODE__) return global.pywebview?.api || null;
    return await new Promise((resolve) => {
      const started = Date.now();
      const onReady = () => {
        window.removeEventListener("pywebviewready", onReady);
        resolve(global.pywebview?.api || null);
      };
      window.addEventListener("pywebviewready", onReady);
      const poll = () => {
        if (global.pywebview?.api) {
          window.removeEventListener("pywebviewready", onReady);
          resolve(global.pywebview.api);
          return;
        }
        if (Date.now() - started >= timeoutMs) {
          window.removeEventListener("pywebviewready", onReady);
          resolve(null);
          return;
        }
        setTimeout(poll, 25);
      };
      setTimeout(poll, 25);
    });
  }

  async function invokeBridge(action, payload) {
    const reqPayload = payload || {};
    let envelope = null;
    if (customTransport) {
      envelope = await customTransport(action, reqPayload);
    } else if (typeof global.__OLDRAG_BRIDGE__ === "function") {
      envelope = await global.__OLDRAG_BRIDGE__(action, reqPayload);
    } else {
      const pywebviewApi = await waitForPyWebViewApi();
      if (pywebviewApi) {
        if (typeof pywebviewApi.dispatch === "function") {
          envelope = await pywebviewApi.dispatch(action, reqPayload);
        } else if (typeof pywebviewApi[action] === "function") {
          const res = await pywebviewApi[action](reqPayload);
          envelope = { ok: true, status: 200, data: res, error: null, error_code: "OK" };
        }
      }
    }
    if (!envelope) {
      envelope = {
        ok: false,
        status: 503,
        data: null,
        error: "OldRAG pywebview native bridge not connected",
        error_code: "SERVICE_UNAVAILABLE",
      };
    }
    // Unwrap data fields onto envelope so callers can access either envelope.data.xyz or envelope.xyz
    if (envelope && envelope.data && typeof envelope.data === "object" && !Array.isArray(envelope.data)) {
      Object.assign(envelope, envelope.data);
    }
    if (envelope && envelope.ok !== undefined && envelope.success === undefined) {
      envelope.success = Boolean(envelope.ok);
    }
    return envelope;
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatBytes(bytes, decimals = 1) {
    if (!bytes || bytes === 0) return "0 B";
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
  }

  function formatTimestamp(isoString) {
    if (!isoString) return "";
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString);
      return d.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      return String(isoString);
    }
  }

  // --- OldRAGApi Definition ---
  const OldRAGApi = {
    setTransport,
    waitForPyWebViewApi,
    invokeBridge,
    escapeHtml,
    formatBytes,
    formatTimestamp,

    // 1. Search & Context Actions
    search: function (payload) {
      return invokeBridge("search", payload);
    },
    getStatus: function (payload) {
      return invokeBridge("get_status", payload || {});
    },
    openCachedMsg: function (payload) {
      const req = typeof payload === "string" ? { doc_id: payload } : payload;
      return invokeBridge("open_cached_msg", req);
    },
    getRelatedContext: function (payload) {
      return invokeBridge("get_related_context", payload || {});
    },
    openDocument: function (payload) {
      return invokeBridge("open_document", payload || {});
    },
    recordAccess: function (payload) {
      const req = typeof payload === "string" ? { doc_id: payload } : payload;
      return invokeBridge("record_access", req);
    },
    explainMatchTier1: function (payload) {
      return invokeBridge("explain_match_tier1", payload || {});
    },
    explainMatchTier2: function (payload) {
      return invokeBridge("explain_match_tier2", payload || {});
    },

    // 2. Mail Actions
    listAccounts: function (payload) {
      return invokeBridge("list_accounts", payload || {});
    },
    saveAccount: function (payload) {
      const req = payload && payload.account ? payload : { account: payload };
      return invokeBridge("save_account", req);
    },
    deleteAccount: function (payload) {
      const req = typeof payload === "string" ? { account_id: payload } : payload;
      return invokeBridge("delete_account", req);
    },
    purgeAccountData: function (payload) {
      const req = typeof payload === "string" ? { account_id: payload } : payload;
      return invokeBridge("purge_account_data", req);
    },
    sendMail: function (payload) {
      return invokeBridge("send_mail", payload || {});
    },
    listMailboxFolders: function (payload) {
      const req = typeof payload === "string" ? { account_id: payload } : payload;
      return invokeBridge("list_mailbox_folders", req || {});
    },
    listMail: function (payload) {
      return invokeBridge("list_mail", payload || {});
    },
    getMailDetail: function (payload) {
      const req = typeof payload === "string" ? { doc_id: payload } : payload;
      return invokeBridge("get_mail_detail", req);
    },

    // 3. Calendar Actions
    listCalendarAccounts: function (payload) {
      return invokeBridge("list_calendar_accounts", payload || {});
    },
    saveCalendarAccount: function (payload) {
      const req = payload && payload.account ? payload : { account: payload };
      return invokeBridge("save_calendar_account", req);
    },
    deleteCalendarAccount: function (payload) {
      const req = typeof payload === "string" ? { account_id: payload } : payload;
      return invokeBridge("delete_calendar_account", req);
    },
    listCalendarEvents: function (payload) {
      return invokeBridge("list_calendar_events", payload || {});
    },
    createCalendarEvent: function (payload) {
      const req = payload && payload.event ? payload : { event: payload };
      return invokeBridge("create_calendar_event", req);
    },
    importIcs: function (payload) {
      const req = typeof payload === "string" ? { ics_content: payload } : payload;
      return invokeBridge("import_ics", req);
    },
    exportIcs: function (payload) {
      return invokeBridge("export_ics", payload || {});
    },

    // 4. Chat Adapter Plugin Actions
    listChatPlugins: function (payload) {
      return invokeBridge("list_chat_plugins", payload || {});
    },
    listChatAccounts: function (payload) {
      return invokeBridge("list_chat_accounts", payload || {});
    },
    saveChatAccount: function (payload) {
      const req = payload && payload.account ? payload : { account: payload };
      return invokeBridge("save_chat_account", req);
    },
    deleteChatAccount: function (payload) {
      const req = typeof payload === "string" ? { account_id: payload } : payload;
      return invokeBridge("delete_chat_account", req);
    },
    testChatConnection: function (payload) {
      const req = payload && payload.account ? payload : { account: payload };
      return invokeBridge("test_chat_connection", req);
    },
    listChatRooms: function (payload) {
      return invokeBridge("list_chat_rooms", payload || {});
    },
    updateRoomSubscription: function (payload) {
      return invokeBridge("update_room_subscription", payload || {});
    },
    listChatMessages: function (payload) {
      return invokeBridge("list_chat_messages", payload || {});
    },
    sendChatMessage: function (payload) {
      const req = payload && payload.message ? payload : { message: payload };
      return invokeBridge("send_chat_message", req);
    },

    // 5. Sync & Quota Actions
    getSyncStatus: function (payload) {
      return invokeBridge("get_sync_status", payload || {});
    },
    updateSyncPolicy: function (payload) {
      const req = payload && payload.policy ? payload : { policy: payload };
      return invokeBridge("update_sync_policy", req);
    },
    triggerSync: function (payload) {
      return invokeBridge("trigger_sync", payload || {});
    },
    getQuotaStatus: function (payload) {
      const req = typeof payload === "string" ? { source_type: payload } : payload;
      return invokeBridge("get_quota_status", req || {});
    },
    updateQuotaPolicy: function (payload) {
      const req = payload && payload.config ? payload : { config: payload };
      return invokeBridge("update_quota_policy", req);
    },
    previewQuotaEviction: function (payload) {
      return invokeBridge("preview_quota_eviction", payload || {});
    },
    runQuotaPurge: function (payload) {
      return invokeBridge("run_quota_purge", payload || {});
    },

    // 6. Admin & Storage Actions
    getAdminOverview: function (payload) {
      return invokeBridge("get_admin_overview", payload || {});
    },
    getStatistics: function (payload) {
      return invokeBridge("get_statistics", payload || {});
    },
    getSettings: function (payload) {
      return invokeBridge("get_settings", payload || {});
    },
    saveSettings: function (payload) {
      const req = payload && payload.settings ? payload : { settings: payload };
      return invokeBridge("save_settings", req);
    },
    saveDashboardLayout: function (payload) {
      const req = payload && payload.layout ? payload : { layout: payload };
      return invokeBridge("save_dashboard_layout", req);
    },
    getDashboardLayout: function (payload) {
      return invokeBridge("get_dashboard_layout", payload || {});
    },
    scanHeadOfFolder: function (payload) {
      return invokeBridge("scan_head_of_folder", payload || {});
    },
    getHeadOfFolderStatus: function (payload) {
      return invokeBridge("get_head_of_folder_status", payload || {});
    },
    pollEvents: function (payload) {
      const req = typeof payload === "number" ? { since_seq: payload } : payload;
      return invokeBridge("poll_events", req || {});
    },

    // Backward-compatibility aliases
    listMessages: function (payload) {
      return invokeBridge("list_mail", payload || {});
    },
    getMessageDetail: function (payload) {
      return invokeBridge("get_mail_detail", typeof payload === "string" ? { doc_id: payload } : payload);
    },
    sendEmail: function (payload) {
      return invokeBridge("send_mail", payload || {});
    },
    reindex: function (payload) {
      return invokeBridge("get_status", payload || {});
    },
    testImapConnection: function (payload) {
      return invokeBridge("save_account", payload || {});
    },
    sweepTempViews: function (payload) {
      return invokeBridge("get_status", payload || {});
    },
    listOutlookFolders: function (payload) {
      return invokeBridge("list_mailbox_folders", payload || {});
    },
  };

  // Expose as window.OldRAGApi and window.SovereignBridge
  global.OldRAGApi = OldRAGApi;
  if (!global.SovereignBridge) {
    global.SovereignBridge = OldRAGApi;
  } else {
    Object.assign(global.SovereignBridge, OldRAGApi);
  }
})(window);
