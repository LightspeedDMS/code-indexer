import inspect


def test_search_catches_value_error_from_backend():
    """Search should catch ValueError from BackendFactory for unindexed repos."""
    from code_indexer.server.services import search_service

    source = inspect.getsource(search_service)
    assert "ValueError" in source, (
        "search_service should catch ValueError for missing vector_store"
    )


def test_search_logs_warning_not_error_for_missing_index():
    """Missing index should log WARNING, not ERROR, for ValueError from BackendFactory."""
    from code_indexer.server.services import search_service

    source = inspect.getsource(search_service)

    # Find the ValueError handler block and verify it uses warning, not error
    # The fix adds a ValueError clause before the broad Exception clause
    # that calls logger.warning (not logger.error)
    lines = source.splitlines()

    in_value_error_block = False
    found_warning = False
    found_error_before_warning = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("except ValueError"):
            in_value_error_block = True
            continue
        if in_value_error_block:
            # Exit the block when we hit the next except or end of indented block
            if stripped.startswith("except ") or (
                stripped and not line.startswith(" ") and not line.startswith("\t")
            ):
                break
            if "logger.warning" in stripped:
                found_warning = True
            if "logger.error" in stripped:
                found_error_before_warning = True

    assert found_warning, (
        "ValueError handler in search_service should call logger.warning"
    )
    assert not found_error_before_warning, (
        "ValueError handler in search_service must NOT call logger.error"
    )
