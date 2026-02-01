"""
Tests for Bug #128 - Self-Monitoring enabled toggle doesn't start/stop service.

Tests that the enabled checkbox on the configuration page actually starts/stops
the running service, not just updating the config file.
"""

from fastapi import status
from fastapi.testclient import TestClient


def test_toggling_enabled_on_starts_service(
    authenticated_client: TestClient,
    web_infrastructure,
):
    """
    Bug #128: Toggling enabled ON should start the service if not running.

    Scenario: Service is stopped, user checks the enabled checkbox
    Expected: Service should start immediately upon save
    """
    from unittest.mock import Mock

    # Get the app from the test client
    app = authenticated_client.app

    # Create a mock service that tracks start/stop calls
    mock_service = Mock()
    mock_service.is_running = False
    mock_service.start = Mock()
    mock_service.stop = Mock()

    # Inject mock service into app state
    app.state.self_monitoring_service = mock_service

    # Get CSRF token
    get_response = authenticated_client.get("/admin/self-monitoring")
    csrf_token = web_infrastructure.extract_csrf_token(get_response.text)

    # Submit form with enabled=ON
    form_data = {
        "csrf_token": csrf_token,
        "enabled": "on",  # Toggle ON
        "cadence_minutes": "60",
        "model": "opus",
        "prompt_template": "",
    }

    response = authenticated_client.post("/admin/self-monitoring", data=form_data)

    # Should succeed
    assert response.status_code == status.HTTP_200_OK

    # Verify: service.start() was called
    mock_service.start.assert_called_once()
    mock_service.stop.assert_not_called()


def test_toggling_enabled_off_stops_service(
    authenticated_client: TestClient,
    web_infrastructure,
):
    """
    Bug #128: Toggling enabled OFF should stop the service if running.

    Scenario: Service is running, user unchecks the enabled checkbox
    Expected: Service should stop immediately upon save
    """
    from unittest.mock import Mock

    # Get the app from the test client
    app = authenticated_client.app

    # Create a mock service that is currently running
    mock_service = Mock()
    mock_service.is_running = True
    mock_service.start = Mock()
    mock_service.stop = Mock()

    # Inject mock service into app state
    app.state.self_monitoring_service = mock_service

    # Get CSRF token
    get_response = authenticated_client.get("/admin/self-monitoring")
    csrf_token = web_infrastructure.extract_csrf_token(get_response.text)

    # Submit form with enabled=OFF (omit "enabled" field - unchecked checkbox)
    form_data = {
        "csrf_token": csrf_token,
        # "enabled" field NOT included (checkbox unchecked)
        "cadence_minutes": "60",
        "model": "opus",
        "prompt_template": "",
    }

    response = authenticated_client.post("/admin/self-monitoring", data=form_data)

    # Should succeed
    assert response.status_code == status.HTTP_200_OK

    # Verify: service.stop() was called
    mock_service.stop.assert_called_once()
    mock_service.start.assert_not_called()
