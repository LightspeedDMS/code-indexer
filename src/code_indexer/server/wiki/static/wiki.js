/* Wiki JavaScript (Stories #282, #288) */
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
    // sessionStorage helpers for TOC state (AC4)
    // Key format: wiki_toc_state_{repoAlias}_{sectionId}
    // ------------------------------------------------------------------
    function _tocStorageKey(sectionId) {
        return "wiki_toc_state_" + (repoAlias || "") + "_" + sectionId;
    }

    function _saveTocState(sectionId, expanded) {
        try {
            sessionStorage.setItem(_tocStorageKey(sectionId), expanded ? "1" : "0");
        } catch (e) {
            // sessionStorage unavailable — graceful degradation
        }
    }

    function _loadTocState(sectionId) {
        try {
            return sessionStorage.getItem(_tocStorageKey(sectionId));
        } catch (e) {
            return null;
        }
    }

    // ------------------------------------------------------------------
    // TOC initialization — Story #288 (AC1, AC2, AC3, AC4)
    // ------------------------------------------------------------------
    function initTOC() {
        var sidebar = document.getElementById("wiki-sidebar");
        if (!sidebar) return;

        // Step 1: All .sidebar-category elements start collapsed (already in HTML via
        // the 'collapsed' class on the template, but we enforce it here in JS too
        // to handle any dynamic content added after page load).
        var categories = sidebar.querySelectorAll(".sidebar-category");
        categories.forEach(function (cat) {
            cat.classList.add("collapsed");
        });

        // Step 2: Restore persisted state from sessionStorage (AC4)
        categories.forEach(function (cat) {
            var sectionId = cat.dataset.sectionId;
            if (!sectionId) return;
            var saved = _loadTocState(sectionId);
            if (saved === "1") {
                cat.classList.remove("collapsed");
            }
        });

        // Step 3: Auto-expand the section(s) containing the active article (AC2).
        // Auto-expand overrides saved collapsed state.
        var active = sidebar.querySelector(".sidebar-item.active");
        if (active) {
            // Expand the immediate .sidebar-category ancestor
            var parentCategory = active.closest(".sidebar-category");
            if (parentCategory) {
                parentCategory.classList.remove("collapsed");
                // Save the expanded state so it survives navigation within session
                var sectionId = parentCategory.dataset.sectionId;
                if (sectionId) _saveTocState(sectionId, true);
            }

            // Expand the .sidebar-group ancestor (group-level collapse, AC2)
            var parentGroup = active.closest(".sidebar-group");
            if (parentGroup) {
                parentGroup.classList.remove("collapsed");
            }
        }

        // Step 4: Wire click handlers for .sidebar-category-header (AC3)
        sidebar.querySelectorAll(".sidebar-category-header").forEach(function (header) {
            header.addEventListener("click", function () {
                var cat = header.closest(".sidebar-category");
                if (!cat) return;
                cat.classList.toggle("collapsed");
                var sectionId = cat.dataset.sectionId;
                if (sectionId) {
                    _saveTocState(sectionId, !cat.classList.contains("collapsed"));
                }
            });
        });

        // Step 5: Wire click handlers for .sidebar-group-header (group-level toggle)
        sidebar.querySelectorAll(".sidebar-group-header").forEach(function (header) {
            // Skip the "Recently Viewed" static header (it has no parent .sidebar-group)
            var group = header.closest(".sidebar-group");
            if (!group) return;
            header.addEventListener("click", function () {
                group.classList.toggle("collapsed");
            });
        });
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
    // Search (Story #290): AC1, AC3, AC4, AC5, AC6
    // ------------------------------------------------------------------
    function initSearch() {
        var input = document.getElementById("wiki-search-input");
        var modeSelect = document.getElementById("wiki-search-mode");
        var clearBtn = document.getElementById("wiki-search-clear");
        var statusEl = document.getElementById("wiki-search-status");
        if (!input || !modeSelect || !clearBtn || !statusEl) return;

        var debounceTimer = null;
        var currentController = null;

        function filterTOCToResults(results) {
            var matchPaths = {};
            results.forEach(function (r) { matchPaths[r.path] = true; });

            var sidebar = document.getElementById("wiki-sidebar");
            if (!sidebar) return;

            // Show/hide sidebar-item elements (AC3)
            var items = sidebar.querySelectorAll(".sidebar-item[data-path]");
            items.forEach(function (item) {
                var path = item.dataset.path;
                if (matchPaths[path]) {
                    item.style.display = "";
                    item.removeAttribute("data-search-hidden");
                } else {
                    item.style.display = "none";
                    item.setAttribute("data-search-hidden", "1");
                }
            });

            // Show/hide .sidebar-category based on whether they contain visible items (AC3)
            var categories = sidebar.querySelectorAll(".sidebar-category");
            categories.forEach(function (cat) {
                var visibleItems = cat.querySelectorAll(".sidebar-item:not([data-search-hidden])");
                if (visibleItems.length > 0) {
                    cat.style.display = "";
                    cat.classList.remove("collapsed");  // expand matching categories (AC3)
                    cat.setAttribute("data-search-expanded", "1");
                } else {
                    cat.style.display = "none";
                }
            });

            // Show/hide .sidebar-group based on whether they contain visible categories (AC3)
            var groups = sidebar.querySelectorAll(".sidebar-group");
            groups.forEach(function (group) {
                var visibleCats = group.querySelectorAll(".sidebar-category:not([style*='display: none'])");
                if (visibleCats.length > 0) {
                    group.style.display = "";
                    group.classList.remove("collapsed");  // expand matching groups (AC3)
                } else {
                    group.style.display = "none";
                }
            });

            // Update status text
            statusEl.style.display = "";
            if (results.length === 0) {
                statusEl.textContent = "No results found";
            } else {
                statusEl.textContent = "Showing " + results.length + " result" + (results.length === 1 ? "" : "s");
            }

            // Render results list below status
            var resultsList = document.getElementById("wiki-search-results");
            if (!resultsList) {
                resultsList = document.createElement("div");
                resultsList.id = "wiki-search-results";
                resultsList.className = "wiki-search-results";
                statusEl.parentNode.insertBefore(resultsList, statusEl.nextSibling);
            }
            resultsList.innerHTML = "";
            resultsList.style.display = "";

            results.forEach(function (r) {
                var a = document.createElement("a");
                a.href = "/wiki/" + repoAlias + "/" + r.path;
                a.className = "search-result-item";
                a.textContent = r.title;
                if (r.score !== undefined) {
                    var scoreSpan = document.createElement("span");
                    scoreSpan.className = "search-result-score";
                    scoreSpan.textContent = Math.round(r.score * 100) + "%";
                    a.appendChild(scoreSpan);
                }
                resultsList.appendChild(a);
            });
        }

        function restoreFullTOC() {
            var sidebar = document.getElementById("wiki-sidebar");
            if (!sidebar) return;

            // Remove all search-applied visibility overrides (AC5)
            var items = sidebar.querySelectorAll(".sidebar-item[data-search-hidden]");
            items.forEach(function (item) {
                item.style.display = "";
                item.removeAttribute("data-search-hidden");
            });

            var categories = sidebar.querySelectorAll(".sidebar-category");
            categories.forEach(function (cat) {
                cat.style.display = "";
                if (cat.hasAttribute("data-search-expanded")) {
                    cat.removeAttribute("data-search-expanded");
                    // Restore saved state (or re-collapse)
                    var sectionId = cat.dataset.sectionId;
                    if (sectionId) {
                        var saved = _loadTocState(sectionId);
                        if (saved !== "1") {
                            cat.classList.add("collapsed");
                        }
                    } else {
                        cat.classList.add("collapsed");
                    }
                }
            });

            var groups = sidebar.querySelectorAll(".sidebar-group");
            groups.forEach(function (group) {
                group.style.display = "";
            });

            // Clear search results list
            var resultsList = document.getElementById("wiki-search-results");
            if (resultsList) {
                resultsList.innerHTML = "";
                resultsList.style.display = "none";
            }

            statusEl.style.display = "none";
            statusEl.textContent = "";

            // Re-run initTOC to restore proper active-item expansion (AC5)
            initTOC();
        }

        function doSearch(queryText) {
            // Cancel any in-flight request (AC6)
            if (currentController) {
                currentController.abort();
                currentController = null;
            }

            if (!queryText || queryText.length < 2) {
                restoreFullTOC();
                clearBtn.style.display = "none";
                return;
            }

            clearBtn.style.display = "";

            // Show loading state
            statusEl.style.display = "";
            statusEl.textContent = "Searching...";

            currentController = new AbortController();
            var mode = modeSelect.value || "semantic";
            var url = "/wiki/" + repoAlias + "/_search?q=" + encodeURIComponent(queryText) + "&mode=" + encodeURIComponent(mode);

            fetch(url, { signal: currentController.signal })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    currentController = null;
                    if (!Array.isArray(data)) {
                        // Error response from server — show actual message
                        statusEl.style.display = "";
                        statusEl.textContent = (data && data.error) ? data.error : "Search unavailable";
                        return;
                    }
                    filterTOCToResults(data);
                })
                .catch(function (err) {
                    if (err.name === "AbortError") return;  // Cancelled — ignore
                    currentController = null;
                    statusEl.textContent = "Search unavailable";
                });
        }

        // AC6: Debounced input with 3000ms delay, min 2 chars
        input.addEventListener("input", function () {
            clearTimeout(debounceTimer);
            var val = input.value.trim();
            debounceTimer = setTimeout(function () {
                doSearch(val);
            }, 3000);
        });

        // Enter key triggers immediate search without waiting for debounce
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") {
                e.preventDefault();
                clearTimeout(debounceTimer);
                var val = input.value.trim();
                doSearch(val);
            }
        });

        // Mode change re-triggers search
        modeSelect.addEventListener("change", function () {
            clearTimeout(debounceTimer);
            var val = input.value.trim();
            if (val.length >= 2) {
                doSearch(val);
            }
        });

        // AC5: Clear button restores full TOC
        clearBtn.addEventListener("click", function () {
            input.value = "";
            clearBtn.style.display = "none";
            clearTimeout(debounceTimer);
            if (currentController) {
                currentController.abort();
                currentController = null;
            }
            restoreFullTOC();
        });
    }

    // ------------------------------------------------------------------
    // Sidebar resize via drag handle (Story #294, AC2-AC5)
    // ------------------------------------------------------------------
    function initSidebarResize() {
        var sidebar = document.getElementById("wiki-sidebar");
        var handle = document.getElementById("wiki-sidebar-handle");
        if (!sidebar || !handle) return;

        var SIDEBAR_MIN = 180;
        var SIDEBAR_MAX_RATIO = 0.5;
        var SIDEBAR_DEFAULT = 280;
        var WIDTH_STORAGE_KEY = "wiki_sidebar_width";

        function clampWidth(w) {
            var maxW = Math.floor(window.innerWidth * SIDEBAR_MAX_RATIO);
            return Math.max(SIDEBAR_MIN, Math.min(w, maxW));
        }

        // AC4: Restore saved width on page load
        var savedWidth = null;
        try {
            savedWidth = localStorage.getItem(WIDTH_STORAGE_KEY);
        } catch (e) {}

        if (savedWidth) {
            var parsed = parseInt(savedWidth, 10);
            if (!isNaN(parsed)) {
                sidebar.style.width = clampWidth(parsed) + "px";
            }
        }
        // AC5: If no saved width, CSS default of 280px applies — no localStorage write on init

        var isResizing = false;

        handle.addEventListener("mousedown", function (e) {
            isResizing = true;
            sidebar.classList.add("resizing");
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
            e.preventDefault();
        });

        document.addEventListener("mousemove", function (e) {
            if (!isResizing) return;
            requestAnimationFrame(function () {
                sidebar.style.width = clampWidth(e.clientX) + "px";
            });
        });

        document.addEventListener("mouseup", function () {
            if (!isResizing) return;
            isResizing = false;
            sidebar.classList.remove("resizing");
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            // AC4: Save width only on drag end, not during drag
            var currentWidth = parseInt(sidebar.style.width, 10);
            if (!isNaN(currentWidth)) {
                try {
                    localStorage.setItem(WIDTH_STORAGE_KEY, currentWidth.toString());
                } catch (e) {}
            }
        });

        // AC3: Re-clamp on window resize to enforce max constraint
        window.addEventListener("resize", function () {
            var currentWidth = parseInt(sidebar.style.width, 10) || SIDEBAR_DEFAULT;
            var clamped = clampWidth(currentWidth);
            if (clamped !== currentWidth) {
                sidebar.style.width = clamped + "px";
            }
        });
    }

    // ------------------------------------------------------------------
    // Client-side navigation (SPA-like) to eliminate TOC flicker
    // ------------------------------------------------------------------
    function initClientNavigation() {
        var sidebar = document.getElementById("wiki-sidebar");
        if (!sidebar) return;

        function swapContent(url) {
            fetch(url)
                .then(function (resp) {
                    if (!resp.ok) {
                        window.location.href = url;
                        return null;
                    }
                    return resp.text();
                })
                .then(function (html) {
                    if (!html) return;

                    var parser = new DOMParser();
                    var doc = parser.parseFromString(html, "text/html");

                    var newMain = doc.querySelector(".wiki-main");
                    var currentMain = document.querySelector(".wiki-main");
                    if (!newMain || !currentMain) {
                        window.location.href = url;
                        return;
                    }

                    // Swap content
                    currentMain.innerHTML = newMain.innerHTML;

                    // Update page title
                    var newTitle = doc.querySelector("title");
                    if (newTitle) {
                        document.title = newTitle.textContent;
                    }

                    // Update URL
                    history.pushState({ path: url }, "", url);

                    // Extract article path from URL for data attribute update
                    var articlePath = url.replace(/^\/wiki\/[^/]+\//, "");
                    sidebar.dataset.currentPath = articlePath;

                    // Record and render recently viewed (reads from DOM + location)
                    recordCurrentPage();
                    var container = document.getElementById("recently-viewed-items");
                    if (container) {
                        container.innerHTML = "";
                        renderRecentlyViewed();
                    }
                })
                .catch(function () {
                    window.location.href = url;
                });
        }

        sidebar.addEventListener("click", function (e) {
            // Find the closest .sidebar-item anchor
            var target = e.target.closest("a.sidebar-item");
            if (!target) return;

            var url = target.getAttribute("href");
            if (!url) return;

            // Only intercept same-origin /wiki/ links
            if (!url.startsWith("/wiki/")) return;

            // Let modifier-clicks open in new tab normally
            if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;

            e.preventDefault();

            // Update active marker immediately — no flicker
            var prevActive = sidebar.querySelector(".sidebar-item.active");
            if (prevActive) {
                prevActive.classList.remove("active");
            }
            target.classList.add("active");

            // Auto-expand parent .sidebar-category if collapsed
            var parentCategory = target.closest(".sidebar-category");
            if (parentCategory && parentCategory.classList.contains("collapsed")) {
                parentCategory.classList.remove("collapsed");
                var sectionId = parentCategory.dataset.sectionId;
                if (sectionId) {
                    _saveTocState(sectionId, true);
                }
            }

            // Auto-expand parent .sidebar-group if collapsed
            var parentGroup = target.closest(".sidebar-group");
            if (parentGroup && parentGroup.classList.contains("collapsed")) {
                parentGroup.classList.remove("collapsed");
            }

            swapContent(url);
        });

        // Handle browser back/forward
        window.addEventListener("popstate", function (e) {
            var url = (e.state && e.state.path) ? e.state.path : window.location.pathname;

            // Update active marker in sidebar
            var newActive = sidebar.querySelector("a.sidebar-item[href='" + url + "']");
            var prevActive = sidebar.querySelector(".sidebar-item.active");
            if (prevActive) prevActive.classList.remove("active");
            if (newActive) {
                newActive.classList.add("active");
                // Expand parent category if collapsed
                var parentCategory = newActive.closest(".sidebar-category");
                if (parentCategory && parentCategory.classList.contains("collapsed")) {
                    parentCategory.classList.remove("collapsed");
                    var sectionId = parentCategory.dataset.sectionId;
                    if (sectionId) _saveTocState(sectionId, true);
                }
                // Expand parent group if collapsed
                var parentGroup = newActive.closest(".sidebar-group");
                if (parentGroup && parentGroup.classList.contains("collapsed")) {
                    parentGroup.classList.remove("collapsed");
                }
            }

            fetch(url)
                .then(function (resp) {
                    if (!resp.ok) { window.location.reload(); return null; }
                    return resp.text();
                })
                .then(function (html) {
                    if (!html) return;
                    var parser = new DOMParser();
                    var doc = parser.parseFromString(html, "text/html");
                    var newMain = doc.querySelector(".wiki-main");
                    var currentMain = document.querySelector(".wiki-main");
                    if (!newMain || !currentMain) { window.location.reload(); return; }
                    currentMain.innerHTML = newMain.innerHTML;
                    var newTitle = doc.querySelector("title");
                    if (newTitle) document.title = newTitle.textContent;
                    recordCurrentPage();
                })
                .catch(function () {
                    window.location.reload();
                });
        });
    }

    // ------------------------------------------------------------------
    // Initialise
    // ------------------------------------------------------------------
    recordCurrentPage();
    renderRecentlyViewed();
    initSidebarToggle();
    initSidebarResize();
    initThemeToggle();
    initTOC();
    initSearch();
    initClientNavigation();
}());
