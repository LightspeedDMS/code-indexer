"""A clear must never take a rebuild-from-existing-data shortcut."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "rebuild_flag",
    ("--rebuild-fts-index", "--rebuild-index", "--rebuild-indexes"),
)
def test_cli_rejects_clear_with_rebuild_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rebuild_flag: str
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "example.py").write_text("def example():\n    return 1\n")

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_text_batch(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * (self.get_model_info()['dimensions'] - 1) for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_text_batch\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(child_bootstrap), str(project_src)))
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    indexed = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert indexed.returncode == 0, indexed.stderr or indexed.stdout

    result = subprocess.run(
        ["cidx", "index", "--clear", rebuild_flag],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "--clear" in output and rebuild_flag in output, output
    assert "Cannot use" in output, output
