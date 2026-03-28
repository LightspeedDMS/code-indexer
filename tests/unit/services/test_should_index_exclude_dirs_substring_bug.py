"""TDD tests for exclude_dirs substring matching bug in SmartIndexer._should_index_file.

Bug: _should_index_file() uses naive `if exclude_dir in path_str` which matches
substrings anywhere in the path, not just directory boundaries. This causes
legitimate source files to be silently excluded from git delta indexing.

Default exclude_dirs: node_modules, venv, __pycache__, .git, dist, build,
target, .idea, .vscode, .gradle, bin, obj, coverage, .next, .nuxt, dist-*,
.code-indexer

Dangerous substring matches: "build", "dist", "target", "bin", "obj", "coverage"
"""

from pathlib import Path

import pytest

from .incremental_filter_helpers import build_smart_indexer


class TestExcludeDirsSubstringBug:
    """Prove _should_index_file falsely excludes files via substring match."""

    def _make_indexer(self, tmp_path: Path):
        metadata = tmp_path / ".code-indexer" / "metadata.json"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text("{}")
        return build_smart_indexer(tmp_path, metadata)

    # -----------------------------------------------------------------
    # "build" substring false positives
    # -----------------------------------------------------------------

    def test_builder_package_not_excluded_by_build(self, tmp_path: Path) -> None:
        """src/builder/App.java must NOT be excluded by 'build' in exclude_dirs."""
        indexer = self._make_indexer(tmp_path)
        assert "build" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/builder/App.java")
        assert result is True, (
            "Substring bug: 'build' in 'src/builder/App.java' is True, "
            "but builder/ is NOT the build/ directory"
        )

    def test_rebuild_module_not_excluded_by_build(self, tmp_path: Path) -> None:
        """src/rebuild/manager.kt must NOT be excluded by 'build'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/rebuild/manager.kt")
        assert result is True, (
            "Substring bug: 'build' in 'src/rebuild/manager.kt' is True"
        )

    def test_build_config_file_not_excluded(self, tmp_path: Path) -> None:
        """src/BuildConfig.java must NOT be excluded by 'build'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/BuildConfig.java")
        assert result is True, (
            "Substring bug: 'build' matches filename BuildConfig.java"
        )

    # -----------------------------------------------------------------
    # "dist" substring false positives
    # -----------------------------------------------------------------

    def test_distributed_package_not_excluded_by_dist(self, tmp_path: Path) -> None:
        """src/distributed/ClusterManager.java must NOT be excluded by 'dist'."""
        indexer = self._make_indexer(tmp_path)
        assert "dist" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/distributed/ClusterManager.java")
        assert result is True, (
            "Substring bug: 'dist' in 'src/distributed/ClusterManager.java' is True"
        )

    def test_distribution_module_not_excluded_by_dist(self, tmp_path: Path) -> None:
        """code/distribution/PackageService.kt must NOT be excluded by 'dist'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("code/distribution/PackageService.kt")
        assert result is True, (
            "Substring bug: 'dist' in 'code/distribution/PackageService.kt' is True"
        )

    def test_redis_dist_lock_file_not_excluded(self, tmp_path: Path) -> None:
        """src/redis_dist_lock.py must NOT be excluded by 'dist'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/redis_dist_lock.py")
        assert result is True, (
            "Substring bug: 'dist' in 'src/redis_dist_lock.py' is True"
        )

    # -----------------------------------------------------------------
    # "target" substring false positives
    # -----------------------------------------------------------------

    def test_target_in_filename_not_excluded(self, tmp_path: Path) -> None:
        """src/deploy/target_host.py must NOT be excluded by 'target'."""
        indexer = self._make_indexer(tmp_path)
        assert "target" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/deploy/target_host.py")
        assert result is True, (
            "Substring bug: 'target' in 'src/deploy/target_host.py' is True"
        )

    def test_retarget_module_not_excluded(self, tmp_path: Path) -> None:
        """src/retarget/linker.rs must NOT be excluded by 'target'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/retarget/linker.rs")
        assert result is True, (
            "Substring bug: 'target' in 'src/retarget/linker.rs' is True"
        )

    # -----------------------------------------------------------------
    # "bin" substring false positives
    # -----------------------------------------------------------------

    def test_cabin_package_not_excluded_by_bin(self, tmp_path: Path) -> None:
        """src/cabin/utils.py must NOT be excluded by 'bin'."""
        indexer = self._make_indexer(tmp_path)
        assert "bin" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/cabin/utils.py")
        assert result is True, "Substring bug: 'bin' in 'src/cabin/utils.py' is True"

    def test_binary_parser_module_not_excluded_by_bin(self, tmp_path: Path) -> None:
        """src/binary_parser/reader.java must NOT be excluded by 'bin'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/binary_parser/reader.java")
        assert result is True, (
            "Substring bug: 'bin' in 'src/binary_parser/reader.java' is True"
        )

    def test_bindings_module_not_excluded_by_bin(self, tmp_path: Path) -> None:
        """src/bindings/ffi.rs must NOT be excluded by 'bin'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/bindings/ffi.rs")
        assert result is True, "Substring bug: 'bin' in 'src/bindings/ffi.rs' is True"

    # -----------------------------------------------------------------
    # "obj" substring false positives
    # -----------------------------------------------------------------

    def test_object_mapper_not_excluded_by_obj(self, tmp_path: Path) -> None:
        """src/object_mapper/serializer.java must NOT be excluded by 'obj'."""
        indexer = self._make_indexer(tmp_path)
        assert "obj" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/object_mapper/serializer.java")
        assert result is True, (
            "Substring bug: 'obj' in 'src/object_mapper/serializer.java' is True"
        )

    def test_objective_module_not_excluded_by_obj(self, tmp_path: Path) -> None:
        """src/objective/goal.kt must NOT be excluded by 'obj'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/objective/goal.kt")
        assert result is True, "Substring bug: 'obj' in 'src/objective/goal.kt' is True"

    # -----------------------------------------------------------------
    # "coverage" substring false positives
    # -----------------------------------------------------------------

    def test_coverage_report_generator_not_excluded(self, tmp_path: Path) -> None:
        """src/coverage_report/generator.py must NOT be excluded by 'coverage'."""
        indexer = self._make_indexer(tmp_path)
        assert "coverage" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/coverage_report/generator.py")
        assert result is True, (
            "Substring bug: 'coverage' in 'src/coverage_report/generator.py' is True"
        )

    # -----------------------------------------------------------------
    # ".git" substring false positives
    # -----------------------------------------------------------------

    def test_github_module_not_excluded_by_dotgit(self, tmp_path: Path) -> None:
        """src/github/client.py must NOT be excluded by '.git'."""
        indexer = self._make_indexer(tmp_path)
        assert ".git" in indexer.config.exclude_dirs
        result = indexer._should_index_file("src/github/client.py")
        assert result is True, (
            "Substring bug: '.git' in 'src/github/client.py' is True — "
            "github/ is NOT .git/"
        )

    def test_gitignore_parser_not_excluded_by_dotgit(self, tmp_path: Path) -> None:
        """src/gitignore_parser.py must NOT be excluded by '.git'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("src/gitignore_parser.py")
        assert result is True, (
            "Substring bug: '.git' in 'src/gitignore_parser.py' is True"
        )

    # -----------------------------------------------------------------
    # "venv" substring false positives
    # -----------------------------------------------------------------

    def test_event_module_not_excluded_by_venv(self, tmp_path: Path) -> None:
        """src/event/handler.py must NOT be excluded by 'venv'.

        Note: 'venv' in 'event' is True because 'ven' + 'v' overlaps... actually
        'venv' is NOT a substring of 'event'. But let's verify with a closer match.
        """
        indexer = self._make_indexer(tmp_path)
        # 'venv' not in 'event' - this is actually fine, but test to be sure
        result = indexer._should_index_file("src/event/handler.py")
        assert result is True

    def test_venv_config_file_not_excluded(self, tmp_path: Path) -> None:
        """tools/venv_setup.py must NOT be excluded by 'venv'."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("tools/venv_setup.py")
        assert result is True, "Substring bug: 'venv' in 'tools/venv_setup.py' is True"

    # -----------------------------------------------------------------
    # Actual exclude dirs MUST still be excluded
    # -----------------------------------------------------------------

    def test_actual_build_dir_is_excluded(self, tmp_path: Path) -> None:
        """build/output/app.java MUST be excluded (it IS in the build/ dir)."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("build/output/app.java")
        assert result is False, "Files in actual build/ directory must be excluded"

    def test_actual_dist_dir_is_excluded(self, tmp_path: Path) -> None:
        """dist/bundle.js MUST be excluded (it IS in the dist/ dir)."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("dist/bundle.js")
        assert result is False, "Files in actual dist/ directory must be excluded"

    def test_actual_bin_dir_is_excluded(self, tmp_path: Path) -> None:
        """bin/run.sh MUST be excluded (it IS in the bin/ dir)."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("bin/run.sh")
        assert result is False, "Files in actual bin/ directory must be excluded"

    def test_actual_target_dir_is_excluded(self, tmp_path: Path) -> None:
        """target/classes/Main.class - not a source extension, excluded regardless."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("target/classes/App.java")
        assert result is False, "Files in actual target/ directory must be excluded"

    def test_actual_node_modules_excluded(self, tmp_path: Path) -> None:
        """node_modules/lodash/index.js MUST be excluded."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("node_modules/lodash/index.js")
        assert result is False, "Files in node_modules/ must be excluded"

    def test_actual_git_dir_excluded(self, tmp_path: Path) -> None:
        """.git/config must be excluded."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file(".git/config")
        assert result is False, "Files in .git/ must be excluded"

    def test_nested_build_dir_is_excluded(self, tmp_path: Path) -> None:
        """project/build/generated/App.java MUST be excluded."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file("project/build/generated/App.java")
        assert result is False, "Files in nested build/ directory must be excluded"

    # -----------------------------------------------------------------
    # Parametrized: many realistic false-positive scenarios
    # -----------------------------------------------------------------

    @pytest.mark.parametrize(
        "path,description",
        [
            ("src/builder/TaskBuilder.java", "builder/ vs build"),
            ("src/rebuilder/core.kt", "rebuilder/ vs build"),
            ("src/distributed/consensus.java", "distributed/ vs dist"),
            ("src/redistribution/license.py", "redistribution/ vs dist"),
            ("src/targeting/rules.ts", "targeting/ vs target"),
            ("src/binary/decoder.go", "binary/ vs bin"),
            ("src/combine/merge.rs", "combine/ vs bin"),
            ("src/objection/handler.cs", "objection/ vs obj"),
            ("src/objects/factory.java", "objects/ vs obj"),
            ("src/discover/scanner.py", "discover/ vs dist (overlap)"),
        ],
    )
    def test_parametrized_false_positives(
        self, tmp_path: Path, path: str, description: str
    ) -> None:
        """Parametrized: legitimate source files must not be excluded by substring match."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file(path)
        assert result is True, (
            f"Substring bug ({description}): '{path}' wrongly excluded"
        )
