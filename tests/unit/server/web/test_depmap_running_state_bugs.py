"""
Unit tests for two "navigate-back" bugs in the dependency map page.

Bug 1: Content health override in depmap_job_status_partial() unconditionally
       replaces the "Running"/"BLUE" badge with "Unhealthy"/"Critical" even
       when analysis is actively running.

Bug 2: The depmap-activity-entries div has no hx-trigger on the initial HTML,
       so journal polling never starts on a fresh page load while analysis is
       already running.

Both tests are FAILING before the fixes are applied.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures (identical pattern to existing test files in this dir)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create FastAPI app with minimal startup."""
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login."""
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"
    return login_resp.cookies


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1: Content health MUST NOT override "Running" badge
# ─────────────────────────────────────────────────────────────────────────────


class TestJobStatusRunningBadgeNotOverriddenByContentHealth:
    """
    Bug 1: depmap_job_status_partial() must return "Running"/"BLUE" badge even
    when DepMapHealthDetector reports the content as unhealthy/critical, as long
    as the job tracking status is "running".

    Fix: Guard the content health override block with
         ``if output_dir is not None and job_status.get("status") != "running":``
    """

    ENDPOINT = "/admin/partials/depmap-job-status"

    def _make_unhealthy_report(self, status="needs_repair"):
        """Build a HealthReport that reports unhealthy content."""
        from code_indexer.server.services.dep_map_health_detector import (
            Anomaly,
            HealthReport,
        )

        anomaly = Anomaly(type="missing_domain_file", domain="auth")
        return HealthReport(
            status=status,
            anomalies=[anomaly],
            repairable_count=1,
        )

    def _make_critical_report(self):
        """Build a HealthReport that reports critical content."""
        return self._make_unhealthy_report(status="critical")

    def _running_job_status(self):
        """Return a job_status dict with status='running'."""
        return {
            "health": "Running",
            "color": "BLUE",
            "status": "running",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }

    def test_running_badge_preserved_when_content_needs_repair(
        self, client, admin_session_cookie
    ):
        """
        When tracking status=running AND content health=needs_repair,
        the partial must display 'Running' badge (not 'Unhealthy').
        """
        fake_dir = Path("/fake/dep-map-output")
        unhealthy_report = self._make_unhealthy_report("needs_repair")
        running_status = self._running_job_status()

        with patch(
            "code_indexer.server.web.dependency_map_routes._get_job_status_data",
            return_value=running_status,
        ), patch(
            "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
            return_value=fake_dir,
        ), patch(
            "code_indexer.server.services.dep_map_health_detector.DepMapHealthDetector"
        ) as mock_detector_cls:
            mock_detector_cls.return_value.detect.return_value = unhealthy_report

            response = client.get(self.ENDPOINT, cookies=admin_session_cookie)

        assert response.status_code == 200
        content = response.text
        # Must show "Running", must NOT show "Unhealthy"
        assert "Running" in content, (
            f"Expected 'Running' in response but got: {content[:500]}"
        )
        assert "Unhealthy" not in content, (
            f"'Unhealthy' must not override Running badge: {content[:500]}"
        )

    def test_running_badge_preserved_when_content_critical(
        self, client, admin_session_cookie
    ):
        """
        When tracking status=running AND content health=critical,
        the partial must display 'Running' badge (not 'Critical').
        """
        fake_dir = Path("/fake/dep-map-output")
        critical_report = self._make_critical_report()
        running_status = self._running_job_status()

        with patch(
            "code_indexer.server.web.dependency_map_routes._get_job_status_data",
            return_value=running_status,
        ), patch(
            "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
            return_value=fake_dir,
        ), patch(
            "code_indexer.server.services.dep_map_health_detector.DepMapHealthDetector"
        ) as mock_detector_cls:
            mock_detector_cls.return_value.detect.return_value = critical_report

            response = client.get(self.ENDPOINT, cookies=admin_session_cookie)

        assert response.status_code == 200
        content = response.text
        assert "Running" in content, (
            f"Expected 'Running' in response but got: {content[:500]}"
        )
        assert "Critical" not in content, (
            f"'Critical' must not override Running badge: {content[:500]}"
        )

    def test_content_health_still_applies_when_status_not_running(
        self, client, admin_session_cookie
    ):
        """
        When tracking status is NOT running (e.g. 'idle'), content health
        override MUST still apply — this is the original Story #342 behaviour.
        """
        fake_dir = Path("/fake/dep-map-output")
        unhealthy_report = self._make_unhealthy_report("needs_repair")
        idle_status = {
            "health": "Healthy",
            "color": "GREEN",
            "status": "idle",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }

        with patch(
            "code_indexer.server.web.dependency_map_routes._get_job_status_data",
            return_value=idle_status,
        ), patch(
            "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
            return_value=fake_dir,
        ), patch(
            "code_indexer.server.services.dep_map_health_detector.DepMapHealthDetector"
        ) as mock_detector_cls:
            mock_detector_cls.return_value.detect.return_value = unhealthy_report

            response = client.get(self.ENDPOINT, cookies=admin_session_cookie)

        assert response.status_code == 200
        content = response.text
        # Content health IS applied when not running - "Unhealthy" or "Needs Repair"
        # should appear, NOT "Healthy"
        assert any(
            label in content for label in ("Unhealthy", "Needs Repair", "Critical")
        ), (
            f"Content health override must apply when not running. Got: {content[:500]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2: depmap-activity-entries div must have hx-trigger="load, every 3s"
# ─────────────────────────────────────────────────────────────────────────────


class TestDepmapActivityEntriesDivHasLoadTrigger:
    """
    Bug 2: The depmap-activity-entries div in dependency_map.html must include
    ``hx-trigger="load, every 3s"`` so that journal polling starts automatically
    on page load when analysis is already running.

    The fix is a one-line HTML change adding the hx-trigger attribute.
    """

    TEMPLATE_PATH = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "dependency_map.html"
    )

    def test_template_file_exists(self):
        """Sanity check: the template file we are testing actually exists."""
        assert self.TEMPLATE_PATH.exists(), (
            f"Template not found at {self.TEMPLATE_PATH}"
        )

    def test_depmap_activity_entries_has_hx_trigger_with_load(self):
        """
        The depmap-activity-entries div must have hx-trigger containing 'load'
        so polling starts immediately on page load.
        """
        template_content = self.TEMPLATE_PATH.read_text(encoding="utf-8")

        # Find the depmap-activity-entries div
        # We look for the id and then check nearby attributes
        assert 'id="depmap-activity-entries"' in template_content, (
            "depmap-activity-entries div must exist in template"
        )

        # Extract the opening tag of the depmap-activity-entries div
        match = re.search(
            r'<div[^>]*id="depmap-activity-entries"[^>]*>',
            template_content,
            re.DOTALL,
        )
        assert match is not None, "Could not find depmap-activity-entries opening tag"

        tag_html = match.group(0)

        # Must have hx-trigger attribute containing "load"
        assert "hx-trigger" in tag_html, (
            f"depmap-activity-entries div must have hx-trigger attribute.\n"
            f"Found tag: {tag_html}"
        )
        assert "load" in tag_html, (
            f"hx-trigger must include 'load' to start polling on page load.\n"
            f"Found tag: {tag_html}"
        )

    def test_depmap_activity_entries_hx_trigger_includes_every_3s(self):
        """
        The hx-trigger must also include 'every 3s' for continued polling.
        """
        template_content = self.TEMPLATE_PATH.read_text(encoding="utf-8")

        match = re.search(
            r'<div[^>]*id="depmap-activity-entries"[^>]*>',
            template_content,
            re.DOTALL,
        )
        assert match is not None, "Could not find depmap-activity-entries opening tag"

        tag_html = match.group(0)

        assert "every 3s" in tag_html, (
            f"hx-trigger must include 'every 3s' for continued polling.\n"
            f"Found tag: {tag_html}"
        )

    def test_depmap_activity_entries_hx_trigger_exact_value(self):
        """
        The hx-trigger value must be exactly 'load, every 3s' matching the
        spec in the bug description.
        """
        template_content = self.TEMPLATE_PATH.read_text(encoding="utf-8")

        match = re.search(
            r'<div[^>]*id="depmap-activity-entries"[^>]*>',
            template_content,
            re.DOTALL,
        )
        assert match is not None, "Could not find depmap-activity-entries opening tag"

        tag_html = match.group(0)

        # Extract the hx-trigger value
        trigger_match = re.search(r'hx-trigger="([^"]*)"', tag_html)
        assert trigger_match is not None, (
            f"Could not extract hx-trigger value from tag: {tag_html}"
        )
        trigger_value = trigger_match.group(1)
        assert trigger_value == "load, every 3s", (
            f"hx-trigger must be exactly 'load, every 3s', got: '{trigger_value}'"
        )
