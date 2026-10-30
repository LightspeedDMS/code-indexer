from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)


# ^ order test_a before test_b; pytest runs a file's tests in definition order
def test_a_leaves_singleton_set():
    MCPSelfRegistrationService.set_instance(
        MCPSelfRegistrationService(config_manager=None, mcp_credential_manager=None)
    )
    assert MCPSelfRegistrationService.get_instance() is not None


def test_b_starts_without_singleton():
    assert MCPSelfRegistrationService.get_instance() is None
