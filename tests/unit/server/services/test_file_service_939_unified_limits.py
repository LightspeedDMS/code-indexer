"""
Unit tests for Bug #939: FileService unified content limits.

Tests that FileService.get_file_content() enforces ContentLimitsConfig.file_content_max_tokens
(default 50000) instead of the deleted FileContentLimitsConfig.max_tokens_per_request (5000).

RED phase: These tests fail until FileService._get_file_content_limits_config() is migrated
to read from ContentLimitsConfig.
"""

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import (
    get_config_service,
    reset_config_service,
)
from code_indexer.server.services.file_service import FileListingService

# ContentLimitsConfig default — must NOT be the old 5 K FileContentLimitsConfig default
DEFAULT_MAX_TOKENS = 50_000
# Custom cap used in the direct-attribute assertion test
CUSTOM_MAX_TOKENS = 30_000
# Standard chars-per-token ratio for source code (used in derived-value and truncation tests)
DEFAULT_CHARS_PER_TOKEN = 4
# High cap: proves the old 5 K legacy limit is not applied (a file inside 100 K must not be truncated)
HIGH_MAX_TOKENS = 100_000
# Chars per line in generated test files including the trailing newline
_LINE_WIDTH = 50
# Factor to produce a file larger than a given token cap (2x max_chars)
_FILE_SIZE_MULTIPLIER = 2


def _make_file(repo_path: Path, num_chars: int) -> Path:
    """Write a file whose content is exactly num_chars characters and return its path."""
    if repo_path is None:
        raise ValueError("repo_path must not be None")
    if num_chars < 0:
        raise ValueError("num_chars must be non-negative")

    line = "x" * (_LINE_WIDTH - 1) + "\n"
    full_lines, remainder = divmod(num_chars, _LINE_WIDTH)
    content = line * full_lines + "x" * remainder
    target = repo_path / "test_file.py"
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture()
def config_dir(tmp_path):
    """Isolated CIDX_DATA_DIR so each test gets a fresh config service."""
    cfg = tmp_path / "cidx_config"
    cfg.mkdir()
    return cfg


@pytest.fixture()
def file_service(config_dir, monkeypatch):
    """FileListingService wired to an isolated config service."""
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(config_dir))
    reset_config_service()
    svc = FileListingService()
    yield svc
    reset_config_service()


@pytest.fixture()
def repo_path(tmp_path):
    """Empty directory used as a fake repository root."""
    p = tmp_path / "test_repo"
    p.mkdir()
    return p


def test_default_content_limits_max_tokens_is_50000(file_service):
    """ContentLimitsConfig.file_content_max_tokens defaults to 50000, not the old 5000."""
    cfg = get_config_service().get_config().content_limits_config
    assert cfg is not None
    assert cfg.file_content_max_tokens == DEFAULT_MAX_TOKENS


def test_file_service_reads_content_limits_config_max_tokens(file_service):
    """_get_file_content_limits_config().max_tokens_per_request reflects ContentLimitsConfig value."""
    content_limits = get_config_service().get_config().content_limits_config
    assert content_limits is not None
    content_limits.file_content_max_tokens = CUSTOM_MAX_TOKENS

    limits = file_service._get_file_content_limits_config()

    assert limits.max_tokens_per_request == CUSTOM_MAX_TOKENS, (
        f"Expected {CUSTOM_MAX_TOKENS} from ContentLimitsConfig.file_content_max_tokens, "
        f"got {limits.max_tokens_per_request}. "
        "FileService must read ContentLimitsConfig, not the deleted FileContentLimitsConfig."
    )


# 25 K chars > old 5K-token/20K-char cap, yet well inside 100K-token/400K-char cap.
# A file of this size must NOT be truncated when ContentLimitsConfig says 100K tokens.
_ABOVE_LEGACY_CHARS = 25_000


@pytest.mark.parametrize(
    "max_tokens,file_chars,expect_truncated",
    [
        (
            CUSTOM_MAX_TOKENS,
            CUSTOM_MAX_TOKENS * DEFAULT_CHARS_PER_TOKEN * _FILE_SIZE_MULTIPLIER,
            True,
        ),
        (
            DEFAULT_MAX_TOKENS,
            DEFAULT_MAX_TOKENS * DEFAULT_CHARS_PER_TOKEN * _FILE_SIZE_MULTIPLIER,
            True,
        ),
        (HIGH_MAX_TOKENS, _ABOVE_LEGACY_CHARS, False),
    ],
    ids=["cap-30k", "cap-50k-default", "cap-100k-no-legacy"],
)
def test_truncation_enforced_at_content_limits_boundary(
    file_service, repo_path, max_tokens, file_chars, expect_truncated
):
    """Content is truncated at ContentLimitsConfig.file_content_max_tokens; old 5K cap does not apply."""
    content_limits = get_config_service().get_config().content_limits_config
    assert content_limits is not None
    content_limits.file_content_max_tokens = max_tokens
    content_limits.chars_per_token = DEFAULT_CHARS_PER_TOKEN

    test_file = _make_file(repo_path, file_chars)
    result = file_service.get_file_content_by_path(
        repo_path=str(repo_path),
        file_path=test_file.name,
        offset=None,
        limit=None,
    )

    max_chars = max_tokens * DEFAULT_CHARS_PER_TOKEN
    metadata = result["metadata"]
    if expect_truncated:
        assert len(result["content"]) <= max_chars
        assert metadata["truncated"] is True
        assert metadata["max_tokens_per_request"] == max_tokens
    else:
        assert metadata["truncated"] is False
        assert metadata["max_tokens_per_request"] == max_tokens
