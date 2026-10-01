/**
 * TOTP Step-Up Elevation Interceptor (Story #923 AC7, Bug #955).
 *
 * Shared by every page that extends base.html (admin) or user_base.html
 * (self-service): transparently intercepts a 403 `elevation_required` or
 * `totp_setup_required` response from fetch()/htmx/plain-HTML-form
 * requests, opens the inline TOTP modal (or redirects to TOTP setup), and
 * retries the original request after elevation so callers never see the
 * raw 403.
 *
 * The including page sets `window._cidxTotpSetupFallbackUrl` to its own
 * role-appropriate TOTP setup page BEFORE loading this script (see
 * base.html / user_base.html); that value is used only as a fallback when
 * the server response omits `setup_url`, which it always provides today.
 */
window._cidxElevationPending = null;
window._cidxRedirecting = false;

function _cidxTotpSetupFallback() {
    return window._cidxTotpSetupFallbackUrl || '/admin/mfa/setup';
}

// Global fetch() interceptor: transparently intercepts 403 elevation_required on
// all fetch() calls, opens the TOTP modal, and retries the original request after
// elevation so callers never see the 403. Fixes fetch()-based endpoints that are
// not intercepted by htmx:beforeSwap (e.g. change-branch, restart, auto-discovery).
(function() {
    var _origFetch = window.fetch;
    window._cidxOrigFetch = _origFetch;
    window.fetch = function() {
        var args = arguments;
        return _origFetch.apply(this, args).then(function(response) {
            if (response.status !== 403) return response;
            return response.clone().json().then(function(body) {
                var err = (body && body.detail && body.detail.error) || (body && body.error);
                if (err === 'totp_setup_required') {
                    if (!window._cidxRedirecting) {
                        window._cidxRedirecting = true;
                        window.location.replace((body.detail && body.detail.setup_url) || _cidxTotpSetupFallback());
                        setTimeout(function() { window._cidxRedirecting = false; }, 3000);
                    }
                    return response;
                }
                if (err === 'elevation_required') {
                    return new Promise(function(resolve, reject) {
                        window._cidxElevationPending = {
                            callback: function() {
                                _origFetch.apply(window, args).then(resolve).catch(reject);
                            },
                            _fetchReject: reject,
                        };
                        _cidxOpenElevationModal();
                    });
                }
                return response;
            }).catch(function() { return response; });
        });
    };
})();

// Global plain-HTML form interceptor: routes same-origin POST forms through fetch()
// so the elevation 403 interceptor above can open the modal instead of raw JSON.
// Uses bubble phase so that onsubmit attribute handlers (e.g. lpSync) run first.
(function () {
    function _formPath(form) {
        try { return new URL(form.action, window.location.href).pathname; }
        catch (e) { return form.action || ''; }
    }
    function _shouldIntercept(form) {
        if (!form || form.method.toLowerCase() !== 'post') return false;
        if (_formPath(form).indexOf(window._cidxFormInterceptPrefix || '/admin/') !== 0) return false;
        if (form.hasAttribute('hx-post') || form.hasAttribute('hx-put') ||
            form.hasAttribute('hx-delete') || form.hasAttribute('hx-patch')) return false;
        if (form.hasAttribute('data-no-ajax')) return false;
        return true;
    }
    function _setBusy(form, busy) {
        form.querySelectorAll('[type="submit"]').forEach(function (b) {
            b.disabled = busy;
            if (busy) { b.setAttribute('aria-busy', 'true'); }
            else { b.removeAttribute('aria-busy'); }
        });
    }
    function _doSubmit(form) {
        _setBusy(form, true);
        fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            credentials: 'same-origin',
        }).then(function (resp) {
            if (window._cidxRedirecting) return;
            return resp.text().then(function (html) {
                _setBusy(form, false);
                if (resp.redirected) {
                    window.location.href = resp.url;
                } else {
                    document.open();
                    document.write(html);
                    document.close();
                    // document.write does NOT reliably re-fire HTMX's
                    // DOMContentLoaded auto-init, so hx-trigger="load"
                    // elements on the new DOM never fire. Poll briefly
                    // for the new document's htmx to become available,
                    // then explicitly process the new body.
                    var _htmxInitTries = 0;
                    function _processNewDoc() {
                        if (window.htmx && typeof window.htmx.process === 'function') {
                            try { window.htmx.process(document.body); } catch (e) {}
                            return;
                        }
                        if (_htmxInitTries++ < 40) {
                            setTimeout(_processNewDoc, 50);
                        }
                    }
                    _processNewDoc();
                }
            });
        }).catch(function (err) {
            _setBusy(form, false);
            if (!err || err.message !== 'elevation_cancelled') {
                window.location.reload();
            }
        });
    }
    // Bubble phase: inline onsubmit fires first.
    document.addEventListener('submit', function (evt) {
        if (evt.defaultPrevented) return;
        if (!_shouldIntercept(evt.target)) return;
        evt.preventDefault();
        _doSubmit(evt.target);
    }, false);
    // Programmatic .submit() override (no submit event fires for these).
    var _origSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () {
        if (_shouldIntercept(this)) { _doSubmit(this); return; }
        _origSubmit.apply(this, arguments);
    };
})();

