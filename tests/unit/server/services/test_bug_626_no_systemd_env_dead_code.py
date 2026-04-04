"""
Regression tests for Bug #626: Dead code removal from ApiKeySyncService.

Verifies that ApiKeySyncService no longer contains the dead
_update_systemd_env_file() method, _systemd_env_path field, or
systemd_env_path constructor parameter that used to write to
/etc/cidx-server/env (a file nothing ever read).

Also verifies seed_api_keys_on_startup() has no systemd_env_path parameter.

These tests act as regression guards — they will fail if the dead code
is ever re-introduced.
"""

from __future__ import annotations

import inspect
import types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_source(mod: types.ModuleType) -> str:
    """Return the full source text of a module."""
    return inspect.getsource(mod)


def _assert_not_in_module_source(mod: types.ModuleType, needle: str) -> None:
    """Assert that needle does not appear in the module source."""
    source = _module_source(mod)
    assert needle not in source, (
        f"{needle!r} must not appear in {mod.__name__} (Bug #626 dead code removal)"
    )


# ---------------------------------------------------------------------------
# TestApiKeySyncServiceNoSystemdEnvDeadCode
# ---------------------------------------------------------------------------


class TestApiKeySyncServiceNoSystemdEnvDeadCode:
    """ApiKeySyncService must not contain systemd env file dead code."""

    def test_no_update_systemd_env_file_method(self):
        """_update_systemd_env_file must not exist — it was dead code."""
        from code_indexer.server.services.api_key_management import ApiKeySyncService

        assert not hasattr(ApiKeySyncService, "_update_systemd_env_file"), (
            "_update_systemd_env_file is dead code (Bug #626): /etc/cidx-server/env "
            "is never read by systemd, the server, or any script."
        )

    def test_no_systemd_env_path_field_on_instance(self, tmp_path):
        """_systemd_env_path must not exist on a real ApiKeySyncService instance."""
        from code_indexer.server.services.api_key_management import ApiKeySyncService

        svc = ApiKeySyncService(claude_config_path=str(tmp_path / "claude.json"))
        assert not hasattr(svc, "_systemd_env_path"), (
            "_systemd_env_path is dead code (Bug #626): the field stored the "
            "path to /etc/cidx-server/env which nothing ever read."
        )

    def test_no_systemd_env_path_constructor_parameter(self):
        """__init__ must not accept systemd_env_path parameter."""
        from code_indexer.server.services.api_key_management import ApiKeySyncService

        sig = inspect.signature(ApiKeySyncService.__init__)
        param_names = list(sig.parameters.keys())
        assert "systemd_env_path" not in param_names, (
            f"systemd_env_path constructor param is dead code (Bug #626). "
            f"Current params: {param_names}"
        )

    def test_constructor_accepts_only_expected_parameters(self):
        """__init__ should only accept self and claude_config_path."""
        from code_indexer.server.services.api_key_management import ApiKeySyncService

        sig = inspect.signature(ApiKeySyncService.__init__)
        param_names = [p for p in sig.parameters.keys() if p != "self"]
        assert param_names == ["claude_config_path"], (
            f"ApiKeySyncService.__init__ has unexpected params: {param_names}. "
            f"Expected only ['claude_config_path']."
        )

    def test_no_app_general_043_error_code_in_module(self):
        """APP-GENERAL-043 error code must not appear in api_key_management module."""
        import code_indexer.server.services.api_key_management as mod

        _assert_not_in_module_source(mod, "APP-GENERAL-043")

    def test_no_app_general_044_error_code_in_module(self):
        """APP-GENERAL-044 error code must not appear in api_key_management module."""
        import code_indexer.server.services.api_key_management as mod

        _assert_not_in_module_source(mod, "APP-GENERAL-044")

    def test_no_etc_cidx_server_env_path_reference(self):
        """The path /etc/cidx-server/env must not appear in api_key_management module."""
        import code_indexer.server.services.api_key_management as mod

        _assert_not_in_module_source(mod, "/etc/cidx-server/env")


# ---------------------------------------------------------------------------
# TestSeedApiKeysOnStartupNoSystemdEnvParameter
# ---------------------------------------------------------------------------


class TestSeedApiKeysOnStartupNoSystemdEnvParameter:
    """seed_api_keys_on_startup must not accept systemd_env_path parameter."""

    def test_no_systemd_env_path_parameter(self):
        """seed_api_keys_on_startup must not accept systemd_env_path."""
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        sig = inspect.signature(seed_api_keys_on_startup)
        param_names = list(sig.parameters.keys())
        assert "systemd_env_path" not in param_names, (
            f"systemd_env_path param in seed_api_keys_on_startup is dead code "
            f"(Bug #626). Current params: {param_names}"
        )

    def test_function_accepts_only_expected_parameters(self):
        """seed_api_keys_on_startup should only accept config_service and claude_config_path."""
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        sig = inspect.signature(seed_api_keys_on_startup)
        param_names = list(sig.parameters.keys())
        assert param_names == ["config_service", "claude_config_path"], (
            f"seed_api_keys_on_startup has unexpected params: {param_names}. "
            f"Expected ['config_service', 'claude_config_path']."
        )

    def test_no_systemd_env_path_reference_in_seeding_module(self):
        """systemd_env_path must not appear anywhere in api_key_seeding module."""
        import code_indexer.server.startup.api_key_seeding as mod

        _assert_not_in_module_source(mod, "systemd_env_path")
