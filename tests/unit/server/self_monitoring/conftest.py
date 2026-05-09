import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _disable_pace_maker_guard():
    with patch("code_indexer.server.services.claude_invoker.enforce_pace_maker_config"):
        with patch(
            "code_indexer.server.services.research_assistant_service.enforce_pace_maker_config"
        ):
            yield
