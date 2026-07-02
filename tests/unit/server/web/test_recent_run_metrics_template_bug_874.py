"""
Unit tests for Story D of Bug #874: Recent Run Metrics UI template redesign.

FR7 requirements tested (6 scenarios, 3 test methods):
  1. (standalone) Legacy null timings -> em-dash in Phase timings cell, no "0.0"
  2. (standalone) NULL run_type -> em-dash in Type column, no run-type-* badge
  3. (parametrized x4) delta/full/refinement/legacy-P1P2 -> correct pills/badges

All assertions scoped to id="recent-run-metrics" section.

Template rendered via _render_complete_response() which pre-parses
phase_timings_json str -> dict in the view layer (no Jinja filter needed).
"""

import json
import os
import re
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_ENDPOINT = "/admin/partials/depmap-job-status"
_ROUTES = "code_indexer.server.web.dependency_map_routes"

# Credentials from env — the server seeds "admin"/"admin" as its test-only
# default (CLAUDE.md). Override via CIDX_TEST_ADMIN_USER / CIDX_TEST_ADMIN_PASSWORD.
_ADMIN_USERNAME = os.environ.get("CIDX_TEST_ADMIN_USER", "admin")
_ADMIN_PASSWORD = os.environ.get("CIDX_TEST_ADMIN_PASSWORD", "admin")

_HTTP_OK = 200
_HTTP_REDIRECT = 303


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles — minimal, no Mock library
# ─────────────────────────────────────────────────────────────────────────────


class _FakeCache:
    """Stand-in for DependencyMapDashboardCacheBackend with configurable run_history."""

    def __init__(self, run_history: list) -> None:
        self._result = json.dumps(
            {
                "health": "Healthy",
                "color": "GREEN",
                "status": "idle",
                "last_run": None,
                "next_run": None,
                "error_message": None,
                "run_history": run_history,
            }
        )

    def is_fresh(self, ttl_seconds: int) -> bool:
        return True

    def get_cached(self) -> Optional[Dict[str, Any]]:
        return {
            "result_json": self._result,
            "computed_at": "2026-01-01T00:00:00+00:00",
            "job_id": None,
            "last_failure_message": None,
            "last_failure_at": None,
        }

    def get_running_job_id(self, job_tracker=None) -> Optional[str]:
        return None

    def claim_job_slot(self, new_job_id: str) -> Optional[str]:
        return None

    def clear_job_slot_for_retry(self) -> None:
        pass

    def clear_job_slot(self) -> None:
        pass


class _FakeJobTracker:
    def get_job(self, job_id: str):
        return None

    def register_job(self, *args, **kwargs) -> None:
        pass

    def update_status(self, job_id: str, **kwargs) -> None:
        pass


class _FakeBgJobManager:
    def submit_job(self, *args, **kwargs) -> str:
        return "unused-job-id"


