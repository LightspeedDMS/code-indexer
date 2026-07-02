"""
Group 1: Prove _should_index_file() dot mismatch bug (smart_indexer.py:206).

Bug:
    path.suffix.lower() returns ".java" (WITH dot)
    config.file_extensions stores "java"  (WITHOUT dot)
    -> ".java" not in ["java", ...] -> always returns False
    -> ALL source files rejected from TRACK 1 (git delta path)

These tests FAIL against current production code to prove the bug exists.

Run:
    PYTHONPATH=src pytest tests/unit/services/test_should_index_file_dot_bug.py \
        -v --tb=short
"""

from pathlib import Path

import pytest

from .incremental_filter_helpers import (
    PRODUCTION_BINARY_EXTENSIONS,
    VALID_SOURCE_EXTENSIONS,
    build_smart_indexer,
)


class TestShouldIndexFileDotBug:
    """Prove _should_index_file() rejects ALL files due to dot mismatch.

    path.suffix.lower() returns ".java" (WITH dot).
    config.file_extensions stores "java"   (WITHOUT dot).
    So ".java" not in ["java", ...] -> always returns False.
    """

    def _make_indexer(self, tmp_path: Path):
        metadata = tmp_path / "metadata.json"
        metadata.write_text("{}")
        return build_smart_indexer(tmp_path, metadata)

    def test_should_index_java_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return True for a .java file path.

        FAILS currently: ".java" not in ["java", ...] -> returns False.
        """
        indexer = self._make_indexer(tmp_path)
        assert "java" in indexer.config.file_extensions, (
            "Config must contain 'java' (no dot) in file_extensions"
        )
        result = indexer._should_index_file("src/Main.java")
        assert result is True, (
            "Bug 1: _should_index_file returned False for .java file — "
            "dot mismatch: path.suffix.lower() returns '.java' but "
            "config.file_extensions contains 'java' (no dot)"
        )

    def test_should_index_kotlin_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return True for a .kt file path.

        FAILS currently due to Bug 1 dot mismatch.
        """
        indexer = self._make_indexer(tmp_path)
        assert "kt" in indexer.config.file_extensions
        result = indexer._should_index_file("src/Utils.kt")
        assert result is True, (
            "Bug 1: _should_index_file returned False for .kt file — dot mismatch"
        )

    def test_should_index_python_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return True for a .py file path.

        FAILS currently due to Bug 1 dot mismatch.
        """
        indexer = self._make_indexer(tmp_path)
        assert "py" in indexer.config.file_extensions
        result = indexer._should_index_file("src/main.py")
        assert result is True, (
            "Bug 1: _should_index_file returned False for .py file — dot mismatch"
        )

    def test_should_index_typescript_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return True for a .ts file path.

        FAILS currently due to Bug 1 dot mismatch.
        """
        indexer = self._make_indexer(tmp_path)
        assert "ts" in indexer.config.file_extensions
        result = indexer._should_index_file("src/App.ts")
        assert result is True, (
            "Bug 1: _should_index_file returned False for .ts file — dot mismatch"
        )

    def test_should_reject_jar_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return False for a .jar file (not in extensions)."""
        indexer = self._make_indexer(tmp_path)
        assert "jar" not in indexer.config.file_extensions
        result = indexer._should_index_file("lib/library.jar")
        assert result is False, ".jar files must always be rejected"

    def test_should_reject_exe_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return False for a .exe file."""
        indexer = self._make_indexer(tmp_path)
        assert "exe" not in indexer.config.file_extensions
        result = indexer._should_index_file("tools/setup.exe")
        assert result is False, ".exe files must always be rejected"

    def test_should_reject_psd_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return False for a .psd file."""
        indexer = self._make_indexer(tmp_path)
        assert "psd" not in indexer.config.file_extensions
        result = indexer._should_index_file("design/artwork.psd")
        assert result is False, ".psd files must always be rejected"

    def test_should_reject_dll_file(self, tmp_path: Path) -> None:
        """_should_index_file MUST return False for a .dll file."""
        indexer = self._make_indexer(tmp_path)
        assert "dll" not in indexer.config.file_extensions
        result = indexer._should_index_file("native/lib.dll")
        assert result is False, ".dll files must always be rejected"

    def test_dot_mismatch_is_the_root_cause(self, tmp_path: Path) -> None:
        """Demonstrate that the bug is specifically the dot: suffix includes it, extensions don't."""
        indexer = self._make_indexer(tmp_path)

        java_path = Path("src/Main.java")
        suffix_with_dot = java_path.suffix.lower()  # ".java"
        extension_no_dot = java_path.suffix.lstrip(".").lower()  # "java"

        # The bad comparison currently happening in production — passes as expected:
        assert suffix_with_dot not in indexer.config.file_extensions, (
            f"Confirming the bug: '{suffix_with_dot}' is NOT in file_extensions "
            f"(which stores '{extension_no_dot}')"
        )

        # The correct check — this also passes (proving the fix):
        assert extension_no_dot in indexer.config.file_extensions, (
            f"The CORRECT check: '{extension_no_dot}' IS in file_extensions — "
            "but _should_index_file uses suffix (WITH dot), not lstrip('.')"
        )

    @pytest.mark.parametrize("ext", VALID_SOURCE_EXTENSIONS)
    def test_all_source_extensions_accepted(self, ext: str, tmp_path: Path) -> None:
        """All standard source extensions must be accepted by _should_index_file.

        FAILS for every extension in VALID_SOURCE_EXTENSIONS due to dot mismatch.
        """
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file(f"src/file.{ext}")
        assert result is True, (
            f"Bug 1: _should_index_file rejected '.{ext}' — "
            "dot mismatch prevents ALL source files from being indexed"
        )

    @pytest.mark.parametrize("ext", PRODUCTION_BINARY_EXTENSIONS)
    def test_all_binary_extensions_rejected(self, ext: str, tmp_path: Path) -> None:
        """All production binary extensions must be rejected by _should_index_file."""
        indexer = self._make_indexer(tmp_path)
        result = indexer._should_index_file(f"lib/file.{ext}")
        assert result is False, (
            f"_should_index_file should reject '.{ext}' (not a source extension)"
        )
