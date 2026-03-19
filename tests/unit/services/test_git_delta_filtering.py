"""
Group 2: Prove git delta files are not properly filtered by extension.

Because _should_index_file() has the dot mismatch bug (Bug 1), it returns
False for ALL source files — so _get_git_deltas_since_commit() always returns
empty lists even for legitimate .java and .kt files.

These tests use REAL git repos in temp directories and FAIL against current code.

Run:
    PYTHONPATH=src pytest tests/unit/services/test_git_delta_filtering.py \
        -v --tb=short
"""

from pathlib import Path


from .incremental_filter_helpers import (
    PRODUCTION_BINARY_EXTENSIONS,
    commit_files,
    get_current_commit,
    init_repo_with_indexer,
    make_binary_content,
    make_source_content,
)


class TestGitDeltaFilteringWithBinaryJunk:
    """Prove git delta files are not properly filtered by extension.

    _get_git_deltas_since_commit calls _should_index_file for each file.
    Due to Bug 1 (dot mismatch), _should_index_file returns False for ALL files,
    so committed_files is always empty — even for .java and .kt source files.
    """

    def test_git_delta_includes_java_files(self, tmp_path: Path) -> None:
        """Git delta reporting .java as modified MUST include it in committed_files.

        FAILS currently because Bug 1 causes _should_index_file to return False
        for .java files, so they never appear in the delta.
        """
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = get_current_commit(tmp_path)

        commit_files(
            tmp_path,
            {
                "code/src/Main.java": make_source_content("java"),
                "code/src/Utils.java": make_source_content("java"),
            },
            "add java sources",
        )
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        all_delta_files = delta.added + delta.modified
        java_files = [f for f in all_delta_files if f.endswith(".java")]

        assert len(java_files) == 2, (
            f"Bug 1: Expected 2 .java files in git delta but got {len(java_files)}: "
            f"{all_delta_files}. "
            "Root cause: _should_index_file uses path.suffix (WITH dot) "
            "but config.file_extensions stores extensions WITHOUT dot, "
            "so '.java' is never found in ['java', ...] -> all source files rejected."
        )

    def test_git_delta_includes_kotlin_files(self, tmp_path: Path) -> None:
        """Git delta reporting .kt as modified MUST include it in committed_files.

        FAILS currently due to Bug 1 dot mismatch.
        """
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = get_current_commit(tmp_path)

        commit_files(
            tmp_path,
            {
                "code/src/Service.kt": make_source_content("kt"),
                "code/src/Model.kt": make_source_content("kt"),
                "code/src/Controller.kt": make_source_content("kt"),
            },
            "add kotlin sources",
        )
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        all_delta_files = delta.added + delta.modified
        kt_files = [f for f in all_delta_files if f.endswith(".kt")]

        assert len(kt_files) == 3, (
            f"Bug 1: Expected 3 .kt files in git delta but got {len(kt_files)}: "
            f"{all_delta_files}. "
            "Root cause: dot mismatch in _should_index_file."
        )

    def test_git_delta_excludes_jar_files(self, tmp_path: Path) -> None:
        """Git delta reporting .jar as modified must NOT include it in committed_files."""
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = get_current_commit(tmp_path)

        commit_files(
            tmp_path,
            {
                "lib/zelix/ZKM.jar": make_binary_content(),
                "lib/zelix/ZKM.class": make_binary_content(),
                "code/src/Main.java": make_source_content("java"),
            },
            "add jar, class, and java",
        )
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        all_delta_files = delta.added + delta.modified

        jar_files = [f for f in all_delta_files if f.endswith(".jar")]
        class_files = [f for f in all_delta_files if f.endswith(".class")]

        assert jar_files == [], (
            f"Bug: .jar files appeared in git delta: {jar_files}. "
            "Binary files must be excluded from committed_files."
        )
        assert class_files == [], (
            f"Bug: .class files appeared in git delta: {class_files}. "
            "Compiled class files must be excluded from committed_files."
        )

    def test_git_delta_excludes_all_production_binary_extensions(
        self, tmp_path: Path
    ) -> None:
        """All binary extensions from the production bug report must be excluded."""
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = get_current_commit(tmp_path)

        binary_files = {
            f"binary/file.{ext}": make_binary_content()
            for ext in PRODUCTION_BINARY_EXTENSIONS
        }
        commit_files(tmp_path, binary_files, "add all production binary files")
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        all_delta_files = delta.added + delta.modified + delta.deleted

        unexpected_binaries = [
            f
            for f in all_delta_files
            if Path(f).suffix.lstrip(".") in PRODUCTION_BINARY_EXTENSIONS
        ]
        assert unexpected_binaries == [], (
            f"Bug: Binary files appeared in git delta: {unexpected_binaries}. "
            "All production binary extensions must be excluded."
        )

    def test_git_delta_mixed_production_scenario(self, tmp_path: Path) -> None:
        """Replicate production scenario: 5 source files + 20 binary junk files.

        After Bug 1 fix: only the 5 source files should appear in committed_files.
        Currently (Bug 1 active): 0 source files appear (all rejected by dot mismatch).
        """
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = get_current_commit(tmp_path)

        source_files = {
            "code/src/ServiceA.java": make_source_content("java"),
            "code/src/ServiceB.java": make_source_content("java"),
            "code/src/ModelC.kt": make_source_content("kt"),
            "code/src/ControllerD.kt": make_source_content("kt"),
            "code/src/UtilsE.java": make_source_content("java"),
        }

        binary_files = {
            "3dparty/metro/webservices-rt.jar": make_binary_content(),
            "3dparty/zelix/ZKM.jar": make_binary_content(),
            "3dparty/zelix/ZKM.class": make_binary_content(),
            "clientside/app.exe": make_binary_content(),
            "clientside/lib.dll": make_binary_content(),
            "clientside/client.zip": make_binary_content(),
            "clientside/dictionary.dic": make_binary_content(),
            "buildtools/exe4j/tool.exe": make_binary_content(),
            "buildtools/exe4j/icon.png": make_binary_content(),
            "buildtools/exe4j/lib.dylib": make_binary_content(),
            "MediaResources/design.psd": make_binary_content(),
            "MediaResources/artwork.xcf": make_binary_content(),
            "MediaResources/logo.blend": make_binary_content(),
            "MediaResources/fonts/custom.ttf": make_binary_content(),
            "Docker/etcd-backup.gz": make_binary_content(),
            "native/libfoo.so": make_binary_content(),
            "native/libbar.dylib": make_binary_content(),
            "media/logo.tif": make_binary_content(),
            "media/splash.gif": make_binary_content(),
            "data/thumbs.db": make_binary_content(),
        }

        commit_files(
            tmp_path,
            {**source_files, **binary_files},
            "production-like commit: 5 source + 20 binary",
        )
        new_commit = get_current_commit(tmp_path)

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        all_delta_files = set(delta.added + delta.modified)

        # No binary files should be in delta
        unexpected = [
            f
            for f in all_delta_files
            if Path(f).suffix.lstrip(".") in PRODUCTION_BINARY_EXTENSIONS
        ]
        assert (
            unexpected == []
        ), f"Binary files leaked into committed_files: {unexpected}"

        # All 5 source files MUST be in delta (FAILS currently due to Bug 1)
        found_source = [
            f for f in all_delta_files if Path(f).suffix.lstrip(".") in ["java", "kt"]
        ]
        assert len(found_source) == 5, (
            f"Bug 1: Expected 5 source files in git delta, got {len(found_source)}: "
            f"{found_source}. "
            f"Full delta: {sorted(all_delta_files)}. "
            "Root cause: _should_index_file dot mismatch causes ALL source files "
            "to be rejected from the git delta."
        )
