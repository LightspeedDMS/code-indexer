"""SCIP queries open only a repository's own project databases.

A SCIP build that indexed dependency packages leaves one ``index.scip.db`` per
package under ``node_modules``. Opening every one of them made a query on such a
repository run for minutes, so queries skip databases under dependency directories.
"""

from cidx_scip.server.query_service import SCIPQueryService


def _db(scip_dir, relative):
    path = scip_dir / relative / "index.scip.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def test_databases_under_node_modules_are_not_queried(tmp_path):
    scip_dir = tmp_path / ".code-indexer" / "scip"
    own = _db(scip_dir, "ui")
    _db(scip_dir, "ui/node_modules/left-pad")
    _db(scip_dir, "ui/node_modules/@scope/inner")

    found = SCIPQueryService._contained_scip_files(tmp_path, scip_dir)

    assert found == [own]


def test_a_project_directory_merely_named_like_a_dependency_is_kept(tmp_path):
    scip_dir = tmp_path / ".code-indexer" / "scip"
    kept = _db(scip_dir, "my_node_modules_tool")

    found = SCIPQueryService._contained_scip_files(tmp_path, scip_dir)

    assert found == [kept]
