//^ invariant filter hides rows via .repo-filter-hidden, a collapsed category via inline display:none; bulk acts only on rows hidden by neither

var _repoAccessFilterTimer = null;
var REPO_ACCESS_FILTER_DEBOUNCE_MS = 150;

function _repoAccessTable() {
    return document.querySelector('.repo-access-table');
}

function _repoAccessRows(table) {
    return Array.from(table.querySelectorAll('tbody tr[data-repo-alias]'));
}

function _setRepoAccessStatus(text) {
    var status = document.getElementById('repo-access-status');
    if (status) status.textContent = text;
}

function onRepoAccessFilterInput() {
    clearTimeout(_repoAccessFilterTimer);
    _repoAccessFilterTimer = setTimeout(applyRepoAccessFilter, REPO_ACCESS_FILTER_DEBOUNCE_MS);
}

function _rowMatches(row, term) {
    if (term === '') return true;
    var alias = (row.dataset.repoAlias || '').toLowerCase();
    var url = (row.dataset.repoUrl || '').toLowerCase();
    return alias.includes(term) || url.includes(term);
}

function _hideEmptyCategoryHeaders(table) {
    table.querySelectorAll('tr.category-group-header').forEach(function (header) {
        var next = header.nextElementSibling;
        var anyMatch = false;
        while (next && !next.classList.contains('category-group-header')) {
            if (next.dataset.repoAlias !== undefined && !next.classList.contains('repo-filter-hidden')) {
                anyMatch = true;
                break;
            }
            next = next.nextElementSibling;
        }
        header.classList.toggle('repo-filter-hidden', !anyMatch);
    });
}

function applyRepoAccessFilter() {
    var table = _repoAccessTable();
    if (!table) return;
    var box = document.getElementById('repo-access-filter');
    var term = box ? box.value.trim().toLowerCase() : '';
    var rows = _repoAccessRows(table);
    var shown = 0;
    rows.forEach(function (row) {
        var match = _rowMatches(row, term);
        row.classList.toggle('repo-filter-hidden', !match);
        if (match) shown++;
    });
    _hideEmptyCategoryHeaders(table);
    var count = document.getElementById('repo-access-count');
    if (count) count.textContent = 'Showing ' + shown + ' of ' + rows.length;
}

function repoAccessVisibleRows() {
    var table = _repoAccessTable();
    if (!table) return [];
    return _repoAccessRows(table).filter(function (row) {
        return !row.classList.contains('repo-filter-hidden')
            && row.style.display !== 'none'
            && row.dataset.repoAlias !== 'cidx-meta';
    });
}

function _setAccessCell(button, state) {
    var granted = state === 'granted';
    button.dataset.state = state;
    button.textContent = granted ? 'Yes' : 'No';
    button.className = granted ? 'access-btn access-granted' : 'access-btn access-denied';
    button.title = granted ? 'Click to revoke access' : 'Click to grant access';
}

function _bulkCandidates(groupId, targetState) {
    var cells = [];
    repoAccessVisibleRows().forEach(function (row) {
        var cell = row.querySelector('.access-btn[data-group="' + groupId + '"]');
        if (cell && cell.dataset.state !== targetState) cells.push(cell);
    });
    return cells;
}

function _bulkQuestion(action, count, groupName) {
    return action === 'grant'
        ? 'Grant ' + count + ' repositories to ' + groupName + '?'
        : 'Revoke ' + count + ' repositories from ' + groupName + '?';
}

async function _postBulkChange(groupId, action, names) {
    var csrfToken = document.getElementById('csrf-data').dataset.csrfToken;
    var response = await fetch('/admin/groups/repo-access/bulk', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': csrfToken
        },
        body: JSON.stringify({ group_id: groupId, action: action, repo_names: names })
    });
    var contentType = response.headers.get('content-type') || '';
    var data = contentType.includes('application/json') ? await response.json() : {};
    if (!response.ok) throw new Error(data.error || ('Server error (' + response.status + ')'));
    return data;
}

async function bulkToggle(control) {
    var groupId = parseInt(control.dataset.group, 10);
    var groupName = control.dataset.groupName;
    var action = control.dataset.action;
    if (isNaN(groupId) || (action !== 'grant' && action !== 'revoke')) return;
    var targetState = action === 'grant' ? 'granted' : 'denied';
    var toChange = _bulkCandidates(groupId, targetState);
    if (toChange.length === 0) {
        _setRepoAccessStatus('Nothing to change');
        return;
    }
    if (!window.confirm(_bulkQuestion(action, toChange.length, groupName))) return;

    var controls = document.querySelectorAll('.bulk-btn[data-group="' + groupId + '"]');
    controls.forEach(function (b) { b.disabled = true; });
    _setRepoAccessStatus('');
    try {
        var names = toChange.map(function (cell) { return cell.dataset.repo; });
        var data = await _postBulkChange(groupId, action, names);
        //^ why unchanged = already in target state (concurrent change) => show the truth
        var inTarget = new Set((data.changed || []).concat(data.unchanged || []));
        toChange.forEach(function (cell) {
            if (inTarget.has(cell.dataset.repo)) _setAccessCell(cell, targetState);
        });
        var rejected = data.rejected || [];
        if (rejected.length > 0) {
            var reasons = rejected.map(function (r) { return r.repo_name + ' (' + r.reason + ')'; });
            _setRepoAccessStatus(rejected.length + ' not changed: ' + reasons.join(', '));
        }
    } catch (err) {
        _setRepoAccessStatus('Bulk change failed: ' + err.message);
        console.error('Bulk access change failed:', err);
    } finally {
        controls.forEach(function (b) { b.disabled = false; });
    }
}
