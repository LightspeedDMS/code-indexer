"""Smoke test guarding the version constant format.

CI auto-creates the git tag from `code_indexer.__version__`. A malformed
version string (e.g. `10.73`, `10.73.0-dev`, `v10.73.0`) would either skip
tag creation or produce an invalid tag. This test pins the format.
"""

import re

import code_indexer


def test_version_is_semver():
    """__version__ must match strict MAJOR.MINOR.HOTFIX numeric format."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", code_indexer.__version__), (
        f"__version__={code_indexer.__version__!r} is not strict MAJOR.MINOR.HOTFIX"
    )
