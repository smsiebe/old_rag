document.addEventListener("DOMContentLoaded", function () {
    var searchInput = document.getElementById("search-input");
    var searchBtn = document.getElementById("search-btn");
    var resultsList = document.getElementById("results-list");

    function doSearch() {
        var query = searchInput.value.trim();
        if (!query) return;

        resultsList.innerHTML = "<div style='color:#909296;padding:12px;'>Searching...</div>";

        window.SovereignBridge.search({ query_text: query, top_k: 25 })
            .then(function (response) {
                renderResults(response.results || []);
            })
            .catch(function (err) {
                resultsList.innerHTML = "<div style='color:#fa5252;padding:12px;'>Error: " + err.message + "</div>";
            });
    }

    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") doSearch();
    });

    function renderResults(results) {
        if (results.length === 0) {
            resultsList.innerHTML = "<div style='color:#909296;padding:12px;'>No matching items found.</div>";
            return;
        }

        var html = "";
        for (var i = 0; i < results.length; i++) {
            var item = results[i];
            var tierClass = "tier-low";
            if (item.relevance_tier === "HIGH") tierClass = "tier-high";
            else if (item.relevance_tier === "MEDIUM") tierClass = "tier-med";

            var badgeText = "#" + (i + 1) + " " + item.relevance_tier;

            html += "<div class='result-card'>";
            html += "<div class='card-header'>";
            html += "<strong>" + (item.source_type === "EMAIL" ? "📧 Outlook Item" : "📁 File") + "</strong>";
            html += "<span class='tier-badge " + tierClass + "'>" + badgeText + "</span>";
            html += "</div>";

            html += "<div class='card-meta'>";
            if (item.sender) html += "<span>From: " + escapeHtml(item.sender) + "</span> | ";
            if (item.folder) html += "<span>Folder: " + escapeHtml(item.folder) + "</span>";
            html += "</div>";

            html += "<div class='card-content'>" + formatSnippet(item.content) + "</div>";

            if (item.has_archived_msg) {
                html += "<a class='action-link' onclick='openMsg(\"" + item.doc_id + "\")'>Open Cached .msg Copy</a>";
            }
            html += "</div>";
        }
        resultsList.innerHTML = html;
    }

    window.openMsg = function (docId) {
        window.SovereignBridge.openCachedMsg(docId).then(function (res) {
            if (!res.success) alert("Could not open: " + res.error);
        });
    };

    function escapeHtml(text) {
        if (!text) return "";
        return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function formatSnippet(text) {
        if (!text) return "";
        var safe = escapeHtml(text);
        return safe.replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
    }
});

