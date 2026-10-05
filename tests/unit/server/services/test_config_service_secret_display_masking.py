"""``ConfigService.get_all_settings()`` (the Config page's data) shows stored
API keys only as a mask revealing at most their last 4 characters
(finding 058). Real ConfigService over a temp server directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.utils.credential_redaction import DISPLAY_MASK_CHAR

MASK = DISPLAY_MASK_CHAR * 8

# Neutral sample values (never real credentials).
ANTHROPIC_KEY = "sk-ant-example-sample-value-0000" + "An7c"
VOYAGE_KEY = "pa-example-sample-value-0000" + "Vo5g"
COHERE_KEY = "co-example-sample-value-0000" + "Ch2r"
LLM_CREDS_KEY = "lcp-example-sample-value-0000" + "Lc4p"
CODEX_KEY = "sk-example-codex-sample-value-0000" + "Cx6d"


@pytest.fixture
def service(tmp_path: Path) -> ConfigService:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    return ConfigService(server_dir_path=str(server_dir))


def _assert_masked(shown: object, key: str) -> None:
    assert shown == MASK + key[-4:], shown
    assert key[:6] not in str(shown)


def test_claude_cli_api_keys_show_only_last_four(service: ConfigService) -> None:
    claude = service.get_config().claude_integration_config
    assert claude is not None
    claude.anthropic_api_key = ANTHROPIC_KEY
    claude.voyageai_api_key = VOYAGE_KEY
    claude.cohere_api_key = COHERE_KEY
    claude.llm_creds_provider_api_key = LLM_CREDS_KEY

    shown = service.get_all_settings()["claude_cli"]

    _assert_masked(shown["anthropic_api_key"], ANTHROPIC_KEY)
    _assert_masked(shown["voyageai_api_key"], VOYAGE_KEY)
    _assert_masked(shown["cohere_api_key"], COHERE_KEY)
    _assert_masked(shown["llm_creds_provider_api_key"], LLM_CREDS_KEY)


def test_unset_claude_cli_api_keys_stay_none(service: ConfigService) -> None:
    claude = service.get_config().claude_integration_config
    assert claude is not None
    claude.anthropic_api_key = None
    claude.voyageai_api_key = None
    claude.cohere_api_key = None
    claude.llm_creds_provider_api_key = ""

    shown = service.get_all_settings()["claude_cli"]

    assert shown["anthropic_api_key"] is None
    assert shown["voyageai_api_key"] is None
    assert shown["cohere_api_key"] is None
    assert shown["llm_creds_provider_api_key"] is None


def test_codex_api_key_shows_only_last_four(service: ConfigService) -> None:
    service.update_setting("codex_integration", "api_key", CODEX_KEY)

    _assert_masked(
        service.get_all_settings()["codex_integration"]["api_key"], CODEX_KEY
    )


def test_resubmitted_masked_codex_key_keeps_stored_key(
    service: ConfigService,
) -> None:
    service.update_setting("codex_integration", "api_key", CODEX_KEY)
    masked = service.get_all_settings()["codex_integration"]["api_key"]

    service.update_setting("codex_integration", "api_key", masked)

    codex = service.get_config().codex_integration_config
    assert codex is not None
    assert codex.api_key == CODEX_KEY
