"""Repository URLs are masked in log output (GitLab provider).

Each logging path is driven with a URL whose userinfo carries a secret
placeholder; the message is still logged, and the logged text never holds
the secret. No path exercised here makes a network call.
"""

import logging
from typing import Any, Dict, List, cast

import pytest

from code_indexer.server.services.git_url_normalizer import GitUrlNormalizer
from code_indexer.server.services.repository_providers.gitlab_provider import (
    GitLabProvider,
)

SECRET = "s3cr3t-value"
# Parseable as a remote URL but without a project path.
NO_PROJECT_URL = f"https://example-user:{SECRET}@gitlab.example.com"
# Not normalizable (invalid port).
UNNORMALIZABLE_URL = f"https://example-user:{SECRET}@gitlab.example.com:bad/g/p.git"
LOGGER = "code_indexer.server.services.repository_providers.gitlab_provider"


class _GoldenRepoListing:
    """Golden-repo listing returning fixed rows."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def list_golden_repos(self) -> List[Dict[str, Any]]:
        return self._rows


def _provider(rows: List[Dict[str, Any]]) -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._url_normalizer = GitUrlNormalizer()
    # The fake implements only list_golden_repos, the one method these
    # paths call, so it is not a GoldenRepoManager subtype.
    provider._golden_repo_manager = cast(Any, _GoldenRepoListing(rows))
    return provider


def _assert_logged_without_secret(caplog: pytest.LogCaptureFixture) -> None:
    records = [r for r in caplog.records if r.name == LOGGER]
    assert records, "the log line under test was not emitted"
    for record in records:
        assert SECRET not in record.getMessage()


def test_enrich_unresolvable_url_is_masked_in_log_output(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    result = _provider([]).enrich_repositories([NO_PROJECT_URL])

    assert result == {}
    _assert_logged_without_secret(caplog)


def test_unnormalizable_indexed_url_is_masked_in_log_output(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    provider = _provider([{"repo_url": UNNORMALIZABLE_URL}])
    indexed = provider._get_indexed_canonical_urls()

    assert indexed == set()
    _assert_logged_without_secret(caplog)


def test_unnormalizable_url_in_indexed_check_is_masked_in_log_output(
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    indexed = _provider([])._is_repo_indexed(
        UNNORMALIZABLE_URL, UNNORMALIZABLE_URL, set()
    )

    assert indexed is False
    _assert_logged_without_secret(caplog)
