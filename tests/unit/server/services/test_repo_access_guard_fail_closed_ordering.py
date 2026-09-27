"""Regression test for a fail-closed-ordering defect in
repo_access_guard.require_repo_access():
the function checked "aliases is None/empty" BEFORE
checking "access_filtering_service is None", so a caller supplying an
empty/None alias list together with a missing access_filtering_service
silently returned (no exception) instead of failing closed.

This shared helper backs every one of this track's fixes (003, 020, 021,
022, 023, 012), so the ordering fix here applies to all of them at once --
no per-route change is needed, only this one function.

Availability must be checked FIRST, unconditionally, before any early-return
for empty/missing aliases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    RepoAccessDeniedError,
    require_repo_access,
)


class TestServiceAvailabilityCheckedBeforeEmptyAliasShortCircuit:
    def test_none_aliases_with_missing_service_fails_closed(self):
        """aliases=None must NOT bypass the missing-service fail-closed check."""
        with pytest.raises(AccessFilteringServiceUnavailableError):
            require_repo_access(None, "some_user", None)

    def test_empty_list_aliases_with_missing_service_fails_closed(self):
        """An empty list (e.g. repository_alias: []) must NOT bypass the
        missing-service fail-closed check either --
        an explicit empty list combined
        with an unavailable access service must never resolve to success."""
        with pytest.raises(AccessFilteringServiceUnavailableError):
            require_repo_access(None, "some_user", [])

    def test_list_of_empty_strings_with_missing_service_fails_closed(self):
        """A list containing only empty/non-string entries also must not
        bypass the availability check (mirrors the same non_empty-filter
        code path as the plain empty-list case)."""
        with pytest.raises(AccessFilteringServiceUnavailableError):
            require_repo_access(None, "some_user", ["", None])  # type: ignore[list-item]

    def test_none_aliases_with_available_service_still_returns_none(self):
        """Once the service IS available, aliases=None remains a legitimate
        'nothing to check' case (unchanged from prior behavior) -- this
        proves the reordering did not turn every no-repo-param call into a
        hard failure."""
        service = MagicMock()
        service.is_admin_user.return_value = False
        # Must complete without raising (require_repo_access returns None).
        require_repo_access(service, "some_user", None)
        service.get_accessible_repos.assert_not_called()

    def test_empty_list_with_available_service_still_returns_none(self):
        service = MagicMock()
        service.is_admin_user.return_value = False
        # Must complete without raising (require_repo_access returns None).
        require_repo_access(service, "some_user", [])
        service.get_accessible_repos.assert_not_called()

    def test_denied_alias_with_available_service_still_raises_denied(self):
        """Reordering must not affect the ordinary denial path."""
        service = MagicMock()
        service.is_admin_user.return_value = False
        service.get_accessible_repos.return_value = {"other-repo"}
        with pytest.raises(RepoAccessDeniedError):
            require_repo_access(service, "some_user", "example-repo")
