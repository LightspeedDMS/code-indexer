"""Web admin door: the internal catalog repository is activated only for a
member of the admins group, judged for the TARGET user (never the acting
admin).

Driven through the real Web router (``POST /admin/golden-repos/activate``)
of the audit front-door harness: real auth components, a real
GoldenRepoManager, a real ActivatedRepoManager, a real AccessFilteringService
over a real GroupAccessManager and a real bound audit store. The only
double is the harness's job runner, which records submissions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from _audit_front_doors import ACTING_ADMIN, front_door_env
from _audit_repos_support import EXAMPLE_ALIAS, register_repo
from code_indexer.server.services.constants import CIDX_META_REPO

_TARGET = "example-target-user"
_ACTIVATE = "/admin/golden-repos/activate"


@pytest.fixture()
def doors(tmp_path: Path, monkeypatch) -> Iterator[SimpleNamespace]:
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    for env in front_door_env(tmp_path, monkeypatch):
        register_repo(env.manager, CIDX_META_REPO)
        activated = ActivatedRepoManager(
            data_dir=str(tmp_path / "activated-data"),
            golden_repo_manager=env.manager,
            background_job_manager=env.jobs,  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            app_module.app.state, "activated_repo_manager", activated, raising=False
        )
        yield SimpleNamespace(env=env)


def _activate(env: Any, golden: str, username: str) -> Any:
    return env.web(
        "POST",
        _ACTIVATE,
        data={"golden_alias": golden, "username": username, "user_alias": "copy"},
    )


class TestWebCatalogActivation:
    def test_refused_for_a_target_outside_the_admins_group(self, doors) -> None:
        env = doors.env

        response = _activate(env, CIDX_META_REPO, _TARGET)

        assert env.jobs.submissions == []
        assert "Access denied" in response.text, response.text
        rows = env.rows("user_repo_activated_by_admin")
        assert [(r.actor, r.outcome) for r in rows] == [(ACTING_ADMIN, "failure")]

    def test_submitted_for_an_admins_group_target(self, doors) -> None:
        env = doors.env

        _activate(env, CIDX_META_REPO, ACTING_ADMIN)

        assert [s["username"] for s in env.jobs.submissions] == [ACTING_ADMIN]

    def test_ordinary_repository_for_a_non_admin_target_is_unaffected(
        self, doors
    ) -> None:
        env = doors.env

        _activate(env, EXAMPLE_ALIAS, _TARGET)

        assert [s["username"] for s in env.jobs.submissions] == [_TARGET]
