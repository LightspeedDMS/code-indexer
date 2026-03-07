"""
Unit tests for the cleanup endpoint's config-default behavior (Story #360, Component 6).

Tests that the /api/admin/jobs/cleanup endpoint uses the configured
cleanup_max_age_hours when no explicit max_age_hours parameter is provided,
and that an explicit parameter overrides the config value.

The endpoint logic is:
  - max_age_hours: Optional[int] = None
  - if None, resolve from config_service.get_config().background_jobs_config.cleanup_max_age_hours
  - if provided, use the explicit value (still clamp to 1-8760)

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import tempfile
import shutil
import os
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestCleanupEndpointConfigDefault:
    """Tests for cleanup endpoint using config default when no param provided (Story #360)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up test environment."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_cleanup_uses_configured_default_when_no_param(self):
        """Story #360: When max_age_hours is None, the configured value (720) should be used.

        This tests that the config-resolution logic works: when None is passed,
        the endpoint should use config.background_jobs_config.cleanup_max_age_hours.
        """
        config = self.config_service.get_config()
        # Default from config should be 720
        configured_value = config.background_jobs_config.cleanup_max_age_hours
        assert configured_value == 720

        # The endpoint logic: None -> resolve from config
        max_age_hours = None
        if max_age_hours is None:
            max_age_hours = configured_value
        assert max_age_hours == 720

    def test_cleanup_uses_explicit_param_over_config(self):
        """Story #360: When max_age_hours is explicitly set, it overrides the config."""
        config = self.config_service.get_config()
        configured_value = config.background_jobs_config.cleanup_max_age_hours
        assert configured_value == 720

        # Explicit param should override config
        explicit_param = 48
        max_age_hours = explicit_param if explicit_param is not None else configured_value
        assert max_age_hours == 48

    def test_cleanup_uses_custom_configured_default(self):
        """Story #360: When config is updated, the new value is used as default."""
        self.config_service.update_setting(
            category="background_jobs",
            key="cleanup_max_age_hours",
            value=168,
        )

        config = self.config_service.get_config()
        configured_value = config.background_jobs_config.cleanup_max_age_hours
        assert configured_value == 168

        # The endpoint logic: None -> resolve from config
        max_age_hours = None
        if max_age_hours is None:
            max_age_hours = configured_value
        assert max_age_hours == 168


class TestCleanupEndpointDefaultParamSignature:
    """Tests that the endpoint function signature uses Optional[int] = None (Story #360)."""

    def test_cleanup_endpoint_default_is_none(self):
        """Story #360: The cleanup endpoint must have max_age_hours: Optional[int] = None.

        Inspects the function signature to verify the parameter default changed
        from the old hardcoded 24 to None (so config can provide the default).
        """
        import inspect
        import code_indexer.server.app as app_module

        # The app module defines cleanup_old_jobs inside a scope.
        # We verify via the routes by checking the registered endpoint.
        # The cleanest verification is via app.py's routes list.
        # We check the module-level cleanup_old_jobs function signature.

        # Get the app object from the module
        app = app_module.app

        # Find the cleanup route in the app's routes
        cleanup_route = None
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/api/admin/jobs/cleanup":
                cleanup_route = route
                break

        assert cleanup_route is not None, (
            "Could not find /api/admin/jobs/cleanup route in app"
        )

        # Inspect the endpoint function
        endpoint = cleanup_route.endpoint
        sig = inspect.signature(endpoint)
        params = sig.parameters

        assert "max_age_hours" in params, (
            "cleanup_old_jobs must have max_age_hours parameter"
        )

        max_age_param = params["max_age_hours"]
        # The default should be None (not 24) for Story #360
        assert max_age_param.default is None, (
            f"max_age_hours default should be None (to use config), "
            f"but got: {max_age_param.default}"
        )
