"""Page scripts that call elevation-gated routes stay inside fetch().

``elevation_interceptor.js`` (loaded by base.html) wraps ``window.fetch``:
a 403 ``elevation_required`` opens the TOTP modal and the original request
is retried after elevation.  A page NAVIGATION bypasses it, so a lapsed
window would replace the page with the raw JSON refusal.  Hence:

- logs.html ``exportLogs()`` downloads ``/admin/logs/export`` through
  fetch() and a blob, exactly like ``audit_logs.js`` ``exportResults()``,
  keeping the server's filename (JSON and CSV);
- ssh_keys.html ``copyPublicKey()`` checks ``response.ok`` before writing
  to the clipboard, like ``showPublicKey()``, so an error body is never
  copied.

Each page's own inline ``<script>`` is taken verbatim from its template,
syntax-checked with ``node --check`` (as test_audit_log_page.py does for
audit_logs.js), then executed in node's ``vm``.  Only the browser surface
the functions touch is provided (element lookup, an anchor, the fetch
reply, the clipboard, ``location``); the page code itself is real.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.web import routes as web_routes

_TEMPLATES = Path(web_routes.__file__).parent / "templates"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, scenarioPath] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
const record = {fetches: [], clicked: [], navigatedTo: null, clipboard: [], alerts: []};
const elements = {};
for (const [id, value] of Object.entries(scenario.elements)) {
    elements[id] = {id: id, value: value, textContent: ''};
}
const document = {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => ({
        tagName: tag, href: '', download: '',
        click() { record.clicked.push({href: this.href, download: this.download}); },
        remove() {},
    }),
    body: {appendChild() {}},
};
let href = '/admin/page';
const location = {};
Object.defineProperty(location, 'href', {
    get: () => href,
    set: (value) => { record.navigatedTo = value; href = value; },
});
const reply = scenario.reply;
function fetch(url, options) {
    record.fetches.push({url: url, options: options || null});
    if (reply.reject) return Promise.reject(new Error(reply.reject));
    const headers = reply.headers || {};
    return Promise.resolve({
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        headers: {get: (name) => (name.toLowerCase() in headers ? headers[name.toLowerCase()] : null)},
        text: () => Promise.resolve(reply.body),
        blob: () => Promise.resolve({body: reply.body}),
    });
}
const context = {
    document: document,
    window: {location: location, fetch: fetch},
    location: location,
    fetch: fetch,
    URL: {createObjectURL: () => 'blob:example', revokeObjectURL() {}},
    navigator: {clipboard: {writeText: (text) => { record.clipboard.push(text); return Promise.resolve(); }}},
    alert: (message) => record.alerts.push(message),
    htmx: {ajax() {}},
    console: console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), context);
vm.runInContext(scenario.call, context);
setTimeout(() => {
    record.text = {};
    for (const [id, element] of Object.entries(elements)) record.text[id] = element.textContent;
    process.stdout.write(JSON.stringify(record));
}, 100);
"""


def _node() -> str:
    node = shutil.which("node")
    assert node, "node is required (present on every host that runs this suite)"
    return node


def _page_script(template: str) -> str:
    """The template's inline (attribute-less) <script> block, verbatim."""
    source = (_TEMPLATES / template).read_text()
    blocks = re.findall(r"<script>\s*(.*?)</script>", source, re.S)
    assert len(blocks) == 1, f"{template}: expected one inline <script>"
    script: str = blocks[0]
    assert "{{" not in script and "{%" not in script, "script must be Jinja-free"
    return script


def _run(
    tmp_path: Path, template: str, call: str, elements: Dict[str, str], reply: Dict
) -> Dict[str, Any]:
    script = tmp_path / "page.js"
    script.write_text(_page_script(template))
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"call": call, "elements": elements, "reply": reply})
    )
    done = subprocess.run(
        [_node(), str(harness), str(script), str(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    result: Dict[str, Any] = json.loads(done.stdout)
    return result


@pytest.mark.parametrize("template", ["logs.html", "ssh_keys.html"])
def test_page_script_is_valid_javascript(tmp_path: Path, template: str) -> None:
    script = tmp_path / "page.js"
    script.write_text(_page_script(template))
    checked = subprocess.run(
        [_node(), "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert checked.returncode == 0, checked.stderr


# ---------------------------------------------------------------------------
# logs.html exportLogs(): fetch + blob, never a navigation
# ---------------------------------------------------------------------------

_LOG_FILTERS = {
    "export-format": "json",
    "level": "ERROR",
    "search": "a b",
    "logs-export-status": "",
}


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_export_downloads_through_fetch_with_server_filename(
    tmp_path: Path, fmt: str
) -> None:
    filename = f"logs_20260101_000000.{fmt}"
    result = _run(
        tmp_path,
        "logs.html",
        "exportLogs()",
        {**_LOG_FILTERS, "export-format": fmt},
        {
            "status": 200,
            "body": "payload",
            "headers": {"content-disposition": f'attachment; filename="{filename}"'},
        },
    )

    assert result["navigatedTo"] is None, "export must not navigate the page"
    assert result["fetches"] == [
        {
            "url": f"/admin/logs/export?format={fmt}&level=ERROR&search=a%20b",
            "options": {"credentials": "same-origin"},
        }
    ]
    assert result["clicked"] == [{"href": "blob:example", "download": filename}]
    assert filename in result["text"]["logs-export-status"]


def test_export_without_filename_header_uses_format_extension(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "logs.html",
        "exportLogs()",
        {**_LOG_FILTERS, "export-format": "csv"},
        {"status": 200, "body": "payload"},
    )

    assert result["clicked"] == [{"href": "blob:example", "download": "logs.csv"}]


def test_export_failure_reports_status_and_never_navigates(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "logs.html",
        "exportLogs()",
        dict(_LOG_FILTERS),
        {"status": 403, "body": '{"detail":{"error":"elevation_required"}}'},
    )

    assert result["navigatedTo"] is None
    assert result["clicked"] == []
    assert "403" in result["text"]["logs-export-status"]


def test_export_cancelled_elevation_reports_no_failure(tmp_path: Path) -> None:
    """The interceptor rejects with 'elevation_cancelled' when the admin
    closes the TOTP modal; that is not an export failure."""
    result = _run(
        tmp_path,
        "logs.html",
        "exportLogs()",
        dict(_LOG_FILTERS),
        {"reject": "elevation_cancelled"},
    )

    assert result["navigatedTo"] is None
    assert result["clicked"] == []
    assert "failed" not in result["text"]["logs-export-status"].lower()


# ---------------------------------------------------------------------------
# ssh_keys.html copyPublicKey(): only a successful reply reaches the clipboard
# ---------------------------------------------------------------------------


def test_copy_public_key_never_copies_an_error_body(tmp_path: Path) -> None:
    error_body = '{"detail":{"error":"elevation_required"}}'
    result = _run(
        tmp_path,
        "ssh_keys.html",
        "copyPublicKey('example-key', '')",
        {},
        {"status": 403, "body": error_body},
    )

    assert result["fetches"][0]["url"] == "/api/ssh-keys/example-key/public"
    assert result["clipboard"] == []
    assert any("Failed to copy" in a for a in result["alerts"]), result["alerts"]


def test_copy_public_key_copies_a_successful_reply(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "ssh_keys.html",
        "copyPublicKey('example-key', '')",
        {},
        {"status": 200, "body": "ssh-ed25519 AAAAexample example@example.com"},
    )

    assert result["clipboard"] == ["ssh-ed25519 AAAAexample example@example.com"]
