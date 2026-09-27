"""Bug #1979 (codex review finding, turn 17): the server-side guard added in
turn 16 lives entirely in ActivatedRepoIndexManager. Standalone `cidx index
--clear` -- which the mission's own Definition of Done explicitly requires
coverage for -- never calls that manager, so a configured secondary
provider whose health check fails during a standalone `--clear` still lets
the CLI exit 0. This is a REAL end-to-end reproduction of that exact gap
(real repo, real `cidx index --clear` subprocess; only the Voyage HTTP
call is faked deterministic, Cohere's health check genuinely fails against
an invalid placeholder key) -- not a guess.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_cli_clear_fails_when_a_configured_provider_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_embeddings(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * 1023 for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_embeddings\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(child_bootstrap), str(project_src)))
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    # A syntactically-present but genuinely invalid Cohere key: this reaches
    # the REAL authenticating health_check(test_api=True) probe in cli.py's
    # secondary-provider loop and fails it for real -- not a missing-key
    # skip (already covered elsewhere), a health-check-failure skip.
    monkeypatch.setenv("CO_API_KEY", "invalid-placeholder-key")
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout

    config_path = repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["embedding_providers"] = ["voyage-ai", "cohere"]
    config_path.write_text(json.dumps(config))

    result = subprocess.run(
        ["cidx", "index", "--clear", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0, (
        "clear=true skipped a configured provider (cohere health check "
        f"failure) but the CLI still exited 0.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
