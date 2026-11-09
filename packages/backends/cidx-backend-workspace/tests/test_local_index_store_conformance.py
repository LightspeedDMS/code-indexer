"""The local index store passes the index store seam's conformance suite."""

from __future__ import annotations

import pytest

from cidx_backend_workspace.index_store import LocalIndexStore, make_index_store
from cidx_sdk.seams.index_store import IndexStore
from cidx_testkit.conformance.index_store import IndexStoreContract


class TestLocalIndexStore(IndexStoreContract):
    @pytest.fixture
    def index_store(self) -> IndexStore:
        store = make_index_store()
        assert isinstance(store, LocalIndexStore) and store.is_local is True
        return store
