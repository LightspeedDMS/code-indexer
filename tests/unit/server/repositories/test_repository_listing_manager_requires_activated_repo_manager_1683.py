"""
Bug #1683: `RepositoryListingManager.__init__` fell back to a bare, unwired
`ActivatedRepoManager()` when `activated_repo_manager` was not provided --
the same anti-pattern Bug #1670 fixed in `web/routes.py`.

`ActivatedRepoManager()`'s constructor hardcodes
`Path.home()/".cidx-server"/"data"` and ignores `CIDX_SERVER_DATA_DIR`, so
a silently-constructed fallback instance would read from the WRONG
per-node store. The sole production construction site
(`startup/service_init.py`) always passes an explicit, properly-wired
manager, so this fallback was dead code in production but a landmine for
any future caller. Fixed to fail loudly (`ValueError`) instead of
silently constructing a wrong instance.

This test file exercises the production fix already applied in
`src/code_indexer/server/repositories/repository_listing_manager.py`'s
`RepositoryListingManager.__init__` (raises `ValueError` when
`activated_repo_manager` is `None` instead of falling back to
`ActivatedRepoManager()`).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
)


class TestRepositoryListingManagerRequiresActivatedRepoManager:
    def test_raises_value_error_when_activated_repo_manager_not_provided(
        self,
    ) -> None:
        golden_repo_manager = MagicMock(name="golden-repo-manager")

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager.__init__",
            side_effect=AssertionError(
                "ActivatedRepoManager() must not be constructed directly; "
                "caller must supply a DI-wired instance (Bug #1683)"
            ),
        ):
            with pytest.raises(
                ValueError, match="activated_repo_manager must be provided"
            ):
                RepositoryListingManager(
                    golden_repo_manager=golden_repo_manager,
                    activated_repo_manager=None,
                )

    def test_accepts_explicitly_provided_activated_repo_manager(self) -> None:
        golden_repo_manager = MagicMock(name="golden-repo-manager")
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")

        manager = RepositoryListingManager(
            golden_repo_manager=golden_repo_manager,
            activated_repo_manager=sentinel_manager,
        )

        assert manager.activated_repo_manager is sentinel_manager
