"""Regression test: TantivyIndexManager must be importable from the absolute path.

This guards against a recurring bug where inline_query.py used a relative import
`from ..services.tantivy_index_manager` which resolves to
`code_indexer.server.services` — a path that does not exist.

The correct absolute path is `code_indexer.services.tantivy_index_manager`.
"""


def test_tantivy_index_manager_absolute_import() -> None:
    """TantivyIndexManager must be importable from its canonical absolute path."""
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    assert TantivyIndexManager is not None, (
        "TantivyIndexManager must be a real class, not None"
    )


def test_tantivy_index_manager_is_a_class() -> None:
    """TantivyIndexManager must be a class (not a function or module).

    Guards against stub/shim replacements that would make the import succeed
    while silently breaking callers that instantiate TantivyIndexManager.
    """
    import inspect

    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    assert inspect.isclass(TantivyIndexManager), (
        f"TantivyIndexManager must be a class, got {type(TantivyIndexManager)}"
    )


def test_wrong_relative_import_path_does_not_exist() -> None:
    """The erroneous relative resolution `code_indexer.server.services` must not exist.

    If this test fails it means a `code_indexer/server/services/` package
    containing `tantivy_index_manager` was accidentally created, which would
    mask the original bug and hide future regressions.
    """
    import importlib.util

    spec = importlib.util.find_spec(
        "code_indexer.server.services.tantivy_index_manager"
    )
    assert spec is None, (
        "code_indexer.server.services.tantivy_index_manager must NOT exist; "
        "the canonical location is code_indexer.services.tantivy_index_manager"
    )
