"""Bug #1871 ALSO IN SCOPE item 2: cidx-meta's override-YAML force-exclude
patching must also cover the legacy lease directory and the exact temp-file
shape lease renewal creates -- a backstop only (relocation is the real
fix), but today's backstop is already incomplete.

`*.tmp` (the current, only entry) never matches
`<hash>-<lease_id>.tmp.<uuid4().hex>` -- `Path.with_suffix()` replaces only
the trailing `.json` suffix with `.tmp.<hex>` (see
``SnapshotReaderLease.renew()``, ``snapshot_reader_lease.py:91``), so the
temp file's basename ends in ``.tmp.<32 lowercase hex chars>``, never
literally ``.tmp``. ``*.tmp`` also never excludes the legacy lease
DIRECTORY itself, so a directory-name backstop is needed too.

Placed under ``tests/unit/global_repos/`` (an owned test directory) rather
than ``tests/unit/server/startup/`` (this bug's owned test directories
don't include the latter; see negotiation turns 3-5) -- pytest does not
require a test file's location to mirror the module under test.
"""

from __future__ import annotations

import fnmatch
import uuid
from typing import Dict, List

from code_indexer.server.startup.bootstrap import (
    _CIDX_META_REQUIRED_EXCLUDE_DIRS,
    _CIDX_META_REQUIRED_FORCE_EXCLUDE_PATTERNS,
    _apply_cidx_meta_excludes,
)

#: The exact shape SnapshotReaderLease.renew() produces:
#: ``self._path.with_suffix(f".tmp.{uuid.uuid4().hex}")`` replaces only
#: the trailing ``.json`` suffix, so the temp file's basename ends in
#: ``.tmp.<32 lowercase hex chars>``, never literally ``.tmp``
#: (snapshot_reader_lease.py:91, 137).
_SAMPLE_LEASE_RENEWAL_TMP_FILENAME = (
    f"deadbeef-hostname-123-{uuid.uuid4().hex}.tmp.{uuid.uuid4().hex}"
)


def test_required_exclude_dirs_include_the_legacy_lease_directory() -> None:
    """RED on unmodified code: `_CIDX_META_REQUIRED_EXCLUDE_DIRS` is
    exactly `(".locks",)` today and never mentions the legacy
    `.snapshot-reader-leases` directory (Bug #1871 backstop exclusion)."""
    assert ".snapshot-reader-leases" in _CIDX_META_REQUIRED_EXCLUDE_DIRS, (
        f"{_CIDX_META_REQUIRED_EXCLUDE_DIRS!r} must include the legacy "
        "lease directory name as a backstop exclude"
    )


def test_required_force_exclude_patterns_match_the_real_lease_renewal_tmp_filename() -> (
    None
):
    """RED on unmodified code: `_CIDX_META_REQUIRED_FORCE_EXCLUDE_PATTERNS`
    is exactly `("*.tmp",)` today, which never matches the actual
    `<hash>-<lease_id>.tmp.<uuid4().hex>` shape lease renewal produces --
    `*.tmp` only matches a basename literally ending in `.tmp`."""
    assert any(
        fnmatch.fnmatch(_SAMPLE_LEASE_RENEWAL_TMP_FILENAME, pattern)
        for pattern in _CIDX_META_REQUIRED_FORCE_EXCLUDE_PATTERNS
    ), (
        f"no pattern in {_CIDX_META_REQUIRED_FORCE_EXCLUDE_PATTERNS!r} "
        f"matches a real lease-renewal temp filename "
        f"({_SAMPLE_LEASE_RENEWAL_TMP_FILENAME!r})"
    )


def test_apply_cidx_meta_excludes_propagates_both_new_entries_into_override_dict() -> (
    None
):
    """Confirms `_apply_cidx_meta_excludes()` -- the function that patches
    the real `.code-indexer-override.yaml` on disk -- actually copies both
    new entries into the override dict, not merely that the module
    constants exist somewhere."""
    data: Dict[str, List[str]] = {}
    changed = _apply_cidx_meta_excludes(data)

    assert changed is True
    assert ".snapshot-reader-leases" in data["add_exclude_dirs"]
    assert any(
        fnmatch.fnmatch(_SAMPLE_LEASE_RENEWAL_TMP_FILENAME, pattern)
        for pattern in data["force_exclude_patterns"]
    ), (
        "the patched override dict's force_exclude_patterns must also match the real tmp filename shape"
    )