class _FakeDashboardService:
    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        return {
            "health": "Healthy",
            "color": "GREEN",
            "status": "idle",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app():
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture(scope="module")
def client(app):
    """TestClient with admin session cookie pre-set (same pattern as siblings)."""
    with TestClient(app) as tc:
        login_page = tc.get("/login")
        assert login_page.status_code == _HTTP_OK
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        assert match, "Could not extract CSRF token from login page"
        csrf_token = match.group(1)
        resp = tc.post(
            "/login",
            data={
                "username": _ADMIN_USERNAME,
                "password": _ADMIN_PASSWORD,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == _HTTP_REDIRECT
        assert "session" in resp.cookies
        tc.cookies.set("session", resp.cookies["session"])
        yield tc


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_row(
    *,
    timestamp: str = "2026-01-01T12:00:00",
    domain_count: int = 5,
    total_chars: int = 1000,
    edge_count: int = 20,
    repos_analyzed: int = 3,
    pass1_duration_s: Optional[float] = None,
    pass2_duration_s: Optional[float] = None,
    run_type: Optional[str] = None,
    phase_timings_json: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a run_history row dict with sensible defaults."""
    return {
        "timestamp": timestamp,
        "domain_count": domain_count,
        "total_chars": total_chars,
        "edge_count": edge_count,
        "repos_analyzed": repos_analyzed,
        "pass1_duration_s": pass1_duration_s,
        "pass2_duration_s": pass2_duration_s,
        "run_type": run_type,
        "phase_timings_json": phase_timings_json,
    }


def _render_section(client: TestClient, run_history: list) -> str:
    """Render the depmap-job-status partial and return the recent-run-metrics section."""
    cache = _FakeCache(run_history=run_history)
    with (
        patch(f"{_ROUTES}._get_dashboard_cache_backend", return_value=cache),
        patch(f"{_ROUTES}._get_job_tracker", return_value=_FakeJobTracker()),
        patch(
            f"{_ROUTES}._get_background_job_manager",
            return_value=_FakeBgJobManager(),
        ),
        patch(
            f"{_ROUTES}._get_dashboard_service",
            return_value=_FakeDashboardService(),
        ),
        patch(f"{_ROUTES}._get_dep_map_output_dir", return_value=None),
    ):
        resp = client.get(_ENDPOINT)
    assert resp.status_code == _HTTP_OK, f"Unexpected HTTP status: {resp.status_code}"
    html = resp.text

    match = re.search(
        r'id="recent-run-metrics"(.*?)(?=<article\b|</div>\s*<!--\s*Story\s*#342|$)',
        html,
        re.DOTALL,
    )
    assert match is not None, (
        'Could not find id="recent-run-metrics" section. '
        "The template must set this id on the Recent Run Metrics article."
    )
    return match.group(1)


# ─────────────────────────────────────────────────────────────────────────────
# Tests (3 methods, 6 scenarios total)
# ─────────────────────────────────────────────────────────────────────────────


class TestRecentRunMetricsTemplate:
    """FR7 template redesign — six scenarios in three test methods."""

    def test_all_null_timings_renders_em_dash_not_zero(self, client):
        """
        Row with all NULL timing fields must show em-dash in the Phase timings
        cell and must NOT contain '0.0' anywhere in the metrics section.

        This kills the 'or 0' coercion bug: {{ "%.1f"|format(None or 0) }} → "0.0".
        """
        section = _render_section(client, [_make_row()])

        assert "phase-pill" not in section, "No pills expected for all-NULL timings"
        assert "—" in section, "Expected em-dash for NULL Phase timings"
        assert "0.0" not in section, "NULL timings must NOT render as 0.0 (bug #874)"

    def test_null_run_type_renders_em_dash_in_type_column(self, client):
        """
        Row with run_type=None must show an em-dash in the Type column
        and must NOT render any run-type-* badge CSS class.
        """
        section = _render_section(client, [_make_row(run_type=None)])

        assert "run-type-" not in section, "No run-type badge for NULL run_type"
        assert "—" in section, "Expected em-dash in Type column for NULL run_type"

    @pytest.mark.parametrize(
        "row,expected_strings,must_contain_pill",
        [
            pytest.param(
                _make_row(
                    run_type="delta",
                    phase_timings_json='{"detect_s":0.5,"merge_s":94.1,"finalize_s":0.2}',
                ),
                ["detect", "0.5s", "merge", "94.1s", "finalize", "0.2s"],
                True,
                id="delta_phase_pills",
            ),
            pytest.param(
                _make_row(
                    run_type="full",
                    phase_timings_json='{"synth_s":12.4,"per_domain_s":318.7,"finalize_s":0.2}',
                ),
                ["run-type-full"],
                False,
                id="full_type_badge",
            ),
            pytest.param(
                _make_row(
                    run_type="refinement",
                    phase_timings_json='{"refine_s":61.0}',
                ),
                ["refine", "61.0s"],
                True,
                id="refinement_refine_pill",
            ),
            pytest.param(
                _make_row(pass1_duration_s=12.4, pass2_duration_s=318.7),
                ["P1", "12.4s", "P2", "318.7s"],
                True,
                id="legacy_p1_p2_fallback",
            ),
        ],
    )
    def test_row_renders_correct_markup(
        self,
        client,
        row: Dict[str, Any],
        expected_strings: List[str],
        must_contain_pill: bool,
    ):
        """
        Parametrized: delta/full/refinement/legacy-P1P2 rows each render the
        correct markup inside the recent-run-metrics section.

        - delta: detect/merge/finalize phase-pill spans with correct durations
        - full: run-type-full badge CSS class with visible 'full' text
        - refinement: refine 61.0s phase-pill
        - legacy P1/P2: P1 12.4s + P2 318.7s fallback pills
        """
        section = _render_section(client, [row])

        if must_contain_pill:
            assert 'class="phase-pill"' in section, (
                f"Expected phase-pill CSS class in metrics section for {row.get('run_type')!r}"
            )

        for text in expected_strings:
            assert text in section, (
                f"Expected {text!r} in metrics section for run_type={row.get('run_type')!r}"
            )

        # Special assertion: full-type badge must contain visible text 'full'
        if row.get("run_type") == "full":
            assert re.search(r"run-type-full[^>]*>\s*full\s*<", section), (
                "Expected visible text 'full' inside the run-type-full badge element"
            )
