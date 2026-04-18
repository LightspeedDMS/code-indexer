"""
Regression tests for CredentialAPIClient async contracts.

Bug #749 sibling: All public CredentialAPIClient methods called via
run_async() in the CLI must be async coroutines. Plain `def` methods
cause: ValueError: a coroutine was expected, got {...}
"""

import inspect
import pytest


class TestCredentialClientAsyncContracts:
    """Bug #749 sibling: CredentialAPIClient run_async()-called methods must be async."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "list_api_keys",
            "create_api_key",
            "delete_api_key",
            "list_mcp_credentials",
            "create_mcp_credential",
            "delete_mcp_credential",
            "admin_list_user_mcp_credentials",
            "admin_delete_user_mcp_credential",
            "admin_list_all_mcp_credentials",
        ],
    )
    def test_method_is_coroutine(self, method_name: str):
        """Each method called via run_async() must be async def."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        method = getattr(CredentialAPIClient, method_name)
        assert inspect.iscoroutinefunction(method), (
            f"CredentialAPIClient.{method_name} must be async def "
            f"so run_async(client.{method_name}(...)) works (Bug #749 sibling)"
        )
