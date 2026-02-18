/**
 * Repository Category Management JavaScript (Story #183)
 *
 * Provides client-side functionality for:
 * - Toggling between flat and grouped views
 * - Grouping repositories by category
 * - Collapsing/expanding category sections
 * - Saving category assignments
 * - Canceling category changes
 */

/**
 * Get localStorage key for grouped view preference based on table class.
 * Each page gets its own independent preference.
 * @returns {string} Storage key
 */
function _getGroupedStorageKey() {
    if (document.querySelector('.golden-repos-table')) return 'cidx-golden-repos-grouped';
    if (document.querySelector('.repos-table')) return 'cidx-activated-repos-grouped';
    return 'cidx-repos-grouped';
}

/**
 * Toggle between flat and grouped view
 */
function toggleGroupedView() {
    const table = document.querySelector('.golden-repos-table') || document.querySelector('.repos-table') || document.querySelector('table');
    if (!table) return;

    const btn = document.getElementById('toggle-grouped');
    const isGrouped = table.classList.toggle('grouped-view');
    btn.textContent = isGrouped ? 'Flat View' : 'Group by Category';

    // Persist preference to localStorage
    localStorage.setItem(_getGroupedStorageKey(), isGrouped ? '1' : '0');

    if (isGrouped) {
        groupRows(table);
    } else {
        ungroupRows(table);
    }
}

/**
 * Group table rows by category
 * @param {HTMLTableElement} table - The table element
 */
function groupRows(table) {
    const tbody = table.querySelector('tbody') || table;
    const rows = Array.from(tbody.querySelectorAll('tr[data-category-name]'));

    // Remove existing group headers
    tbody.querySelectorAll('.category-group-header').forEach(h => h.remove());

    // Group by category
    const groups = {};
    rows.forEach(row => {
        const cat = row.dataset.categoryName || 'Unassigned';
        const priority = parseInt(row.dataset.categoryPriority) || 999999;
        if (!groups[cat]) groups[cat] = { priority, rows: [] };
        groups[cat].rows.push(row);
    });

    // Sort: by priority, Unassigned last
    const sorted = Object.entries(groups).sort(([,a], [,b]) => a.priority - b.priority);

    // Rebuild tbody with headers
    const colCount = table.querySelectorAll('thead th, thead td').length || rows[0]?.children.length || 8;
    sorted.forEach(([name, group]) => {
        const header = document.createElement('tr');
        header.className = 'category-group-header';
        header.style.cursor = 'pointer';
        header.onclick = function() { collapseSection(this); };
        header.innerHTML = `<td colspan="${colCount}" style="background: var(--pico-primary-background); font-weight: bold; padding: 0.5rem 1rem;">${escapeHtml(name)} (${group.rows.length})</td>`;
        tbody.appendChild(header);
        group.rows.forEach(r => {
            tbody.appendChild(r);
            // Move the corresponding details row to stay adjacent (Story #218 fix)
            var alias = r.dataset.repoAlias;
            if (alias) {
                var detailsRow = document.getElementById('details-' + alias);
                if (detailsRow) tbody.appendChild(detailsRow);
            }
        });
    });
}

/**
 * Ungroup table rows (return to flat view)
 * @param {HTMLTableElement} table - The table element
 */
function ungroupRows(table) {
    const tbody = table.querySelector('tbody') || table;
    // Remove group headers
    tbody.querySelectorAll('.category-group-header').forEach(h => h.remove());
    // Show all rows
    tbody.querySelectorAll('tr[data-category-name]').forEach(r => r.style.display = '');
    // Sort alphabetically by alias
    const rows = Array.from(tbody.querySelectorAll('tr[data-category-name]'));
    rows.sort((a, b) => (a.dataset.repoAlias || '').localeCompare(b.dataset.repoAlias || ''));
    rows.forEach(r => {
        tbody.appendChild(r);
        var alias = r.dataset.repoAlias;
        if (alias) {
            var detailsRow = document.getElementById('details-' + alias);
            if (detailsRow) tbody.appendChild(detailsRow);
        }
    });
}

/**
 * Collapse/expand a category section
 * @param {HTMLElement} header - The category group header element
 */
