from __future__ import annotations

import json
from unittest.mock import MagicMock


def _make_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.golden_repo_exists.return_value = False
    mgr.register_local_repo.return_value = True
    return mgr


class TestBootstrapCidxMetaSanitizesPythonPath:
    def test_relative_pythonpath_with_shadowing_package_still_initialises(
        self, monkeypatch, tmp_path
    ):
        from code_indexer.server.startup.bootstrap import bootstrap_cidx_meta

        cidx_meta_path = tmp_path / "cidx-meta"
        shadow = cidx_meta_path / "src" / "click"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text("raise ImportError('shadowed click')\n")
        monkeypatch.setenv("PYTHONPATH", "./src")

        bootstrap_cidx_meta(_make_manager(), str(tmp_path))

        config = json.loads(
            (cidx_meta_path / ".code-indexer" / "config.json").read_text()
        )
        assert config["codebase_dir"] == str(cidx_meta_path.resolve())
