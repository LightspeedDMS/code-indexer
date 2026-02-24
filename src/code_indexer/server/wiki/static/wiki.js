/* Wiki JavaScript (Story #282) */
(function () {
    "use strict";

    var repoAlias = document.body.dataset.repoAlias;

    // ------------------------------------------------------------------
    // Recently Viewed (AC11): last 10 articles per repo in localStorage
    // ------------------------------------------------------------------
    var STORAGE_KEY = repoAlias ? "wiki_recent_" + repoAlias : null;
    var MAX_RECENT = 10;

    function recordCurrentPage() {
        if (!STORAGE_KEY) return;
        var titleEl = document.querySelector(".wiki-content h1");
        var path = window.location.pathname;
        if (!titleEl || !path.startsWith("/wiki/")) return;

        var recent = [];
        try {
            recent = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
        } catch (e) {
            recent = [];
        }
        // Remove duplicate entry for this path
        recent = recent.filter(function (item) { return item.path !== path; });
        recent.unshift({ title: titleEl.textContent.trim(), path: path });
        recent = recent.slice(0, MAX_RECENT);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(recent));
    }

    function renderRecentlyViewed() {
        if (!STORAGE_KEY) return;
        var container = document.getElementById("recently-viewed-items");
        if (!container) return;

        var recent = [];
        try {
            recent = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
        } catch (e) {
            return;
        }

        recent.forEach(function (item) {
            var a = document.createElement("a");
            a.href = item.path;
            a.className = "sidebar-item";
            a.textContent = item.title;
            container.appendChild(a);
        });
    }

    // ------------------------------------------------------------------
    // Sidebar toggle (AC9)
    // ------------------------------------------------------------------
    function initSidebarToggle() {
        var toggle = document.getElementById("sidebar-toggle");
        var sidebar = document.getElementById("wiki-sidebar");
        if (!toggle || !sidebar) return;

        toggle.addEventListener("click", function () {
            sidebar.classList.toggle("collapsed");
        });
    }

    // ------------------------------------------------------------------
    // Auto-expand group containing the active article (AC3)
    // ------------------------------------------------------------------
    function autoExpandActiveGroup() {
        var active = document.querySelector(".sidebar-item.active");
        if (!active) return;

        var group = active.closest(".sidebar-group");
        if (group) group.classList.remove("collapsed");

        var category = active.closest(".sidebar-category");
        if (category) category.classList.remove("collapsed");
    }

    // ------------------------------------------------------------------
    // Theme toggle (dark/light mode)
    // ------------------------------------------------------------------
    var THEME_KEY = "wiki_theme";

    function initThemeToggle() {
        var toggle = document.getElementById("theme-toggle");
        if (!toggle) return;

        toggle.addEventListener("click", function () {
            var html = document.documentElement;
            var current = html.getAttribute("data-theme") || "dark";
            var next = current === "dark" ? "light" : "dark";
            html.setAttribute("data-theme", next);
            localStorage.setItem(THEME_KEY, next);
        });
    }

    // ------------------------------------------------------------------
    // Initialise
    // ------------------------------------------------------------------
    recordCurrentPage();
    renderRecentlyViewed();
    initSidebarToggle();
    initThemeToggle();
    autoExpandActiveGroup();
}());
