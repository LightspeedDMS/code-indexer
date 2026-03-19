"""
Group 3: End-to-end tests with a real git repo containing binary junk.

Creates a temp git repo mirroring the production Java/Kotlin structure,
does a simulated 'git pull' (commit new files), then verifies incremental
indexing only processes source files.

Also validates that FileFinder and SmartIndexer._should_index_file agree.

These tests FAIL against current code (Bug 1: dot mismatch).

Run:
    PYTHONPATH=src pytest tests/unit/services/test_incremental_indexing_binary_repo.py \
        -v --tb=short
"""

from pathlib import Path

from .incremental_filter_helpers import (
    PRODUCTION_BINARY_EXTENSIONS,
    build_smart_indexer,
    commit_files,
    create_git_repo,
    init_repo_with_indexer,
    make_binary_content,
    make_source_content,
)


def _commit_production_structure(repo_path: Path) -> str:
    """Commit a full production-like directory structure and return the commit hash."""
    files = {
        # Source files (SHOULD index)
        "code/src/Main.java": make_source_content("java"),
        "code/src/Utils.kt": make_source_content("kt"),
        "code/src/Config.java": make_source_content("java"),
        "code/test/MainTest.java": make_source_content("java"),
        "code/test/UtilsTest.kt": make_source_content("kt"),
        # 3rd party binaries
        "code/3dparty/metro/webservices-rt.jar": make_binary_content(),
        "code/3dparty/zelix/ZKM.jar": make_binary_content(),
        "code/3dparty/zelix/ZKM.class": make_binary_content(),
        # Client-side binaries
        "code/clientside/app.exe": make_binary_content(),
        "code/clientside/lib.dll": make_binary_content(),
        "code/clientside/client.zip": make_binary_content(),
        "code/clientside/dictionary.dic": make_binary_content(),
        # Build tools
        "buildtools/exe4j/tool.exe": make_binary_content(),
        "buildtools/exe4j/icon.png": make_binary_content(),
        "buildtools/exe4j/lib.dylib": make_binary_content(),
        # Media / design resources
        "MediaResources/photoshop.old/design.psd": make_binary_content(),
        "MediaResources/photoshop.old/artwork.xcf": make_binary_content(),
        "MediaResources/photoshop.old/logo.blend": make_binary_content(),
        "MediaResources/photoshop.old/logo.blend1": make_binary_content(),
        "MediaResources/photoshop.old/Logos/logo.tif": make_binary_content(),
        "MediaResources/fonts/custom.ttf": make_binary_content(),
        # Archive
        "Docker/etcdserver/etcd-backup.gz": make_binary_content(),
        # OS junk
        "Thumbs.db": make_binary_content(),
    }
    return commit_files(repo_path, files, "full production-like repo structure")


