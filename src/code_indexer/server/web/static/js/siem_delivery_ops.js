/**
 * SIEM Delivery operator panels (config page, SIEM section).
 *
 * htmx does not swap non-2xx responses by default; a refused action (400,
 * 403 bad CSRF, 409, 413, 503) comes back as an HTML result fragment that
 * must be shown verbatim.  401 and the JSON 403 elevation bodies are left to
 * the global elevation interceptor (elevation_interceptor.js).
 */
(function () {
    document.addEventListener('htmx:beforeSwap', function (evt) {
        var xhr = evt.detail.xhr;
        var target = evt.detail.target;
        if (!target || !target.id || target.id.indexOf('siem-ops-') !== 0) return;
        var html = (xhr.getResponseHeader('Content-Type') || '').indexOf('text/html') === 0;
        if (xhr.status >= 400 && xhr.status !== 401 && html) {
            evt.detail.shouldSwap = true;
            evt.detail.isError = false;
        }
    });

    // "Open destination by key": the dialog route takes the key in its path.
    document.addEventListener('submit', function (evt) {
        var form = evt.target;
        if (!form || form.id !== 'siem-ops-open-destination') return;
        evt.preventDefault();
        var key = (form.elements.destination_key.value || '').trim();
        if (!key) return;
        htmx.ajax('GET', '/admin/siem-delivery/partials/destinations/' +
            encodeURIComponent(key) + '/abandon', '#siem-ops-dialog');
    });
})();
