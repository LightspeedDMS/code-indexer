#!/usr/bin/env python3
"""Browser E2E: Groups > Repository Access hover, filter and bulk Yes/No.

Manual-run (browser automation is outside the automated E2E phases).  Runs
against a LIVE scratch cidx-server (never a shared one); Playwright drives
headless Chromium; nothing is mocked.  Every setup step and every
verification goes through a front door: golden repos are registered with
REST ``POST /api/admin/golden-repos`` (local bare repos, file:// URLs),
categories and admin TOTP through the Web UI, audit rows are read with REST
``GET /api/v1/audit-logs``, and a throwaway normal user's view is read with
the MCP ``list_global_repos`` tool.  The server indexes what it registers,
so it needs an embedding provider key in its environment.

    E2E_ADMIN_USER=... E2E_ADMIN_PASS=... \
    python3 tests/e2e/manual/groups_repo_access_browser.py \
        --base-url http://127.0.0.1:8765 --repos-dir <scratch>/repos
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import httpx
import pyotp
from playwright.sync_api import Page, sync_playwright

UI_WAIT_MS = 5000
HTTP_TIMEOUT_S = 30
JOB_TIMEOUT_S = 900
JOB_POLL_S = 3
AUDIT_PAGE_LIMIT = 500
GROUP = "billing-team"
MEMBER = "example-member"
# alias -> (team directory, bare repository name)
LAYOUT: Dict[str, Tuple[str, str]] = {
    "billing": ("team-a", "billing"),
    "billing-2": ("team-b", "billing"),
    "pay-01": ("team-b", "pay-01"),
    "pay-02": ("team-b", "pay-02"),
    "pay-03": ("team-b", "pay-03"),
    "plat-01": ("team-b", "plat-01"),
    "plat-02": ("team-b", "plat-02"),
    "plat-03": ("team-b", "plat-03"),
    "misc-01": ("team-a", "misc-01"),
}
PAY_REPOS = sum(1 for a in LAYOUT if a.startswith("pay-"))
PRE_GRANTED = ["pay-01", "plat-01"]
FAILURES: List[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    """Record one check; a failure prints its detail."""
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {'' if ok else detail}")
    if not ok:
        FAILURES.append(label)


def make_bare_repos(repos_dir: Path) -> Dict[str, str]:
    """One-file local bare repositories; returns alias -> file:// clone URL."""
    git = ["git", "-c", "user.name=example", "-c", "user.email=e@example.com"]
    urls: Dict[str, str] = {}
    for alias, (team, name) in LAYOUT.items():
        work = repos_dir / "work" / team / name
        bare = repos_dir / team / f"{name}.git"
        if not bare.exists():
            work.mkdir(parents=True)
            body = f"def {name.replace('-', '_')}():\n    return 1\n"
            (work / "main.py").write_text(body)
            subprocess.run(git + ["init", "-q", "-b", "main"], cwd=work, check=True)
            subprocess.run(git + ["add", "."], cwd=work, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "init"], cwd=work, check=True)
            bare.parent.mkdir(parents=True, exist_ok=True)
            clone = ["git", "clone", "-q", "--bare", str(work), str(bare)]
            subprocess.run(clone, check=True)
        urls[alias] = f"file://{bare}"
    return urls


