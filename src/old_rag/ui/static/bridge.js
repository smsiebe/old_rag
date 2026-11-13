/**
 * bridge.js - Generated IIFE Client for pywebview Native Bridge
 * Strictly conforms to bridge_schema.json with ZERO external CDN references.
 */
(function (root) {
    "use strict";

    var isReady = false;
    var readyCallbacks = [];

    window.addEventListener("pywebviewready", function () {
        isReady = true;
        for (var i = 0; i < readyCallbacks.length; i++) {
            readyCallbacks[i]();
        }
        readyCallbacks = [];
    });

    function callApi(method, payload) {
        return new Promise(function (resolve, reject) {
            function execute() {
                if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api[method] !== "function") {
                    reject(new Error("Bridge method not available: " + method));
                    return;
                }
                window.pywebview.api[method](payload).then(resolve).catch(reject);
            }

            if (isReady) {
                execute();
            } else {
                readyCallbacks.push(execute);
            }
        });
    }

    var SovereignBridge = {
        search: function (request) {
            return callApi("search", request);
        },
        getStatus: function () {
            return callApi("get_status", {});
        },
        openCachedMsg: function (docId) {
            return callApi("open_cached_msg", { doc_id: docId });
        },
        reindex: function () {
            return callApi("reindex", {});
        },
        getAdminOverview: function () {
            return callApi("get_admin_overview", {});
        },
        getStatistics: function () {
            return callApi("get_statistics", {});
        },
        getSettings: function () {
            return callApi("get_settings", {});
        },
        saveSettings: function (settings) {
            return callApi("save_settings", { settings: settings });
        },
        testImapConnection: function (config) {
            return callApi("test_imap_connection", { config: config });
        },
        sweepTempViews: function () {
            return callApi("sweep_temp_views", {});
        },
        listOutlookFolders: function () {
            return callApi("list_outlook_folders", {});
        },
        listAccounts: function () {
            return callApi("list_accounts", {});
        },
        saveAccount: function (account) {
            return callApi("save_account", { account: account });
        },
        deleteAccount: function (accountId) {
            return callApi("delete_account", { account_id: accountId });
        },
        listMailboxFolders: function (accountId) {
            return callApi("list_mailbox_folders", { account_id: accountId });
        },
        listMessages: function (options) {
            return callApi("list_messages", options || {});
        },
        getMessageDetail: function (docId) {
            return callApi("get_message_detail", { doc_id: docId });
        },
        sendEmail: function (options) {
            return callApi("send_email", options);
        },
        listCalendarEvents: function () {
            return callApi("list_calendar_events", {});
        },
        createCalendarEvent: function (event) {
            return callApi("create_calendar_event", { event: event });
        },
        scanHeadOfFolder: function (folderPath, recursive) {
            return callApi("scan_head_of_folder", { folder_path: folderPath, recursive: recursive !== false });
        },
        getHeadOfFolderStatus: function (folderPath) {
            return callApi("get_head_of_folder_status", { folder_path: folderPath });
        },
        getRelatedContext: function (docId, subject, text, topK) {
            return callApi("get_related_context", {
                doc_id: docId,
                subject: subject || "",
                text: text || "",
                top_k: topK || 6
            });
        },
        openDocument: function (docId, originalPath) {
            return callApi("open_document", {
                doc_id: docId,
                original_path: originalPath
            });
        },
        explainMatchTier1: function (request) {
            return callApi("explain_match_tier1", request || {});
        },
        explainMatchTier2: function (request) {
            return callApi("explain_match_tier2", request || {});
        }
    };

    root.SovereignBridge = SovereignBridge;
})(window);

