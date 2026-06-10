"""Unit tests for the canonical versioned-snapshot path predicate (Bug #1084 Phase A1).

`is_versioned_snapshot(path, *, mount_point=None)` is the SINGLE authority for
deciding whether an absolute path is a versioned snapshot. The canonical shape on
every backend is ``<root>/.versioned/{ns}/v_<unix_ts>``. A transition clause
recognizes the legacy cow-daemon shape ``{mount}/{ns}/v_<unix_ts>`` (no
``.versioned`` segment) ONLY when ``mount_point`` is supplied.
"""

from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot


class TestCanonicalShape:
    """Canonical ``.../.versioned/{ns}/v_<ts>`` is True on every backend."""

    def test_canonical_local_path_is_snapshot(self):
        path = "/data/golden-repos/.versioned/my-repo/v_1700000000"
        assert is_versioned_snapshot(path) is True

    def test_canonical_cow_mount_path_is_snapshot(self):
        path = "/mnt/cow-storage/.versioned/langfuse_x/v_1749523200"
        assert is_versioned_snapshot(path) is True

    def test_canonical_path_with_mount_point_still_true(self):
        path = "/mnt/cow-storage/.versioned/repo/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is True

    def test_trailing_slash_tolerated(self):
        path = "/data/golden-repos/.versioned/repo/v_1700000000/"
        assert is_versioned_snapshot(path) is True


class TestCanonicalRejections:
    """Canonical clause rejects non-snapshot shapes."""

    def test_leaf_not_v_timestamp_is_false(self):
        # leaf is not v_<digits>
        path = "/data/golden-repos/.versioned/repo/main"
        assert is_versioned_snapshot(path) is False

    def test_v_prefix_without_digits_is_false(self):
        path = "/data/golden-repos/.versioned/repo/v_notanumber"
        assert is_versioned_snapshot(path) is False

    def test_versioned_segment_but_no_namespace_parent_is_false(self):
        # .versioned directly followed by v_* (missing namespace dir between them)
        path = "/data/golden-repos/.versioned/v_1700000000"
        assert is_versioned_snapshot(path) is False

    def test_master_base_clone_is_false(self):
        # golden-repos/{repo} — the master base clone, NEVER a snapshot
        path = "/data/golden-repos/my-repo"
        assert is_versioned_snapshot(path) is False

    def test_master_base_clone_with_mount_point_is_false(self):
        path = "/data/golden-repos/my-repo"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is False


class TestLegacyTransitionClause:
    """Legacy cow-daemon shape ``{mount}/{ns}/v_<ts>`` recognized ONLY with mount_point."""

    def test_legacy_cow_shape_true_with_mount_point(self):
        path = "/mnt/cow-storage/langfuse_x/v_1749523200"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is True

    def test_legacy_cow_shape_false_without_mount_point(self):
        # Without mount_point we cannot distinguish a legacy snapshot from an
        # arbitrary {parent}/{name} path — must be conservative and return False.
        path = "/mnt/cow-storage/langfuse_x/v_1749523200"
        assert is_versioned_snapshot(path) is False

    def test_legacy_cow_shape_trailing_slash_mount_point(self):
        path = "/mnt/cow-storage/repo/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage/") is True

    def test_activated_repos_subtree_is_false(self):
        # {mount}/activated-repos/{user}/{repo} (Bug #1052) must NOT be a snapshot,
        # even though it lives directly under the mount.
        path = "/mnt/cow-storage/activated-repos/alice/some-repo"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is False

    def test_activated_repos_with_v_leaf_is_false(self):
        # Defense: even a v_*-leafed path under activated-repos must be False.
        path = "/mnt/cow-storage/activated-repos/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is False

    def test_legacy_too_many_parts_is_false(self):
        # {mount}/a/b/v_* has 3 mount-relative parts — not the legacy 2-part shape.
        path = "/mnt/cow-storage/a/b/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is False

    def test_legacy_leaf_not_v_is_false(self):
        path = "/mnt/cow-storage/repo/main"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is False

    def test_path_not_under_mount_point_falls_back_to_canonical(self):
        # A canonical path that is not under the supplied mount_point is still
        # recognized via the canonical clause.
        path = "/data/golden-repos/.versioned/repo/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/cow-storage") is True


class TestOntapFlatShape:
    """ONTAP flat ``{mount}/v_<ts>`` recognized as a snapshot via the legacy clause."""

    def test_ontap_flat_shape_true_with_mount_point(self):
        # ONTAP stores flat single-component v_<ts> directly under the mount.
        path = "/mnt/fsx/v_1700000000"
        assert is_versioned_snapshot(path, mount_point="/mnt/fsx") is True

    def test_ontap_flat_shape_false_without_mount_point(self):
        path = "/mnt/fsx/v_1700000000"
        assert is_versioned_snapshot(path) is False


class TestNoneAndGarbage:
    """None / empty / garbage inputs return False, never raise."""

    def test_none_returns_false(self):
        assert is_versioned_snapshot(None) is False  # type: ignore[arg-type]

    def test_empty_string_returns_false(self):
        assert is_versioned_snapshot("") is False

    def test_garbage_returns_false(self):
        assert is_versioned_snapshot("not/a/real/path") is False

    def test_relative_versioned_path_is_recognized(self):
        # Even a relative path matching the canonical shape is recognized — the
        # predicate operates on path structure, not absoluteness.
        assert is_versioned_snapshot(".versioned/repo/v_1700000000") is True
