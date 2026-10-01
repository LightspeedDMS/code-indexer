"""ConfigService rejects colliding temporal_embedders at the Web UI Config
Screen submission boundary (Story #1457 AC6, round-13 Codex N13-1,
defense-in-depth).

sanitize_model_name() is NOT injective, so two DIFFERENTLY-NAMED embedders
can sanitize to the SAME collection slug. The materialization-point guard in
temporal_indexer.py catches this at index-run time, but this is the ONE
server-side boundary where the temporal embedder SET is actually submitted
(the Web UI Config Screen path, per "No Environment Variables for Server
Settings") -- a colliding set must be rejected here too, at config-update
time, rather than silently persisted and only failing later inside a
background index run.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceTemporalEmbeddersSlugCollision:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_colliding_temporal_embedders_rejected_at_config_update_time(self):
        with pytest.raises(ValueError, match="collapse to the same collection slug"):
            self.config_service.update_setting(
                category="indexing",
                key="temporal_embedders",
                value=["collide-a.1457", "collide-a-1457"],
            )
