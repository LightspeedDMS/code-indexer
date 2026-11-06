"""EVO-65484: multimodal (image) indexing is env-gated so deployments that never
issue image queries can stop the pure-waste voyage-multimodal-3 vendor spend."""

import os
from unittest import mock

from code_indexer.services.high_throughput_processor import multimodal_indexing_enabled


class TestMultimodalIndexingGate:
    def test_default_enabled_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert multimodal_indexing_enabled() is True

    def test_enabled_when_true(self):
        with mock.patch.dict(os.environ, {"CIDX_MULTIMODAL_INDEXING_ENABLED": "true"}):
            assert multimodal_indexing_enabled() is True

    def test_disabled_when_false(self):
        with mock.patch.dict(os.environ, {"CIDX_MULTIMODAL_INDEXING_ENABLED": "false"}):
            assert multimodal_indexing_enabled() is False

    def test_disabled_case_insensitive(self):
        with mock.patch.dict(os.environ, {"CIDX_MULTIMODAL_INDEXING_ENABLED": "FALSE"}):
            assert multimodal_indexing_enabled() is False

    def test_only_false_disables(self):
        # any non-"false" value keeps the default-on behavior
        with mock.patch.dict(os.environ, {"CIDX_MULTIMODAL_INDEXING_ENABLED": "0"}):
            assert multimodal_indexing_enabled() is True