def login(client: httpx.Client, user: str, password: str) -> Dict[str, str]:
    """Bearer header for *user* from REST /auth/login."""
    resp = client.post("/auth/login", json={"username": user, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def member_password() -> str:
    """A fresh password for the throwaway normal user of this run."""
    return f"Ex-{secrets.token_urlsafe(18)}-9!"


def register_golden_repos(
    client: httpx.Client, admin: Dict[str, str], urls: Dict[str, str]
) -> None:
    """Register every repo through the REST door and wait for the jobs."""
    jobs: Dict[str, str] = {}
    for alias, url in urls.items():
        body = {"repo_url": url, "alias": alias, "default_branch": "main"}
        resp = client.post("/api/admin/golden-repos", json=body, headers=admin)
        resp.raise_for_status()
        jobs[alias] = resp.json()["job_id"]
    deadline = time.monotonic() + JOB_TIMEOUT_S
    pending = dict(jobs)
    while pending and time.monotonic() < deadline:
        for alias, job_id in list(pending.items()):
            resp = client.get(f"/api/jobs/{job_id}", headers=admin)
            resp.raise_for_status()
            status = resp.json()["status"]
            if status in ("completed", "failed", "cancelled"):
                if status != "completed":
                    raise RuntimeError(f"registering {alias} ended {status}")
                del pending[alias]
        time.sleep(JOB_POLL_S)
    if pending:
        raise RuntimeError(f"registration still running: {sorted(pending)}")


def setup_group_and_member(
    client: httpx.Client, admin: Dict[str, str], password: str
) -> int:
    """Throwaway group (two repos pre-granted) holding a throwaway normal user."""
    resp = client.post(
        "/api/v1/groups", json={"name": GROUP, "description": ""}, headers=admin
    )
    resp.raise_for_status()
    group_id = int(resp.json()["id"])
    steps = [
        (f"/api/v1/groups/{group_id}/repos", {"repos": PRE_GRANTED}),
        (
            "/api/admin/users",
            {"username": MEMBER, "password": password, "role": "normal_user"},
        ),
        (f"/api/v1/groups/{group_id}/members", {"user_id": MEMBER}),
    ]
    for path, body in steps:
        client.post(path, json=body, headers=admin).raise_for_status()
    return group_id


def audit_success_targets(
    client: httpx.Client, admin: Dict[str, str], action: str
) -> List[str]:
    """Targets of successful *action* rows, read through the audit REST door."""
    params: Dict[str, Any] = {
        "action_type": action,
        "outcome": "success",
        "tier": "all",
        "limit": AUDIT_PAGE_LIMIT,
    }
    resp = client.get("/api/v1/audit-logs", params=params, headers=admin)
    resp.raise_for_status()
    page = resp.json()
    logs = page["logs"]
    if page.get("total") is not None and page["total"] > len(logs):
        raise RuntimeError(f"audit page truncated: {page['total']} > {len(logs)}")
    return [str(row["target_id"]) for row in logs]


def visible_to_member(client: httpx.Client, member: Dict[str, str]) -> List[str]:
    """Repos the throwaway normal user sees through MCP list_global_repos."""
    import json

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_global_repos", "arguments": {}},
    }
    resp = client.post("/mcp", json=call, headers=member)
    resp.raise_for_status()
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    if payload.get("success") is not True:
        raise RuntimeError(f"list_global_repos failed: {payload}")
    return sorted(str(r["repo_name"]) for r in payload["repos"])


def create_categories(page: Page, base: str) -> None:
    """Payments and Platform categories through the Web UI form."""
    for name, pattern in (("Payments", "pay-"), ("Platform", "plat-")):
        page.goto(f"{base}/admin/repo-categories")
        form = page.locator('form[action="/admin/repo-categories/create"]')
        form.locator('input[name="name"]').fill(name)
        form.locator('input[name="pattern"]').fill(pattern)
        with page.expect_response(
            lambda r: r.url.endswith("/repo-categories/create")
        ) as created:
            form.locator('input[name="pattern"]').press("Enter")
        if not created.value.ok:
            raise RuntimeError(f"creating {name} answered {created.value.status}")


def setup_admin_totp(page: Page, base: str) -> pyotp.TOTP:
    """Enrol the admin's TOTP through the Web UI; return its generator."""
    page.goto(f"{base}/admin/mfa/setup")
    secret = re.sub(r"\s+", "", page.text_content(".mk") or "")
    if not secret:
        raise RuntimeError("MFA setup page shows no manual key")
    totp = pyotp.TOTP(secret)
    page.fill('input[name="totp_code"]', totp.now())
    with page.expect_navigation():
        page.press('input[name="totp_code"]', "Enter")
    return totp


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

    def on_dialog(dialog: Any) -> None:
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


def enable_elevation_enforcement(page: Page, base: str) -> None:
    """Turn on TOTP elevation enforcement through the Web UI Config door."""
    page.goto(f"{base}/admin/config")
    token = page.get_attribute('input[name="csrf_token"]', "value") or ""
    form: Dict[str, Any] = {
        "csrf_token": token,
        "elevation_enforcement_enabled": "true",
        "elevation_idle_timeout_seconds": "300",
        "elevation_max_age_seconds": "1800",
    }
    resp = page.request.post(f"{base}/admin/config/totp_elevation", form=form)
    if not resp.ok:
        raise RuntimeError(f"enabling elevation enforcement answered {resp.status}")


def _member_sees(ctx: SimpleNamespace) -> List[str]:
    return [a for a in visible_to_member(ctx.client, ctx.member) if a in LAYOUT]


def scenario_hover(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    titles = {}
    for alias in ("billing", "billing-2"):
        cell = f'tr[data-repo-alias="{alias}"] td.repo-name'
        page.hover(cell)
        titles[alias] = page.get_attribute(cell, "title")
    check(
        "S1 billing hover shows its clone URL",
        titles["billing"] == ctx.urls["billing"],
        titles,
    )
    check(
        "S1 billing-2 hover shows its clone URL",
        titles["billing-2"] == ctx.urls["billing-2"],
        titles,
    )
    check(
        "S1 numbered duplicates are told apart",
        titles["billing"] != titles["billing-2"],
        titles,
    )


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


def _audited(ctx: SimpleNamespace, action: str, aliases: List[str]) -> int:
    targets = audit_success_targets(ctx.client, ctx.admin, action)
    return sum(1 for t in targets if t in aliases)


def scenario_bulk_grant(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    page.click("tr.category-group-header:has-text('Platform')")
    expected = [a for a in ctx.team_b if a not in ctx.plat and a not in PRE_GRANTED]
    before = _audited(ctx, "repo_access_grant", expected)
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
    added = _audited(ctx, "repo_access_grant", expected) - before
    check("S3 one grant audit row per repository", added == len(expected), added)
    sees = _member_sees(ctx)
    check("S3 member now sees the granted repos", set(expected) <= set(sees), sees)


def scenario_nothing_to_change(ctx: SimpleNamespace) -> None:
    sent = len(ctx.bodies)
    bulk_without_dialog(ctx, "grant")
    status = ctx.page.text_content("#repo-access-status")
    check("S6 Nothing to change is shown", status == "Nothing to change", status)
    check("S6 no request is sent", len(ctx.bodies) == sent, ctx.bodies[sent:])


def scenario_bulk_revoke(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    to_revoke = [a for a in ctx.team_b if a not in ctx.plat]
    before = _audited(ctx, "repo_access_revoke", to_revoke)
    message = bulk_with_dialog(ctx, "revoke", accept=True)
    question = f"Revoke {len(to_revoke)} repositories from {GROUP}?"
    check("S4 dialog names the count and group", message == question, message)
    states = [cell_state(page, a, ctx.group_id) for a in to_revoke]
    check("S4 confirmed rows show No", set(states) == {"denied"}, states)
    kept = cell_state(page, "plat-01", ctx.group_id)
    check("S4 collapsed row untouched", kept == "granted", kept)
    added = _audited(ctx, "repo_access_revoke", to_revoke) - before
    check("S4 one revoke audit row per repository", added == len(to_revoke), added)
    sees = _member_sees(ctx)
    check("S4 member no longer sees them", not set(to_revoke) & set(sees), sees)


def open_matrix(page: Page, base: str) -> None:
    page.goto(f"{base}/admin/groups")
    page.click("#tab-repos")


def scenario_admins_and_meta(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    controls = page.locator("th.group-column:has-text('admins') .bulk-btn").count()
    check("S5 admins column has no bulk control", controls == 0, controls)
    page.click("tr.category-group-header:has-text('Platform')")
    set_filter(page, "", ctx.total, ctx.total)
    bulk_with_dialog(ctx, "grant", accept=True)
    named = [b for b in ctx.bodies if "cidx-meta" in b]
    check("S5 cidx-meta is never part of a bulk change", named == [], named)


def _wait_fresh_totp(ctx: SimpleNamespace) -> None:
    """Wait (bounded) for a TOTP step newer than the enrolment code's."""
    step = ctx.totp.interval
    deadline = time.monotonic() + step + 1
    while int(time.time() // step) <= int(ctx.totp_used_at // step):
        if time.monotonic() > deadline:
            raise RuntimeError("no fresh TOTP step")
        time.sleep(1)


def scenario_elevation(ctx: SimpleNamespace) -> None:
    page: Page = ctx.page
    aliases = list(LAYOUT)
    enable_elevation_enforcement(page, ctx.base)
    open_matrix(page, ctx.base)

    def accept(dialog: Any) -> None:
        dialog.accept()

    page.on("dialog", accept)
    try:
        bulk = lambda r: r.url.endswith("/repo-access/bulk")  # noqa: E731
        with page.expect_response(bulk) as refused:
            page.click(_bulk_button(ctx, "revoke"))
        check("E1 unelevated request is refused", refused.value.status == 403)
        page.wait_for_selector("#elevationModal[open]", timeout=UI_WAIT_MS)
        states = {cell_state(page, a, ctx.group_id) for a in aliases}
        check("E1 refused change leaves every cell", states == {"granted"}, states)
        _wait_fresh_totp(ctx)
        page.fill("#elevationTotpCode", ctx.totp.now())
        with page.expect_response(bulk) as retried:
            page.click("#elevationVerifyBtn")
        check("E2 retried request after elevation", retried.value.status == 200)
        page.wait_for_function(
            "s => !document.querySelector(s).disabled",
            arg=_bulk_button(ctx, "revoke"),
            timeout=UI_WAIT_MS,
        )
    finally:
        page.remove_listener("dialog", accept)
    states = {cell_state(page, a, ctx.group_id) for a in aliases}
    check("E2 elevated bulk change applies", states == {"denied"}, states)


def _browser_login(page: Page, base: str, user: str, password: str) -> None:
    page.goto(f"{base}/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', password)
    with page.expect_navigation():
        page.click('button[type="submit"]')


def run(base: str, repos_dir: Path, user: str, password: str) -> None:
    urls = make_bare_repos(repos_dir)
    team_b = [a for a, (team, _name) in LAYOUT.items() if team == "team-b"]
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT_S) as client:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                _browser_login(page, base, user, password)
                create_categories(page, base)
                register_golden_repos(client, login(client, user, password), urls)
                admin = login(client, user, password)
                secret = member_password()
                group_id = setup_group_and_member(client, admin, secret)
                totp = setup_admin_totp(page, base)
                bodies: List[str] = []
                page.on(
                    "request",
                    lambda r: bodies.append(r.post_data or "")
                    if r.url.endswith("/repo-access/bulk")
                    else None,
                )
                open_matrix(page, base)
                ctx = SimpleNamespace(
                    page=page,
                    base=base,
                    client=client,
                    admin=admin,
                    member=login(client, MEMBER, secret),
                    group_id=group_id,
                    total=page.locator("tbody tr[data-repo-alias]").count(),
                    team_b=team_b,
                    plat=[a for a in team_b if a.startswith("plat-")],
                    bodies=bodies,
                    urls=urls,
                    totp=totp,
                    totp_used_at=time.time(),
                )
                for scenario in (
                    scenario_hover,
                    scenario_filter,
                    scenario_bulk_grant,
                    scenario_nothing_to_change,
                    scenario_bulk_revoke,
                    scenario_admins_and_meta,
                    scenario_elevation,
                ):
                    scenario(ctx)
            finally:
                browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--repos-dir", required=True, type=Path)
    args = parser.parse_args()
    user = os.environ.get("E2E_ADMIN_USER")
    password = os.environ.get("E2E_ADMIN_PASS")
    if not user or not password:
        print("E2E_ADMIN_USER and E2E_ADMIN_PASS must be set", file=sys.stderr)
        return 2
    run(args.base_url.rstrip("/"), args.repos_dir, user, password)
    print(f"{len(FAILURES)} failure(s)" if FAILURES else "ALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