class TestIncrementalIndexingBinaryRepo:
    """End-to-end tests with a real git repo containing binary junk."""

    def test_incremental_only_indexes_changed_source_files(
        self, tmp_path: Path
    ) -> None:
        """After simulated git pull changing 3 source files, only those 3 appear in delta.

        FAILS currently because Bug 1 (dot mismatch) causes _should_index_file
        to return False for .java files, so committed_files is always empty.
        """
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = _commit_production_structure(tmp_path)

        new_commit = commit_files(
            tmp_path,
            {
                "code/src/Main.java": "// updated\n" + make_source_content("java"),
                "code/src/Utils.kt": "// updated\n" + make_source_content("kt"),
                "code/src/Config.java": "// updated\n" + make_source_content("java"),
            },
            "simulate git pull: 3 source files changed",
        )

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        committed = delta.added + delta.modified

        assert len(committed) == 3, (
            f"Bug 1: Expected 3 source files in committed_files, got {len(committed)}: "
            f"{committed}. "
            "Root cause: _should_index_file dot mismatch rejects all source files."
        )

        source_exts = {Path(f).suffix.lstrip(".") for f in committed}
        assert source_exts <= {
            "java",
            "kt",
        }, f"Only source extensions should be present, got: {source_exts}"

    def test_incremental_does_not_include_binary_files_after_pull(
        self, tmp_path: Path
    ) -> None:
        """Simulate git pull where binary files also changed — they must NOT be indexed.

        Source files assertion (2) FAILS currently due to Bug 1.
        Binary exclusion assertion passes (binaries not in extensions).
        """
        indexer, _ = init_repo_with_indexer(tmp_path)
        old_commit = _commit_production_structure(tmp_path)

        new_commit = commit_files(
            tmp_path,
            {
                # Source (should index)
                "code/src/Main.java": "// updated\n" + make_source_content("java"),
                "code/src/Utils.kt": "// updated\n" + make_source_content("kt"),
                # Binary (must NOT index)
                "code/3dparty/metro/webservices-rt.jar": make_binary_content()
                + b"\x99",
                "code/3dparty/zelix/ZKM.jar": make_binary_content() + b"\x88",
                "buildtools/exe4j/tool.exe": make_binary_content() + b"\x77",
                "MediaResources/photoshop.old/design.psd": make_binary_content()
                + b"\x66",
                "Docker/etcdserver/etcd-backup.gz": make_binary_content() + b"\x55",
            },
            "simulate git pull: 2 source + 5 binary changed",
        )

        delta = indexer._get_git_deltas_since_commit(old_commit, new_commit)
        committed = delta.added + delta.modified

        binary_in_committed = [
            f
            for f in committed
            if Path(f).suffix.lstrip(".") in PRODUCTION_BINARY_EXTENSIONS
        ]
        assert binary_in_committed == [], (
            f"Binary files in committed_files: {binary_in_committed}. "
            "Binary files must NEVER be passed to the indexing processor."
        )

        source_in_committed = [
            f for f in committed if Path(f).suffix.lstrip(".") in ["java", "kt"]
        ]
        assert len(source_in_committed) == 2, (
            f"Bug 1: Expected 2 source files in committed_files, "
            f"got {len(source_in_committed)}: {source_in_committed}. "
            f"Full committed: {committed}."
        )

    def test_full_index_excludes_binary_files(self, tmp_path: Path) -> None:
        """Full reindex via FileFinder must only find files with source extensions.

        FileFinder._get_base_filtering_result uses lstrip('.') correctly,
        so this should PASS even with Bug 1 present. Validates the full-index path.
        """
        create_git_repo(tmp_path)
        _commit_production_structure(tmp_path)

        metadata = tmp_path / ".code-indexer" / "metadata.json"
        metadata.parent.mkdir(exist_ok=True)
        metadata.write_text("{}")
        indexer = build_smart_indexer(tmp_path, metadata)

        found_files = list(indexer.file_finder.find_files())
        found_exts = {f.suffix.lstrip(".") for f in found_files}

        leaked_binary = found_exts & set(PRODUCTION_BINARY_EXTENSIONS)
        assert leaked_binary == set(), (
            f"Binary extensions appeared in full index: {leaked_binary}. "
            "FileFinder must exclude all binary file types."
        )

        source_exts_found = found_exts & {"java", "kt"}
        assert len(source_exts_found) > 0, (
            "FileFinder must find at least some source files. "
            f"Found extensions: {found_exts}"
        )

    def test_smart_indexer_should_index_file_matches_file_finder(
        self, tmp_path: Path
    ) -> None:
        """_should_index_file and FileFinder._should_include_file must agree.

        Currently they DISAGREE because:
        - FileFinder uses: file_path.suffix.lstrip('.')  (correct, no dot)
        - SmartIndexer uses: path.suffix.lower()          (BUG: has dot)

        FAILS currently: file_finder returns True, _should_index_file returns False.
        """
        create_git_repo(tmp_path)
        metadata = tmp_path / ".code-indexer" / "metadata.json"
        metadata.parent.mkdir(exist_ok=True)
        metadata.write_text("{}")
        indexer = build_smart_indexer(tmp_path, metadata)

        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir(parents=True, exist_ok=True)
        java_file.write_text(make_source_content("java"))

        file_finder_result = indexer.file_finder._should_include_file(java_file)
        smart_indexer_result = indexer._should_index_file("src/Main.java")

        assert file_finder_result is True, (
            "FileFinder._should_include_file must accept .java files "
            "(it uses lstrip('.') which is correct)"
        )
        assert smart_indexer_result is True, (
            f"Bug 1: SmartIndexer._should_index_file returned {smart_indexer_result} "
            f"for .java while FileFinder returned {file_finder_result}. "
            "These two methods must agree. The dot mismatch causes divergence: "
            "git delta is filtered differently from filesystem scan."
        )
