"""Whether a user's activated copy is there, as the content seam can tell.

One answer for every route: the copy's directory when the content seam's
implementation reads this pod's disk, else the registry's record of THIS
user's activation (the pod's directory holds index state at most).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.server.services.activated_copy_content import (
    activated_copy_present,
)
from code_indexer.server.startup import content_backend_wiring as backend_wiring


class _Registry:
    def __init__(self, activated: bool) -> None:
        self.activated = activated
        self.asked: List[Tuple[str, str]] = []

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        self.asked.append((username, alias))
        return self.activated


class _Delegated:
    is_local = False

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        raise AssertionError("not asked")

    def fetch(self, repo_path: Any, paths: Any) -> Any:
        raise AssertionError("not asked")


def test_on_this_pod_the_directory_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)
    registry = _Registry(activated=True)

    assert activated_copy_present(registry, "u", "r", tmp_path) is True
    assert activated_copy_present(registry, "u", "r", tmp_path / "gone") is False
    assert registry.asked == []


@pytest.mark.parametrize("activated", [True, False])
def test_elsewhere_the_registry_answers_for_this_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, activated: bool
) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", _Delegated)
    monkeypatch.setattr(
        backend_wiring, "is_pod_local_golden_path", lambda repo_path: False
    )
    registry = _Registry(activated=activated)

    assert activated_copy_present(registry, "u", "r", tmp_path / "gone") is activated
    assert registry.asked == [("u", "r")]
