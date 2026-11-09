"""The refresh reconcile finds SCIP indexes wherever the build wrote them.

SCIP databases are nested per project (``scip/<project path>/index.scip.db``) for a
multi-project repository. Looking only directly under ``scip/`` recorded such a
repository as having no SCIP index, so the registry said absent for an index that
was there. Databases of dependency packages do not count: queries skip them.

The check is asked of the ``scip`` index builder (the cidx-scip feature's,
modular phase 4d); without the feature there is no answer (None), and the
registry flag is left as it is.
"""

from typing import Any, Iterator, cast

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.unit.server._scip_feature import scip_pipeline_installed


def _has_scip(repo_path):
    # The check reads only the repository path, not scheduler state.
    return RefreshScheduler._has_scip_indexes(cast(Any, None), repo_path)


@pytest.fixture
def scip_builder() -> Iterator[None]:
    with scip_pipeline_installed():
        yield


def _db(scip_dir, relative):
    path = scip_dir / relative / "index.scip.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")


def test_nested_project_database_counts(tmp_path, scip_builder):
    _db(tmp_path / ".code-indexer" / "scip", "code/util/nativeapp")

    assert _has_scip(tmp_path) is True


def test_flat_database_still_counts(tmp_path, scip_builder):
    scip_dir = tmp_path / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    (scip_dir / "index.scip.db").write_bytes(b"")

    assert _has_scip(tmp_path) is True


def test_only_dependency_package_databases_do_not_count(tmp_path, scip_builder):
    _db(tmp_path / ".code-indexer" / "scip", "ui/node_modules/left-pad")

    assert _has_scip(tmp_path) is False


def test_no_scip_directory_means_none(tmp_path, scip_builder):
    assert _has_scip(tmp_path) is False


def test_without_the_scip_feature_there_is_no_answer(tmp_path):
    _db(tmp_path / ".code-indexer" / "scip", "code")

    assert _has_scip(tmp_path) is None