function collapseSection(header) {
    let next = header.nextElementSibling;
    const isCollapsing = next && next.style.display !== 'none';
    while (next && !next.classList.contains('category-group-header')) {
        if (isCollapsing) {
            next.style.display = 'none';
        } else {
            // When expanding, keep details rows hidden unless explicitly opened
            if (next.classList.contains('details-row')) {
                next.style.display = 'none';
            } else {
                next.style.display = '';
            }
        }
        next = next.nextElementSibling;
    }
    // Toggle indicator
    const td = header.querySelector('td');
    if (td) {
        const text = td.textContent;
        if (isCollapsing && !text.includes('[+]')) {
            td.textContent = text.replace(/^/, '[+] ');
        } else {
            td.textContent = text.replace('[+] ', '');
        }
    }
}

/**
 * Show or hide OK/Cancel buttons based on whether the select value differs from original.
 * Called by the onchange handler on the category select element.
 * @param {HTMLSelectElement} select - The category select element
 */
function onCategoryChange(select) {
    const cell = select.closest('.category-cell');
    const okBtn = cell.querySelector('.category-ok');
    const cancelBtn = cell.querySelector('.category-cancel');
    const changed = select.value !== select.dataset.original;
    if (okBtn) okBtn.style.display = changed ? 'inline-block' : 'none';
    if (cancelBtn) cancelBtn.style.display = changed ? 'inline-block' : 'none';
}

/**
 * Save category assignment for a repository
 * @param {string} alias - Repository alias
 * @param {HTMLElement} button - The OK button element
 */
function saveCategory(alias, button) {
    const row = button.closest('tr');
    const select = row.querySelector('.category-select');
    const categoryId = select.value;
    const original = select.dataset.original;

    // No change - skip
    if (categoryId === original) return;

    // Get CSRF token from page
    const csrfInput = document.querySelector('input[name="csrf_token"]');
    const csrfToken = csrfInput ? csrfInput.value : '';

    const formData = new FormData();
    formData.append('category_id', categoryId);
    formData.append('csrf_token', csrfToken);

    fetch(`/admin/golden-repos/${encodeURIComponent(alias)}/category`, {
        method: 'POST',
        body: formData
    })
    .then(r => {
        if (!r.ok) throw new Error(`Server error: ${r.status}`);
        return r.text();
    })
    .then(html => {
        // Update data-original to new value
        select.dataset.original = categoryId;
        // Update data attributes on row
        const option = select.options[select.selectedIndex];
        row.dataset.categoryId = categoryId;
        row.dataset.categoryName = option.text;
        // Hide OK/Cancel buttons since value is now saved
        const cell = select.closest('.category-cell');
        const okBtn = cell.querySelector('.category-ok');
        const cancelBtn = cell.querySelector('.category-cancel');
        if (okBtn) okBtn.style.display = 'none';
        if (cancelBtn) cancelBtn.style.display = 'none';
        // Show brief success then restore button text
        button.textContent = 'Saved!';
        setTimeout(() => { button.textContent = 'OK'; }, 1500);
        // Re-group if grouped view is active
        const table = row.closest('table');
        if (table && table.classList.contains('grouped-view')) {
            groupRows(table);
        }
    })
    .catch(err => {
        button.textContent = 'Error';
        setTimeout(() => { button.textContent = 'OK'; }, 2000);
        console.error('Save failed:', err);
    });
}

/**
 * Cancel category change and revert to original value
 * @param {HTMLElement} button - The Cancel button element
 */
function cancelCategory(button) {
    const row = button.closest('tr');
    const select = row.querySelector('.category-select');
    select.value = select.dataset.original;
    // Hide OK/Cancel buttons since value is now reverted
    const cell = select.closest('.category-cell');
    const okBtn = cell.querySelector('.category-ok');
    const cancelBtn = cell.querySelector('.category-cancel');
    if (okBtn) okBtn.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
}

/**
 * Escape HTML to prevent XSS
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Apply stored grouped view preference on page load or HTMX refresh.
 * Reads from localStorage and applies grouped view if previously selected.
 */
function applyStoredGroupedView() {
    const key = _getGroupedStorageKey();
    if (localStorage.getItem(key) === '1') {
        const table = document.querySelector('.golden-repos-table') || document.querySelector('.repos-table') || document.querySelector('table');
        const btn = document.getElementById('toggle-grouped');
        if (table && btn) {
            table.classList.add('grouped-view');
            btn.textContent = 'Flat View';
            groupRows(table);
        }
    }
}
