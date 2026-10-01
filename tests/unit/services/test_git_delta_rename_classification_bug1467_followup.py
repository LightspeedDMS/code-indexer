"""Codex round-6 MEDIUM finding (follow-up to Bug #1467): rename records
are still filtered incorrectly in ONE case.

`_get_git_deltas_since_commit` (smart_indexer.py) reads `file_path =
parts[1]` and applies the common `_should_index_file(file_path)` exclude
check BEFORE it even looks at whether the record is a rename (`status.
startswith("R")`). For a rename, `parts[1]` is the OLD path -- so if the
OLD path is excluded (e.g. a `.jar` binary extension) but the NEW path is
genuinely included (e.g. a `.py` source file), the early `continue` drops
the WHOLE line before the rename-specific branch (which already correctly
classifies `old_path`/`new_path` independently) is ever reached. The file
that should now be indexed at its new path is silently skipped.

Fix: classify rename records' old and new paths independently, before the
common exclude filter, not after.

Real git repos in temp dirs (mirrors test_git_delta_filtering.py's
established pattern) -- no mocking of git or the filtering logic under
test.
"""

from pathlib import Path

from .incremental_filter_helpers import (
    commit_files,
    get_current_commit,
    init_repo_with_indexer,
    make_source_content,
)


class TestGitDeltaRenameFromExcludedToIncludedPath:
    def test_rename_from_excluded_old_path_to_included_new_path_still_indexes_new_path(
        self, tmp_path: Path
    ) -> None:
        indexer, _ = init_repo_with_indexer(tmp_path)

        # A large, repeated content body so git's default rename-detection
        # similarity threshold is comfortably exceeded even though the
        # file extension changes across the rename.
        body = make_source_content("java") * 20

        commit_files(tmp_path, {"data/module.jar": body}, "add excluded jar")
        old_commit = get_current_commit(tmp_path)

        # Simulate `git mv data/module.jar src/module.py`: delete the old
        # (excluded) path, write the new (included) path with the SAME
        # content, commit both together so git's rename detector fires.
        (tmp_path / "data" / "module.jar").unlink()
        commit_files(tmp_path, {"src/module.py": body}, "rename jar to py")
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)

        assert "src/module.py" in delta.added, (
            "Bug: a rename from an EXCLUDED old path (data/module.jar) "
            "to an INCLUDED new path (src/module.py) was skipped "
            "entirely -- the common exclude-filter check on parts[1] "
            "(the OLD path for a rename record) ran BEFORE recognizing "
            "the record as a rename, so the whole line was dropped via "
            "'continue' before the rename-specific old/new independent "
            "classification could ever run. delta=" + repr(delta)
        )
        assert "data/module.jar" not in (delta.deleted + delta.added), (
            "The excluded old path must never appear as a tracked "
            "deletion either -- it was never indexed in the first place."
        )
