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
        }
    };

    root.SovereignBridge = SovereignBridge;
})(window);