document.addEventListener('htmx:beforeSwap', function(evt) {
    var xhr = evt.detail.xhr;

    // Belt-and-suspenders: enforce hx-swap="none" for elements that declare it.
    // HTMX is not honoring hx-swap="none" reliably, causing JSON responses
    // to leak into button innerHTML on trigger buttons.
    var srcElt = evt.detail.elt || (evt.detail.requestConfig && evt.detail.requestConfig.elt);
    if (srcElt && srcElt.getAttribute && srcElt.getAttribute('hx-swap') === 'none') {
        evt.detail.shouldSwap = false;
    }

    if (xhr.status === 403) {
        try {
            var body = JSON.parse(xhr.responseText);
            var err = (body && body.detail && body.detail.error) || (body && body.error);
            if (err === 'totp_setup_required') {
                evt.detail.shouldSwap = false;
                if (!window._cidxRedirecting) {
                    window._cidxRedirecting = true;
                    window.location.replace((body.detail.setup_url) || _cidxTotpSetupFallback());
                    setTimeout(function() { window._cidxRedirecting = false; }, 3000);
                }
                return;
            }
            if (err === 'elevation_required') {
                evt.detail.shouldSwap = false;
                window._cidxElevationPending = {
                    verb: evt.detail.requestConfig && evt.detail.requestConfig.verb,
                    path: evt.detail.requestConfig && evt.detail.requestConfig.path,
                    target: evt.detail.target,
                    parameters: evt.detail.requestConfig && evt.detail.requestConfig.parameters,
                    sourceElt: evt.detail.requestConfig && evt.detail.requestConfig.elt,
                };
                _cidxOpenElevationModal();
                return;
            }
        } catch (e) {
            // Not a JSON body — fall through to default HTMX handling.
        }
    }

    if (xhr.status === 401 ||
        (xhr.responseURL && xhr.responseURL.indexOf('/login') !== -1)) {
        evt.detail.shouldSwap = false;
        if (!window._cidxRedirecting) {
            window._cidxRedirecting = true;
            window.location.href = '/login';
            setTimeout(function() { window._cidxRedirecting = false; }, 3000);
        }
    }
});

function _cidxOpenElevationModal() {
    var modal = document.getElementById('elevationModal');
    if (!modal) return;
    document.getElementById('elevationTotpCode').value = '';
    var recEl = document.getElementById('elevationRecoveryCode');
    if (recEl) recEl.value = '';
    var errEl = document.getElementById('elevationError');
    errEl.style.display = 'none';
    errEl.textContent = '';
    modal.showModal();
    setTimeout(function() { document.getElementById('elevationTotpCode').focus(); }, 50);
}

function _cidxCloseElevationModal(cancelled) {
    var modal = document.getElementById('elevationModal');
    if (modal && modal.open) modal.close();
    var pending = window._cidxElevationPending;
    window._cidxElevationPending = null;
    if (cancelled && pending && typeof pending._fetchReject === 'function') {
        pending._fetchReject(new Error('elevation_cancelled'));
    }
}

document.addEventListener('DOMContentLoaded', function() {
    var modal = document.getElementById('elevationModal');
    if (!modal) return;

    document.getElementById('elevationCancelBtn').addEventListener('click', function() {
        _cidxCloseElevationModal(true);
    });

    function _doElevateAjax() {
        var totpCode = document.getElementById('elevationTotpCode').value.trim();
        var recEl = document.getElementById('elevationRecoveryCode');
        var recoveryCode = recEl ? recEl.value.trim() : '';
        var btn = document.getElementById('elevationVerifyBtn');
        btn.setAttribute('aria-busy', 'true');
        btn.disabled = true;
        var params = new URLSearchParams();
        if (totpCode) params.append('totp_code', totpCode);
        if (recoveryCode) params.append('recovery_code', recoveryCode);
        fetch('/auth/elevate-ajax', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: params.toString(),
        })
        .then(function(resp) {
            return resp.json().then(function(data) { return {status: resp.status, data: data}; });
        })
        .then(function(result) {
            btn.removeAttribute('aria-busy');
            btn.disabled = false;
            if (result.data && result.data.success) {
                var pending = window._cidxElevationPending;
                _cidxCloseElevationModal(false);
                if (pending && pending.verb && pending.path) {
                    if (pending.sourceElt) {
                        pending.sourceElt.querySelectorAll('textarea').forEach(function(el) { el.value = ''; });
                    }
                    htmx.ajax(pending.verb.toUpperCase(), pending.path, {
                        target: pending.target,
                        values: pending.parameters || {},
                    });
                } else if (pending && typeof pending.callback === 'function') {
                    pending.callback();
                }
            } else {
                var errEl = document.getElementById('elevationError');
                errEl.textContent = (result.data && result.data.error) || 'Invalid code.';
                errEl.style.display = '';
                document.getElementById('elevationTotpCode').value = '';
                document.getElementById('elevationTotpCode').focus();
            }
        })
        .catch(function() {
            btn.removeAttribute('aria-busy');
            btn.disabled = false;
            var errEl = document.getElementById('elevationError');
            errEl.textContent = 'Network error. Please try again.';
            errEl.style.display = '';
        });
    }

    document.getElementById('elevationVerifyBtn').addEventListener('click', _doElevateAjax);
    document.getElementById('elevationTotpCode').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { _doElevateAjax(); }
    });
});
