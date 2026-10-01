/**
 * Audit Logs page behaviour.
 *
 * Every value this script needs arrives through a data-* attribute or the
 * JSON config block (#audit-logs-config); nothing is interpolated into
 * script text and no row data is ever written as markup: result sections
 * are server-rendered (autoescaped) and fetched with htmx, so the shared
 * elevation interceptor (elevation_interceptor.js) can open the TOTP modal
 * and retry.
 */
(function () {
    'use strict';

    var LIST_ID = 'audit-list-section';

    function config() {
        var el = document.getElementById('audit-logs-config');
        return el ? JSON.parse(el.textContent) : null;
    }

    function form() {
        return document.getElementById('audit-filter-form');
    }

    function formParams() {
        var params = new URLSearchParams();
        new FormData(form()).forEach(function (value, key) {
            if (typeof value === 'string' && value.trim() !== '') {
                params.append(key, value.trim());
            }
        });
        return params;
    }

    function syncCustomRange() {
        var custom = document.getElementById('audit-window').value === 'custom';
        document.querySelectorAll('.audit-custom-range').forEach(function (el) {
            el.hidden = !custom;
        });
    }

    function syncViewButtons(view) {
        document.querySelectorAll('.audit-view-btn').forEach(function (btn) {
            var active = btn.dataset.view === view;
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.classList.toggle('outline', !active);
        });
    }

    function refresh(cfg) {
        var params = formParams();
        if (params.get('window') !== 'custom') {
            params.delete('date_from');
            params.delete('date_to');
        }
        var endpoint = params.get('view') === 'auth_activity' ? cfg.aggregate_url : cfg.rows_url;
        htmx.ajax('GET', endpoint + '?' + params.toString(), {
            target: '#' + LIST_ID,
            swap: 'innerHTML',
        });
    }

    function clearFilters() {
        var f = form();
        f.querySelectorAll('input[type="text"], input[type="datetime-local"]').forEach(function (el) {
            el.value = '';
        });
        f.querySelectorAll('select').forEach(function (el) {
            el.selectedIndex = 0;
        });
    }

    function setWindow(value) {
        document.getElementById('audit-window').value = value;
        syncCustomRange();
    }

    function showCorrelation(cfg, correlationId) {
        clearFilters();
        document.getElementById('audit-view').value = 'all';
        syncViewButtons('all');
        document.getElementById('audit-correlation').value = correlationId;
        setWindow('all');
        refresh(cfg);
    }

    function currentExportQuery() {
        var results = document.querySelector('#' + LIST_ID + ' .audit-results');
        return results ? results.dataset.exportQuery : null;
    }

    function setExportStatus(text) {
        var status = document.getElementById('audit-export-status');
        if (status) status.textContent = text;
    }

    function exportResults(cfg) {
        var query = currentExportQuery();
        if (query === null) {
            setExportStatus('Nothing to export yet.');
            return;
        }
        var format = document.getElementById('audit-export-format').value;
        var url = cfg.export_url + '?' + query + '&format=' + encodeURIComponent(format);
        setExportStatus('Preparing export...');
        fetch(url, { credentials: 'same-origin' }).then(function (response) {
            if (!response.ok) {
                setExportStatus('Export failed (HTTP ' + response.status + ').');
                return null;
            }
            var disposition = response.headers.get('Content-Disposition') || '';
            var match = /filename="([^"]+)"/.exec(disposition);
            var filename = match ? match[1] : 'audit_logs.' + format;
            return response.blob().then(function (blob) {
                var link = document.createElement('a');
                link.href = URL.createObjectURL(blob);
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                link.remove();
                URL.revokeObjectURL(link.href);
                setExportStatus('Exported ' + filename + '.');
            });
        }).catch(function (err) {
            if (!err || err.message !== 'elevation_cancelled') {
                setExportStatus('Export failed.');
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var cfg = config();
        if (!cfg || !form()) return;
        syncCustomRange();

        form().addEventListener('submit', function (evt) {
            evt.preventDefault();
            refresh(cfg);
        });
        document.getElementById('audit-window').addEventListener('change', syncCustomRange);
        document.getElementById('audit-clear-btn').addEventListener('click', function () {
            clearFilters();
            var view = document.getElementById('audit-view').value;
            setWindow(cfg.default_windows[view] || '7d');
            refresh(cfg);
        });
        document.querySelectorAll('.audit-view-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                document.getElementById('audit-view').value = btn.dataset.view;
                syncViewButtons(btn.dataset.view);
                setWindow(cfg.default_windows[btn.dataset.view] || '7d');
                refresh(cfg);
            });
        });
        document.getElementById('audit-export-btn').addEventListener('click', function () {
            exportResults(cfg);
        });
        // Delegated: result rows are replaced by htmx on every query.
        document.getElementById(LIST_ID).addEventListener('click', function (evt) {
            var link = evt.target.closest('.audit-correlation-link');
            if (!link) return;
            evt.preventDefault();
            showCorrelation(cfg, link.dataset.correlationId);
        });
    });

    // A refused filter comes back as HTTP 400 with an explanation: show it
    // instead of silently keeping the previous results.
    document.addEventListener('htmx:beforeSwap', function (evt) {
        if (evt.detail.xhr.status === 400 && evt.detail.target &&
                evt.detail.target.id === LIST_ID) {
            evt.detail.shouldSwap = true;
            evt.detail.isError = false;
        }
    });
})();
