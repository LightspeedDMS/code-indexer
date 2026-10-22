#!/usr/bin/env python3
"""Browser E2E: Groups > Repository Access hover, filter and bulk Yes/No.

Manual-run (browser automation is outside the automated E2E phases).  Runs
against a LIVE scratch cidx-server (never a shared one): Playwright drives
headless Chromium; nothing is mocked.

    E2E_ADMIN_USER=... E2E_ADMIN_PASS=... \
    python3 tests/e2e/manual/groups_repo_access_browser.py \
        --base-url http://127.0.0.1:8765 --server-dir <scratch>/.cidx-server

Setup data is neutral sample data seeded through the real backends and the
REST front door.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import httpx
from playwright.sync_api import Page, sync_playwright

UI_WAIT_MS = 5000
HTTP_TIMEOUT_S = 30
PAY_REPOS = 5
PLAT_REPOS = 4
MISC_REPOS = 3
GROUP = "billing-team"
# Deliberate non-secret fixture: the page must show it only masked.
LEDGER_URL = (
    "https://user:FAKE-TOKEN-FOR-MASKING-TEST@git.example.com/team-a/ledger.git"
)
LEDGER_TOKEN = "FAKE-TOKEN-FOR-MASKING-TEST"
LEDGER_MASKED = "https://***@git.example.com/team-a/ledger.git"
REPOS: Dict[str, str] = {
    "billing": "https://git.example.com/team-a/billing.git",
    "billing-2": "https://git.example.com/team-b/billing.git",
    "ledger": LEDGER_URL,
}
REPOS.update(
    {
        f"pay-0{n}": f"https://git.example.com/team-b/pay-0{n}.git"
        for n in range(1, PAY_REPOS + 1)
    }
)
REPOS.update(
    {
        f"plat-0{n}": f"https://git.example.com/team-b/plat-0{n}.git"
        for n in range(1, PLAT_REPOS + 1)
    }
)
REPOS.update(
    {
        f"misc-0{n}": f"https://git.example.com/team-a/misc-0{n}.git"
        for n in range(1, MISC_REPOS + 1)
    }
)
PRE_GRANTED = ["pay-01", "plat-01"]
FAILURES: List[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    """Record one check; a failure prints its detail."""
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {'' if ok else detail}")
    if not ok:
        FAILURES.append(label)


def seed(server_dir: Path) -> None:
    """Golden-repo rows and two categories, through the real SQLite backends."""
    from code_indexer.server.services.repo_category_service import RepoCategoryService
    from code_indexer.server.storage.sqlite_backends.golden_repo_metadata_backend import (
        GoldenRepoMetadataSqliteBackend,
    )

    db = str(server_dir / "data" / "cidx_server.db")
    backend = GoldenRepoMetadataSqliteBackend(db)
    for alias, url in REPOS.items():
        backend.add_repo(
            alias=alias,
            repo_url=url,
            default_branch="main",
            clone_path=str(server_dir / "data" / "golden-repos" / alias),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    categories = RepoCategoryService(db)
    categories.create_category("Payments", "pay-")
    categories.create_category("Platform", "plat-")
    categories.bulk_re_evaluate()


def rest_setup(base: str, user: str, password: str) -> int:
    """Create the throwaway group and pre-grant two repos via the REST door."""
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT_S) as client:
        login = client.post(
            "/auth/login", json={"username": user, "password": password}
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post(
            "/api/v1/groups", json={"name": GROUP, "description": ""}, headers=headers
        )
        created.raise_for_status()
        group_id = int(created.json()["id"])
        granted = client.post(
            f"/api/v1/groups/{group_id}/repos",
            json={"repos": PRE_GRANTED},
            headers=headers,
        )
        granted.raise_for_status()
    return group_id


def grant_rows(server_dir: Path, aliases: List[str]) -> int:
    """Success repo_access_grant audit rows naming *aliases* (read-only)."""
    if not aliases:
        return 0
    conn = sqlite3.connect(f"file:{server_dir / 'groups.db'}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in aliases)
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'repo_access_grant' "
            f"AND outcome = 'success' AND target_id IN ({marks})",
            aliases,
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def cell_state(page: Page, alias: str, group_id: int) -> str:
    selector = f'tr[data-repo-alias="{alias}"] .access-btn[data-group="{group_id}"]'
    return str(page.get_attribute(selector, "data-state"))


def set_filter(page: Page, term: str, shown: int, total: int) -> None:
    page.fill("#repo-access-filter", term)
    page.wait_for_function(
        "t => document.getElementById('repo-access-count').textContent === t",
        arg=f"Showing {shown} of {total}",
        timeout=UI_WAIT_MS,
    )


def _bulk_button(ctx: SimpleNamespace, action: str) -> str:
    return f'.bulk-btn[data-group="{ctx.group_id}"][data-action="{action}"]'


def bulk_with_dialog(ctx: SimpleNamespace, action: str, accept: bool) -> str:
    """Click a column control that must open the count dialog; return its text."""
    page: Page = ctx.page
    seen: List[str] = []

    def on_dialog(dialog) -> None:  # type: ignore[no-untyped-def]
        seen.append(dialog.message)
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", on_dialog)
    try:
        if accept:
            with page.expect_response(lambda r: r.url.endswith("/repo-access/bulk")):
                page.click(_bulk_button(ctx, action))
            page.wait_for_function(
                "s => !document.querySelector(s).disabled",
                arg=_bulk_button(ctx, action),
                timeout=UI_WAIT_MS,
            )
        else:
            page.click(_bulk_button(ctx, action))
    finally:
        page.remove_listener("dialog", on_dialog)
    check(f"{action} control opened the count dialog", len(seen) == 1, seen)
    return seen[0] if seen else ""


def bulk_without_dialog(ctx: SimpleNamespace, action: str) -> None:
    """Click a column control that must NOT open a dialog."""
    ctx.page.click(_bulk_button(ctx, action))


def scenario_hover(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    page.hover('tr[data-repo-alias="billing-2"] td.repo-name')
    title = page.get_attribute('tr[data-repo-alias="billing-2"] td.repo-name', "title")
    check("S1 billing-2 hover shows its clone URL", title == REPOS["billing-2"], title)
    ledger = page.get_attribute('tr[data-repo-alias="ledger"] td.repo-name', "title")
    check("S1 embedded credential is masked", ledger == LEDGER_MASKED, ledger)
    check("S1 secret absent from the page", LEDGER_TOKEN not in page.content())


def scenario_filter(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    shown, total = len(ctx.team_b), ctx.total
    set_filter(page, "TEAM-B/", shown, total)
    visible = page.locator("tbody tr[data-repo-alias]:visible").count()
    check("S2 flat view shows exactly the matching rows", visible == shown, visible)
    page.click("#toggle-grouped")
    set_filter(page, "PAY-", PAY_REPOS, total)
    headers = page.locator("tr.category-group-header:visible").all_text_contents()
    only_payments = len(headers) == 1 and headers[0].startswith("Payments")
    check("S2 grouped view hides categories with no match", only_payments, headers)
    set_filter(page, "", total, total)
    visible = page.locator("tbody tr[data-repo-alias]:visible").count()
    check("S2 clearing the filter shows every row", visible == total, visible)
    set_filter(page, "team-b/", shown, total)


def scenario_bulk_grant(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    page.click("tr.category-group-header:has-text('Platform')")
    expected = [a for a in ctx.team_b if a not in ctx.plat and a not in PRE_GRANTED]
    before = grant_rows(ctx.server_dir, expected)
    message = bulk_with_dialog(ctx, "grant", accept=False)
    question = f"Grant {len(expected)} repositories to {GROUP}?"
    check("S3 dialog names the count and group", message == question, message)
    check("S3 cancel sends no request", ctx.bodies == [], ctx.bodies)
    states = [cell_state(page, a, ctx.group_id) for a in expected]
    check("S3 cancel changes nothing", set(states) == {"denied"}, states)
    bulk_with_dialog(ctx, "grant", accept=True)
    states = [cell_state(page, a, ctx.group_id) for a in expected]
    check("S3 confirmed rows show Yes", set(states) == {"granted"}, states)
    plat = [cell_state(page, a, ctx.group_id) for a in ctx.plat]
    unchanged = ["granted" if a in PRE_GRANTED else "denied" for a in ctx.plat]
    check("S3 collapsed rows untouched", plat == unchanged, plat)
    added = grant_rows(ctx.server_dir, expected) - before
    check("S3 one grant audit row per repository", added == len(expected), added)


def scenario_nothing_to_change(ctx: SimpleNamespace) -> None:
    sent = len(ctx.bodies)
    bulk_without_dialog(ctx, "grant")
    status = ctx.page.text_content("#repo-access-status")
    check("S6 Nothing to change is shown", status == "Nothing to change", status)
    check("S6 no request is sent", len(ctx.bodies) == sent, ctx.bodies[sent:])


def scenario_bulk_revoke(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    to_revoke = [a for a in ctx.team_b if a not in ctx.plat]
    message = bulk_with_dialog(ctx, "revoke", accept=True)
    question = f"Revoke {len(to_revoke)} repositories from {GROUP}?"
    check("S4 dialog names the count and group", message == question, message)
    states = [cell_state(page, a, ctx.group_id) for a in to_revoke]
    check("S4 confirmed rows show No", set(states) == {"denied"}, states)
    kept = cell_state(page, "plat-01", ctx.group_id)
    check("S4 collapsed row untouched", kept == "granted", kept)


def scenario_admins_and_meta(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    controls = page.locator("th.group-column:has-text('admins') .bulk-btn").count()
    check("S5 admins column has no bulk control", controls == 0, controls)
    page.click("tr.category-group-header:has-text('Platform')")
    set_filter(page, "", ctx.total, ctx.total)
    bulk_with_dialog(ctx, "grant", accept=True)
    named = [b for b in ctx.bodies if "cidx-meta" in b]
    check("S5 cidx-meta is never part of a bulk change", named == [], named)


def run(base: str, server_dir: Path, user: str, password: str) -> None:
    seed(server_dir)
    group_id = rest_setup(base, user, password)
    team_b = [a for a, url in REPOS.items() if "team-b/" in url]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            bodies: List[str] = []
            page.on(
                "request",
                lambda r: bodies.append(r.post_data or "")
                if r.url.endswith("/repo-access/bulk")
                else None,
            )
            page.goto(f"{base}/login")
            page.fill('input[name="username"]', user)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.goto(f"{base}/admin/groups")
            page.click("#tab-repos")
            ctx = SimpleNamespace(
                page=page,
                group_id=group_id,
                total=page.locator("tbody tr[data-repo-alias]").count(),
                team_b=team_b,
                plat=[a for a in team_b if a.startswith("plat-")],
                bodies=bodies,
                server_dir=server_dir,
            )
            scenario_hover(ctx)
            scenario_filter(ctx)
            scenario_bulk_grant(ctx)
            scenario_nothing_to_change(ctx)
            scenario_bulk_revoke(ctx)
            scenario_admins_and_meta(ctx)
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--server-dir", required=True, type=Path)
    args = parser.parse_args()
    user = os.environ.get("E2E_ADMIN_USER")
    password = os.environ.get("E2E_ADMIN_PASS")
    if not user or not password:
        print("E2E_ADMIN_USER and E2E_ADMIN_PASS must be set", file=sys.stderr)
        return 2
    run(args.base_url.rstrip("/"), args.server_dir, user, password)
    print(f"{len(FAILURES)} failure(s)" if FAILURES else "ALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
