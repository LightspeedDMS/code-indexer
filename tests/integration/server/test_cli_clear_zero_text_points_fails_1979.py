"""Bug #1979 P2 (Codex review finding): the CLI `--clear` guard
(`cli.py`'s call into `provider_rebuild_check.find_providers_not_rebuilt_since`)
originally checked ONLY provider metadata timestamps. A file that produces
solely multimodal chunks (file_chunking_manager.py: "a file consisting
solely of multimodal chunks legitimately has none [regular points]")
completes successfully, increments `files_processed`, and refreshes the
provider's metadata timestamp -- even though its configured TEXT collection
ends the run with zero points. `clear=true` is documented as "index from
scratch"; a genuinely empty TEXT collection is not a full rebuild, and the
CLI must not exit 0 for it.

Real repo, real `cidx` subprocess; only the external VoyageAI HTTP call is
faked (matches the existing #1979 test pattern). Manually confirmed before
writing this test: a repo containing only a Markdown file whose entire
content is an image reference produces `Chunks indexed: 0` with
`voyage-code-3` (TEXT) at 0 points and `voyage-multimodal-3` at 1 point,
while `cidx index --clear` exits 0 -- reproducing the review's exact
scenario, not a guess.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

# Minimal valid 1x1 red-pixel PNG (neutral, no external dependency).
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII="
)


def test_cli_clear_fails_when_a_file_produces_only_multimodal_points(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    images_dir = repo / "images"
    images_dir.mkdir()
    (images_dir / "pixel.png").write_bytes(_PIXEL_PNG)
    docs_dir = repo / "docs"
    docs_dir.mkdir()
    # This file's ENTIRE content is the image reference -- the text chunker
    # produces zero chunks for it (Amendment 1502/#1979: a file consisting
    # solely of multimodal chunks legitimately has none), while the image
    # extractor still finds the reference and produces one multimodal point.
    (docs_dir / "only_image.md").write_text("![pixel](../images/pixel.png)\n")

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "from code_indexer.services.voyage_multimodal import VoyageMultimodalClient\n"
        "def fake_text_batch(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * (self.get_model_info()['dimensions'] - 1) for _ in texts]\n"
        "def fake_multimodal_batch(self, items, input_type=None):\n"
        "    dims = self.get_model_info()['dimensions']\n"
        "    return [[1.0] + [0.0] * (dims - 1) for _ in items]\n"
        "def fake_multimodal_single(self, text, image_paths, input_type=None):\n"
        "    dims = self.get_model_info()['dimensions']\n"
        "    return [1.0] + [0.0] * (dims - 1)\n"
        "VoyageAIClient.get_embeddings_batch = fake_text_batch\n"
        "VoyageMultimodalClient.get_embeddings_batch = fake_text_batch\n"
        "VoyageMultimodalClient.get_multimodal_embeddings_batch = fake_multimodal_batch\n"
        "VoyageMultimodalClient.get_multimodal_embedding = fake_multimodal_single\n"
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

    result = subprocess.run(
        ["cidx", "index", "--clear", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )

    assert "Chunks indexed: 0" in result.stdout, (
        "Test setup assumption broke -- expected the only file to produce "
        f"zero text chunks.\nstdout:\n{result.stdout}"
    )
    assert result.returncode != 0, (
        "clear=true left the configured provider's TEXT collection with "
        "zero points (only a multimodal point was produced) but the CLI "
        f"still exited 0.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
