"""The Web Audit Logs page (``/admin/audit-logs``), end to end through TestClient.

One real app per module: a fresh server data directory, the real lifespan
(so ``app.state.audit_service`` is the real store), a real Web login, and rows
written through the store's single write function.  Elevation is simulated
with the project's established ``dependency_overrides`` pattern (no TOTP in
CI); the refusal tests run WITHOUT that override.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterator, List, Tuple
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.services.audit_log_query import AuditFilters
from tests.unit.server._audit_read_support import make_event

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _ago(**delta) -> str:
    """A time *delta* before NOW (the moment of the call, never import time:
    a module-level clock ages out when this file runs late in a long batch)."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# Long, allowlisted details: config key names (> 2 KB as JSON).
_MANY_KEYS = [f"example_setting_{i:03d}" for i in range(120)]


class _Collector(HTMLParser):
    """Collects (tag, attrs, text) for every element."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: List[Tuple[str, Dict[str, str]]] = []
        self.texts: List[str] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {k: (v or "") for k, v in attrs}))

    def handle_data(self, data):
        self.texts.append(data)


def _parse(html: str) -> _Collector:
    parser = _Collector()
    parser.feed(html)
    return parser


def _by_class(html: str, cls: str) -> List[Dict[str, str]]:
    return [
        attrs
        for _, attrs in _parse(html).elements
        if cls in attrs.get("class", "").split()
    ]


def _row_ids(html: str) -> List[int]:
    return [int(a["data-row-id"]) for a in _by_class(html, "audit-row")]


def _elevation_deps(router) -> list:
    found = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            fn = getattr(dep, "dependency", None)
            if (
                fn is not None
                and getattr(fn, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                found.append(fn)
    return found


def _seed_rows(store) -> Dict[str, int]:
    """Rows for every display rule, keyed by a label -> row id."""
    events = {
        "security_old": make_event(ts=_ago(days=10), actor="pageuser", target_id="old"),
        "token_refresh": make_event(
            ts=_ago(hours=5),
            action_type="token_refresh_success",
            target_type="auth",
            actor="pageuser",
            target_id="pageuser",
        ),
        "incident": make_event(
            ts=_ago(hours=4),
            action_type="security_incident",
            target_type="auth",
            actor="pageuser",
            target_id="pageuser",
        ),
        "attempt_pending": make_event(
            ts=_ago(seconds=20),
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id="cred-p",
            outcome="attempted",
            actor="pageuser",
            correlation_id="corr-pending",
        ),
        "attempt_unknown": make_event(
            ts=_ago(hours=3),
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id="cred-u",
            outcome="attempted",
            actor="pageuser",
            correlation_id="corr-unknown",
        ),
        "paired_attempt": make_event(
            ts=_ago(hours=2, seconds=1),
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id="cred-x",
            outcome="attempted",
            actor="pageuser",
            correlation_id="corr-pair",
        ),
        "paired_success": make_event(
            ts=_ago(hours=2),
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id="cred-x",
            outcome="success",
            actor="pageuser",
            correlation_id="corr-pair",
        ),
        "job": make_event(
            ts=_ago(hours=1, minutes=30),
            action_type="golden_repo_removed",
            target_type="repo",
            target_id="example-repo",
            actor="pageuser",
            details_json='{"job_id": "j1"}',
        ),
        "system": make_event(
            ts=_ago(hours=1, minutes=20),
            action_type="user_created",
            actor="system:golden-repo-reconciler",
            actor_is_system=True,
            source="system",
            ip_address=None,
            auth_method="system",
            target_id="sysuser",
        ),
        "fake_system": make_event(
            ts=_ago(hours=1, minutes=10),
            action_type="user_created",
            actor="system:evil",
            target_id="fakesys",
        ),
        "failed_login": make_event(
            ts=_ago(hours=1),
            action_type="authentication_failure",
            target_type="auth",
            target_id="admin",
            actor="guesser",
            outcome="failure",
            auth_method="none",
            correlation_id="corr-login",
        ),
        "legacy": make_event(
            ts=_ago(minutes=50),
            action_type="group_create",
            target_type="group",
            target_id="7",
            actor="pageuser",
            outcome=None,
            source=None,
            ip_address=None,
            auth_method=None,
            details_json="L" * 3000,
        ),
        "long_details": make_event(
            ts=_ago(minutes=45),
            action_type="config_changed",
            target_type="config",
            target_id="server",
            actor="pageuser",
            details_json=json.dumps(
                {"change_kind": "update", "changed_keys": _MANY_KEYS}
            ),
        ),
        "xss": make_event(
            ts=_ago(minutes=40),
            action_type="security_incident",
            target_type="auth",
            target_id="<script>alert(1)</script>",
            actor="pageuser",
            details_json=json.dumps(
                {"username": "<img src=x onerror=alert(2)>", "note": "hidden-note"}
            ),
        ),
        "formula": make_event(
            ts=_ago(minutes=30),
            action_type="user_deleted",
            target_id="=HYPERLINK(1)",
            actor="exportuser",
        ),
    }
    for i in range(5):
        events[f"burst{i}"] = make_event(
            ts=_ago(minutes=20),  # five rows sharing ONE timestamp
            action_type="user_deleted",
            target_id=f"burst{i}",
            actor="burstuser",
        )
    ordered = list(events.items())
    store.insert_events([event for _, event in ordered])
    rows = store.query_page(
        AuditFilters(), "all", seek=None, direction="older", limit=1000
    )
    by_uuid = {row["event_uuid"]: row["id"] for row in rows}
    return {label: by_uuid[event.event_uuid] for label, event in ordered}


@pytest.fixture(scope="module")
def page_env(tmp_path_factory) -> Iterator[dict]:
    data_dir = tmp_path_factory.mktemp("audit_page_server")
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(data_dir)}):
        from code_indexer.server.app import create_app
        from code_indexer.server.services.config_service import reset_config_service
        from code_indexer.server.web.audit_log_routes import audit_log_web_router

        reset_config_service()
        app = create_app()
        original = dict(app.dependency_overrides)
        with TestClient(app, follow_redirects=False) as client:
            ids = _seed_rows(app.state.audit_service)
            _web_login(client, "admin", "admin")
            elevation = _elevation_deps(audit_log_web_router)
            for fn in elevation:
                app.dependency_overrides[fn] = lambda: None
            yield {"app": app, "client": client, "ids": ids, "elevation": elevation}
        app.dependency_overrides = original
        reset_config_service()


def _web_login(client: TestClient, username: str, password: str) -> None:
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "login page must carry a CSRF token"
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": match.group(1)},
    )
    assert response.status_code == 303, response.text[:300]


def _rows(env, **params) -> str:
    response = env["client"].get("/admin/partials/audit-logs", params=params)
    assert response.status_code == 200, response.text[:500]
    return str(response.text)


@contextmanager
def _without_elevation(env) -> Iterator[None]:
    """Run with the REAL elevation gate enforced (no elevation window)."""
    from code_indexer.server.services.config_service import get_config_service

    app = env["app"]
    config = get_config_service()
    config.update_setting("totp_elevation", "elevation_enforcement_enabled", True)
    saved = {fn: app.dependency_overrides.pop(fn) for fn in env["elevation"]}
    try:
        yield
    finally:
        app.dependency_overrides.update(saved)
        config.update_setting("totp_elevation", "elevation_enforcement_enabled", False)


_DATA_ROUTES = (
    "/partials/audit-logs",
    "/partials/audit-logs-aggregate",
    "/audit-logs/export",
)


class TestShellAndAccess:
    def test_nav_has_a_top_level_audit_logs_item(self, page_env):
        page = page_env["client"].get("/admin/audit-logs")
        assert page.status_code == 200
        links = [
            a
            for tag, a in _parse(page.text).elements
            if tag == "a" and a.get("href") == "/admin/audit-logs"
        ]
        assert links and "aria-current" in links[0]
        other = page_env["client"].get("/admin/logs")
        assert 'href="/admin/audit-logs"' in other.text

    def test_shell_without_a_session_redirects_to_login(self, page_env):
        anonymous = TestClient(page_env["app"], follow_redirects=False)
        response = anonymous.get("/admin/audit-logs")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?redirect_to=")

    def test_the_session_gate_is_resolved_at_call_time(self, page_env):
        """The page asks ``web.routes`` for the admin session on every
        request, so it never keeps a reference bound at import time (a
        reference captured while another test patched the gate would
        otherwise outlive that patch)."""
        with patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            response = page_env["client"].get("/admin/audit-logs")
        assert response.status_code == 303

    def test_shell_passes_query_parameters_to_the_first_load(self, page_env):
        page = page_env["client"].get(
            "/admin/audit-logs",
            params={"view": "auth_activity", "actor": "a&b", "window": "30d"},
        )
        elements = _parse(page.text).elements
        section = [a for _, a in elements if a.get("id") == "audit-list-section"][0]
        assert section["hx-get"].startswith("/admin/partials/audit-logs-aggregate?")
        assert "actor=a%26b" in section["hx-get"]
        assert 'value="30d" selected' in page.text
        bogus = page_env["client"].get("/admin/audit-logs", params={"view": "nope"})
        assert bogus.status_code == 200  # the partial reports the 400

    def test_every_data_route_requires_elevation(self):
        from code_indexer.server.web.audit_log_routes import audit_log_web_router

        gated = {
            route.path
            for route in audit_log_web_router.routes
            if isinstance(route, APIRoute) and _elevation_deps_of(route)
        }
        assert set(_DATA_ROUTES) <= gated
        shell = [
            r
            for r in audit_log_web_router.routes
            if isinstance(r, APIRoute) and r.path == "/audit-logs"
        ]
        assert shell and not _elevation_deps_of(shell[0])

    def test_data_routes_refuse_without_an_elevation_window(self, page_env):
        with _without_elevation(page_env):
            for path in _DATA_ROUTES:
                response = page_env["client"].get(f"/admin{path}")
                assert response.status_code == 403, (path, response.status_code)
                error = response.json()["detail"]["error"]
                assert error in {"totp_setup_required", "elevation_required"}
                assert "audit-row" not in response.text

    def test_data_routes_refuse_a_non_admin(self, page_env):
        from code_indexer.server.auth import dependencies
        from code_indexer.server.auth.user_manager import UserRole

        assert dependencies.user_manager is not None
        dependencies.user_manager.create_user(
            "erin", "Zq7!mountain-Harbor-92", UserRole.NORMAL_USER
        )
        other = TestClient(page_env["app"], follow_redirects=False)
        _web_login(other, "erin", "Zq7!mountain-Harbor-92")
        assert other.get("/admin/audit-logs").status_code == 303
        with _without_elevation(page_env):
            response = other.get("/admin/partials/audit-logs")
        assert response.status_code in (401, 403)
        assert "audit-row" not in response.text


def _elevation_deps_of(route) -> list:
    return [
        dep
        for dep in route.dependencies or []
        if getattr(getattr(dep, "dependency", None), "__qualname__", "")
        == _ELEVATION_QUALNAME
    ]


class TestSecurityView:
    def test_default_view_is_security_over_seven_days(self, page_env):
        ids = page_env["ids"]
        html = _rows(page_env)  # no parameters: the page's defaults
        shown = set(_row_ids(html))
        assert ids["incident"] in shown and ids["job"] in shown
        assert ids["token_refresh"] not in shown  # authentication activity
        assert ids["failed_login"] not in shown
        assert ids["security_old"] not in shown  # outside the 7-day window
        window = _by_class(html, "audit-window")
        assert window, "the applied window is always shown"
        assert "UTC" in html

    def test_all_time_window_includes_older_rows(self, page_env):
        html = _rows(page_env, window="all", actor="pageuser")
        assert page_env["ids"]["security_old"] in _row_ids(html)

    def test_total_is_shown(self, page_env):
        html = _rows(page_env, actor="burstuser")
        totals = _by_class(html, "audit-total")
        assert totals and totals[0]["data-total"] == "5"
        assert totals[0]["data-total-capped"] == "false"


def _get(env, url: str) -> str:
    response = env["client"].get(url)
    assert response.status_code == 200, response.text[:500]
    return str(response.text)


class TestFiltersAndPaging:
    def test_filters_narrow_the_rows(self, page_env):
        ids = page_env["ids"]
        html = _rows(page_env, window="all", actor="pageuser", outcome="attempted")
        assert set(_row_ids(html)) == {
            ids["attempt_pending"],
            ids["attempt_unknown"],
            ids["paired_attempt"],
        }
        html = _rows(page_env, window="all", target_type="repo", source="web")
        assert _row_ids(html) == [ids["job"]]

    @pytest.mark.parametrize(
        "params",
        [
            {"outcome": "maybe"},
            {"source": "carrier-pigeon"},
            {"view": "everything"},
            {"window": "forever"},
            {"window": "custom"},
            {"window": "custom", "date_from": "not-a-date"},
            {"cursor": "not-a-cursor"},
            {"direction": "newer"},
            {"limit": "many"},
            {"actor": "x" * 300},
        ],
    )
    def test_bad_filters_are_refused_with_400(self, page_env, params):
        rows_only = {"cursor", "direction", "limit", "view"}
        for path in (
            "/admin/partials/audit-logs",
            "/admin/partials/audit-logs-aggregate",
        ):
            if path.endswith("aggregate") and set(params) & rows_only:
                continue
            response = page_env["client"].get(path, params=params)
            assert response.status_code == 400, (path, params, response.status_code)
            assert _by_class(response.text, "audit-error")
            assert "audit-row" not in response.text

    def test_older_then_newer_walks_rows_sharing_one_timestamp(self, page_env):
        ids = page_env["ids"]
        expected = sorted((ids[f"burst{i}"] for i in range(5)), reverse=True)
        html = _rows(page_env, actor="burstuser", limit="2")
        pages = [html]
        assert not _by_class(html, "audit-page-newer")  # first page
        walked = _row_ids(html)
        for _ in range(5):
            older = _by_class(html, "audit-page-older")
            if not older:
                break
            html = _get(page_env, older[0]["hx-get"])
            pages.append(html)
            walked += _row_ids(html)
        assert walked == expected
        # Back towards the newest rows from the last page.
        back = _row_ids(pages[-1])
        html = pages[-1]
        for _ in range(5):
            newer = _by_class(html, "audit-page-newer")
            if not newer:
                break
            html = _get(page_env, newer[0]["hx-get"])
            back = _row_ids(html) + back
        assert back == expected
        assert _by_class(pages[-1], "audit-page-latest")
        # The requested page size survives navigation.
        assert [len(_row_ids(p)) for p in pages] == [2, 2, 1]
        assert "limit=2" in _by_class(pages[0], "audit-page-older")[0]["hx-get"]

    def test_paging_links_freeze_the_applied_window(self, page_env):
        html = _rows(page_env, actor="burstuser", limit="2")
        url = _by_class(html, "audit-page-older")[0]["hx-get"]
        assert "window=custom" in url and "date_from=" in url
        assert "actor=burstuser" in url

    def test_custom_window_with_both_bounds_is_frozen_into_links(self, page_env):
        html = _rows(
            page_env,
            actor="burstuser",
            limit="2",
            window="custom",
            date_from=_ago(days=1),
            date_to=_ago(minutes=1),
        )
        assert len(_row_ids(html)) == 2
        url = _by_class(html, "audit-page-older")[0]["hx-get"]
        assert "date_from=" in url and "date_to=" in url
        window = re.search(r'<p class="audit-window">([^<]*)', html)
        assert window and "UTC to " in window.group(1)
        assert not window.group(1).endswith("to now")  # the upper bound is shown

    def test_missing_store_fails_loudly_with_503(self, page_env):
        state = page_env["app"].state
        store = state.audit_service
        state.audit_service = None
        try:
            response = page_env["client"].get("/admin/partials/audit-logs")
        finally:
            state.audit_service = store
        assert response.status_code == 503

    def test_correlation_query_shows_exactly_the_correlated_rows(self, page_env):
        ids = page_env["ids"]
        html = _rows(page_env, view="all", window="all", correlation_id="corr-pair")
        assert set(_row_ids(html)) == {ids["paired_attempt"], ids["paired_success"]}
        links = _by_class(html, "audit-correlation-link")
        assert {a["data-correlation-id"] for a in links} == {"corr-pair"}


class TestAggregateView:
    def test_groups_count_authentication_activity_in_the_window(self, page_env):
        response = page_env["client"].get(
            "/admin/partials/audit-logs-aggregate", params={"actor": "guesser"}
        )
        assert response.status_code == 200
        groups = _by_class(response.text, "audit-group")
        assert [(g["data-action-type"], g["data-outcome"]) for g in groups] == [
            ("authentication_failure", "failure")
        ]
        assert _by_class(response.text, "audit-group-count")
        assert ">1<" in response.text  # the one failed login in the 24 h window
        drill = _by_class(response.text, "audit-drilldown")[0]["hx-get"]
        assert "action_type=authentication_failure" in drill
        assert "view=auth_activity" in drill and "window=custom" in drill
        rows = _get(page_env, drill)
        assert _row_ids(rows) == [page_env["ids"]["failed_login"]]
        assert "attempted: guesser" in rows

    def test_security_rows_never_enter_the_aggregate(self, page_env):
        response = page_env["client"].get(
            "/admin/partials/audit-logs-aggregate",
            params={"actor": "pageuser", "window": "all"},
        )
        groups = _by_class(response.text, "audit-group")
        actions = {g["data-action-type"] for g in groups}
        assert actions == {"token_refresh_success"}


def _refused(env) -> str:
    response = env["client"].get(
        "/admin/partials/audit-logs", params={"outcome": "bogus"}
    )
    assert response.status_code == 400
    return str(response.text)


def _row_html(html: str, row_id: int) -> str:
    match = re.search(
        rf'<tr class="audit-row" data-row-id="{row_id}">(.*?)</tr>', html, re.S
    )
    assert match, row_id
    return match.group(1)


class TestRowDisplay:
    @pytest.fixture(scope="class")
    def html(self, page_env) -> str:
        return _rows(page_env, view="all", window="all", limit="1000")

    def test_a_fresh_attempted_row_reads_in_progress(self, page_env):
        # Written now and rendered at once, so it is always inside the
        # pairing grace window, however late this test runs.
        event = make_event(
            ts=datetime.now(timezone.utc).isoformat(),
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id="cred-fresh",
            outcome="attempted",
            actor="freshuser",
        )
        page_env["app"].state.audit_service.insert_events([event])
        html = _rows(page_env, view="all", window="all", actor="freshuser")
        (row_id,) = _row_ids(html)
        assert "in progress" in _row_html(html, row_id)
        assert ">success<" not in html and ">failure<" not in html

    def test_outcome_labels(self, page_env, html):
        ids = page_env["ids"]
        unknown = _row_html(html, ids["attempt_unknown"])
        assert "outcome unknown" in unknown and "title=" in unknown
        paired = _row_html(html, ids["paired_attempt"])
        assert ">attempted<" in paired
        assert "submitted" in _row_html(html, ids["job"])
        assert ">success<" not in _row_html(html, ids["job"])
        for label in ("attempt_unknown", "paired_attempt"):
            cell = _row_html(html, ids[label])
            assert ">success<" not in cell and ">failure<" not in cell

    def test_actor_labels(self, page_env, html):
        ids = page_env["ids"]
        system = _row_html(html, ids["system"])
        assert "audit-actor-badge" in system
        fake = _row_html(html, ids["fake_system"])
        assert "audit-actor-badge" not in fake and "not a system actor" in fake
        assert "attempted: guesser" in _row_html(html, ids["failed_login"])

    def test_legacy_rows_show_dashes(self, page_env, html):
        legacy = _row_html(html, page_env["ids"]["legacy"])
        assert legacy.count("<td>-</td>") >= 3  # source, IP, node

    def test_details_are_previewed_and_capped(self, page_env, html):
        long_row = _row_html(html, page_env["ids"]["long_details"])
        summary_match = re.search(r"<summary>(.*?)</summary>", long_row, re.S)
        full_match = re.search(r"<pre>(.*?)</pre>", long_row, re.S)
        assert summary_match and full_match
        summary = unescape(summary_match.group(1))
        assert len(summary) == 203 and summary.endswith("...")
        full = unescape(full_match.group(1))
        assert len(full) == 2048
        assert "audit-details-truncated" in long_row

    def test_legacy_free_text_details_are_summarised_not_shown(self, page_env, html):
        legacy = _row_html(html, page_env["ids"]["legacy"])
        assert "LLLL" not in legacy
        assert "(unstructured)" in unescape(legacy)

    def test_details_outside_the_allowlist_are_named_not_shown(self, page_env, html):
        cell = unescape(_row_html(html, page_env["ids"]["xss"]))
        assert "hidden-note" not in cell
        assert '"omitted_fields": ["note"]' in cell


class TestTemplateSafety:
    def test_no_inline_event_handlers_anywhere(self, page_env):
        pages = [
            page_env["client"].get("/admin/audit-logs").text,
            _rows(page_env, view="all", window="all", limit="1000"),
            page_env["client"].get("/admin/partials/audit-logs-aggregate").text,
            _refused(page_env),
        ]
        for html in pages:
            for tag, attrs in _parse(html).elements:
                handlers = [a for a in attrs if a.lower().startswith("on")]
                assert not handlers, (tag, handlers)
                assert "hx-on" not in " ".join(attrs), tag

    def test_row_values_render_escaped(self, page_env):
        html = _rows(page_env, view="all", window="all", actor="pageuser")
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=alert(2)>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;img src=x onerror=alert(2)&gt;" in html  # a details value

    def test_config_reaches_js_as_json_data(self, page_env):
        html = page_env["client"].get("/admin/audit-logs").text
        match = re.search(
            r'<script type="application/json" id="audit-logs-config">(.*?)</script>',
            html,
            re.S,
        )
        assert match
        config = json.loads(match.group(1))
        assert config["rows_url"] == "/admin/partials/audit-logs"

    def test_script_never_writes_markup_from_data(self):
        from code_indexer.server.web import audit_log_routes

        js = (
            Path(audit_log_routes.__file__).parent / "static" / "js" / "audit_logs.js"
        ).read_text()
        node = shutil.which("node")
        assert node, "node is required (present on every host that runs this suite)"
        script = Path(audit_log_routes.__file__).parent / "static" / "js"
        checked = subprocess.run(
            [node, "--check", str(script / "audit_logs.js")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert checked.returncode == 0, checked.stderr
        # Markup sinks: a property write, or an API that parses a string.
        assert not re.search(r"\.(innerHTML|outerHTML)\s*[+]?=", js)
        for sink in ("insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in js, sink


class TestExport:
    def _export(self, page_env, **params):
        response = page_env["client"].get("/admin/audit-logs/export", params=params)
        assert response.status_code == 200, response.text[:300]
        assert response.headers["content-disposition"].startswith("attachment;")
        return response

    def test_csv_export_of_the_filtered_rows(self, page_env):
        response = self._export(
            page_env, format="csv", view="all", window="all", actor="burstuser"
        )
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert sorted(int(r["id"]) for r in rows) == sorted(
            page_env["ids"][f"burst{i}"] for i in range(5)
        )

    def test_export_carries_exactly_the_shared_row_fields(self, page_env):
        from code_indexer.server.services.audit_log_query import AUDIT_ROW_FIELDS

        response = self._export(
            page_env, format="csv", view="all", window="all", actor="pageuser"
        )
        reader = csv.DictReader(io.StringIO(response.text))
        assert tuple(reader.fieldnames or ()) == AUDIT_ROW_FIELDS
        body = json.loads(
            self._export(
                page_env, format="json", view="all", window="all", actor="pageuser"
            ).text
        )
        assert all(tuple(row) == AUDIT_ROW_FIELDS for row in body["rows"])
        exported = {row["id"]: row for row in body["rows"]}
        xss = json.loads(exported[page_env["ids"]["xss"]]["details"])
        assert xss == {
            "username": "<img src=x onerror=alert(2)>",
            "omitted_fields": ["note"],
        }
        assert "LLLL" not in response.text and "hidden-note" not in response.text

    def test_csv_cells_never_start_a_formula(self, page_env):
        response = self._export(
            page_env, format="csv", window="all", actor="exportuser"
        )
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert rows[0]["target_id"] == "'=HYPERLINK(1)"

    def test_json_export_keeps_values_as_data(self, page_env):
        response = self._export(
            page_env, format="json", view="all", window="all", actor="pageuser"
        )
        body = json.loads(response.text)
        targets = {r["target_id"] for r in body["rows"]}
        assert "<script>alert(1)</script>" in targets
        assert body["row_count"] == len(body["rows"])
        assert body["filters"] == {"actor": "pageuser"}

    def test_export_is_bounded(self, page_env, monkeypatch):
        from code_indexer.server.web import audit_log_routes

        monkeypatch.setattr(audit_log_routes, "EXPORT_MAX_ROWS", 3)
        monkeypatch.setattr(audit_log_routes, "EXPORT_CHUNK_ROWS", 2)
        response = self._export(page_env, format="json", view="all", window="all")
        body = json.loads(response.text)
        assert body["row_count"] == 3 and body["row_limit_reached"] is True
        newest = _row_ids(_rows(page_env, view="all", window="all", limit="3"))
        assert [r["id"] for r in body["rows"]] == newest  # two pages, no gap/dup

    @pytest.mark.parametrize(
        "params", [{"format": "xml"}, {"outcome": "maybe"}, {"window": "custom"}]
    )
    def test_export_refuses_bad_arguments(self, page_env, params):
        response = page_env["client"].get("/admin/audit-logs/export", params=params)
        assert response.status_code == 400


class TestRealElevationWindow:
    """No simulated elevation: enforcement ON, a real TOTP elevation."""

    def test_rows_load_only_after_a_real_elevation(self, page_env):
        import pyotp

        from code_indexer.server.web.mfa_routes import get_totp_service

        totp_service = get_totp_service()
        assert totp_service is not None
        with _without_elevation(page_env):
            secret = totp_service.generate_secret("admin")
            assert totp_service.activate_mfa("admin", pyotp.TOTP(secret).now())
            try:
                refused = page_env["client"].get(
                    "/admin/partials/audit-logs", params={"actor": "burstuser"}
                )
                assert refused.status_code == 403
                assert refused.json()["detail"]["error"] == "elevation_required"

                elevated = page_env["client"].post(
                    "/auth/elevate-ajax",
                    data={"totp_code": pyotp.TOTP(secret).now()},
                )
                assert elevated.status_code == 200, elevated.text
                assert elevated.json() == {"success": True}

                allowed = page_env["client"].get(
                    "/admin/partials/audit-logs", params={"actor": "burstuser"}
                )
                assert allowed.status_code == 200
                assert len(_row_ids(allowed.text)) == 5
                export = page_env["client"].get(
                    "/admin/audit-logs/export",
                    params={"format": "csv", "actor": "burstuser"},
                )
                assert export.status_code == 200
            finally:
                totp_service.disable_mfa("admin", actor="admin")
