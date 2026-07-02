"""
Tests verifying that legacy server-paged discovery symbols are absent (Story #754).

Scenario 7: Old server-paged discovery routes and helpers are removed.
These symbols must no longer exist after the Story #754 refactor:
  - _run_fill_loop
  - _FILL_SAFETY_CAP
  - _collect_unindexed_from_batch
  - _encode_cursor
  - _decode_cursor
  - _decode_cursor_payload
  - _validate_cursor_metadata
  - _extract_cursor_fields

Symbols are checked at both the class level AND the module level to catch
module-scope constants/helpers as well as instance/class methods.

RED phase: currently all these symbols exist, so all tests should fail.
"""

import importlib
import pytest


# ---------------------------------------------------------------------------
# Parameterization values
# ---------------------------------------------------------------------------

_LEGACY_SYMBOLS = [
    "_run_fill_loop",
    "_FILL_SAFETY_CAP",
    "_collect_unindexed_from_batch",
    "_encode_cursor",
    "_decode_cursor",
    "_decode_cursor_payload",
    "_validate_cursor_metadata",
    "_extract_cursor_fields",
]

_PROVIDER_MODULE_AND_CLASS = [
    (
        "code_indexer.server.services.repository_providers.gitlab_provider",
        "GitLabProvider",
    ),
    (
        "code_indexer.server.services.repository_providers.github_provider",
        "GitHubProvider",
    ),
]

# Build cartesian product of (module_path, class_name, symbol)
_PARAMS = [
    (mod_path, cls_name, symbol)
    for mod_path, cls_name in _PROVIDER_MODULE_AND_CLASS
    for symbol in _LEGACY_SYMBOLS
]

_IDS = [
    f"{cls_name}.{symbol}"
    for mod_path, cls_name in _PROVIDER_MODULE_AND_CLASS
    for symbol in _LEGACY_SYMBOLS
]


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod_path,cls_name,symbol", _PARAMS, ids=_IDS)
def test_legacy_symbol_absent_on_class(mod_path, cls_name, symbol):
    """Legacy symbol must not be an attribute of the provider class."""
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    assert not hasattr(cls, symbol), (
        f"{symbol!r} still present on {cls_name} — must be deleted for Story #754"
    )


@pytest.mark.parametrize("mod_path,cls_name,symbol", _PARAMS, ids=_IDS)
def test_legacy_symbol_absent_in_module(mod_path, cls_name, symbol):
    """Legacy symbol must not be a name in the provider module namespace."""
    mod = importlib.import_module(mod_path)
    assert not hasattr(mod, symbol), (
        f"{symbol!r} still present in {mod_path} module namespace — must be deleted for Story #754"
    )
