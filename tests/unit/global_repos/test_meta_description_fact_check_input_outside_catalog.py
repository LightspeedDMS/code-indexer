"""With fact checking enabled, a registered repository's description is
verified from a working copy staged OUTSIDE the cidx-meta tree, so a
backup stage-all or snapshot taken meanwhile never sees an unverified or
partial description; only the verified content is put in place.

Real hook and filesystem; only the Claude boundaries are replaced (the
description generator and the verification pass, which rewrites the
document like the real one).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

REPO = "example-repo"
GENERATED = "# example-repo\n\nGenerated description.\n"
VERIFIED = "# example-repo\n\nVerified description.\n"


@pytest.fixture
def golden(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "golden-repos"
    (root / "cidx-meta").mkdir(parents=True)
    (root / REPO).mkdir()
    meta_description_hook.set_refresh_scheduler(None)
    meta_description_hook.set_debouncer(None)
    meta_description_hook.set_tracking_backend(None)
    meta_description_hook.set_scheduler(None)
    yield root


def test_fact_check_input_is_staged_outside_the_catalog(golden: Path) -> None:
    catalog = golden / "cidx-meta"
    seen: Dict[str, Any] = {}

    def _verify(self: Any, document_path: Path, **kwargs: Any) -> bool:
        seen["path"] = Path(document_path)
        seen["catalog"] = sorted(p.name for p in catalog.iterdir())
        Path(document_path).write_text(VERIFIED, encoding="utf-8")
        return True

    cli_manager = MagicMock()
    cli_manager.check_cli_available.return_value = True
    config = MagicMock()
    config.claude_integration_config.dep_map_fact_check_enabled = True
    config.claude_integration_config.dependency_map_pass_timeout_seconds = 60
    config_service = MagicMock()
    config_service.get_config.return_value = config
    with (
        patch.object(
            meta_description_hook, "get_claude_cli_manager", return_value=cli_manager
        ),
        patch.object(
            meta_description_hook,
            "_generate_repo_description",
            lambda *a, **k: GENERATED,
        ),
        patch.object(DependencyMapAnalyzer, "invoke_verification_pass", _verify),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=config_service,
        ),
    ):
        meta_description_hook.on_repo_added(
            REPO,
            "https://example.com/example-repo.git",
            str(golden / REPO),
            str(golden),
        )

    document: Path = seen["path"]
    assert catalog.resolve() not in document.resolve().parents, (
        "the fact-check input was written inside cidx-meta"
    )
    listing: List[str] = seen["catalog"]
    assert listing == [], f"cidx-meta held {listing} while verification ran"
    assert (catalog / f"{REPO}.md").read_text() == VERIFIED
    assert not document.exists(), "the fact-check input was left behind"
