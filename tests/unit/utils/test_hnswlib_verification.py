"""
Unit tests for hnswlib verification utility.

Tests the verify_custom_hnswlib() function that validates the custom hnswlib
build has the check_integrity() method available.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestHnswlibVerification:
    """Test suite for hnswlib verification utility."""

    def test_verify_custom_hnswlib_succeeds_when_check_integrity_exists(self):
        """
        GIVEN hnswlib is installed with check_integrity() method
        WHEN verify_custom_hnswlib() is called
        THEN it returns True without raising an exception
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Mock hnswlib.Index to have check_integrity method
        mock_index = MagicMock()
        mock_index.check_integrity = MagicMock(return_value=True)

        mock_hnswlib = MagicMock()
        mock_hnswlib.Index.return_value = mock_index

        with patch("code_indexer.utils.hnswlib_verification._hnswlib", mock_hnswlib):
            result = verify_custom_hnswlib()

        assert result is True

    def test_verify_custom_hnswlib_raises_when_check_integrity_missing(self):
        """
        GIVEN hnswlib is installed WITHOUT check_integrity() method
        WHEN verify_custom_hnswlib() is called
        THEN it raises AttributeError with descriptive message
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Mock hnswlib module and Index without check_integrity method
        mock_index = MagicMock(spec=[])  # Empty spec = no methods

        mock_hnswlib = MagicMock()
        mock_hnswlib.Index.return_value = mock_index

        with patch("code_indexer.utils.hnswlib_verification._hnswlib", mock_hnswlib):
            with pytest.raises(
                AttributeError,
                match=r"hnswlib\.Index does not have check_integrity\(\) method",
            ):
                verify_custom_hnswlib()

    def test_verify_custom_hnswlib_raises_when_hnswlib_not_installed(self):
        """
        GIVEN hnswlib is not installed (_hnswlib is None)
        WHEN verify_custom_hnswlib() is called
        THEN it raises ImportError with descriptive message including submodule instructions
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Simulate module not installed
        with patch("code_indexer.utils.hnswlib_verification._hnswlib", None):
            with pytest.raises(ImportError) as exc_info:
                verify_custom_hnswlib()

        # Verify error message contains key instructions
        error_msg = str(exc_info.value)
        assert "hnswlib is not installed" in error_msg
        assert "submodule" in error_msg
        assert "pip install" in error_msg

    def test_verify_custom_hnswlib_can_be_called_at_startup(self):
        """
        GIVEN the application is starting
        WHEN verify_custom_hnswlib() is called early in startup
        THEN it completes quickly (no slow imports or I/O)
        """
        import time
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Mock successful verification
        mock_index = MagicMock()
        mock_index.check_integrity = MagicMock(return_value=True)

        mock_hnswlib = MagicMock()
        mock_hnswlib.Index.return_value = mock_index

        with patch("code_indexer.utils.hnswlib_verification._hnswlib", mock_hnswlib):
            start = time.time()
            verify_custom_hnswlib()
            elapsed = time.time() - start

        # Should complete in under 100ms
        assert elapsed < 0.1

    def test_verify_custom_hnswlib_provides_helpful_error_message(self):
        """
        GIVEN hnswlib from PyPI is installed (missing check_integrity)
        WHEN verify_custom_hnswlib() is called
        THEN error message includes submodule initialization instructions
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        mock_index = MagicMock(spec=["init_index", "add_items"])  # PyPI methods only

        mock_hnswlib = MagicMock()
        mock_hnswlib.Index.return_value = mock_index

        with patch("code_indexer.utils.hnswlib_verification._hnswlib", mock_hnswlib):
            with pytest.raises(AttributeError) as exc_info:
                verify_custom_hnswlib()

        error_msg = str(exc_info.value)
        assert "git submodule update --init" in error_msg
        assert "pip install -e ." in error_msg

    def test_verify_custom_hnswlib_handles_index_creation_failure(self):
        """
        GIVEN hnswlib is installed but Index creation fails
        WHEN verify_custom_hnswlib() is called
        THEN it raises ImportError suggesting reinstallation
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Mock hnswlib.Index to raise exception on creation
        mock_hnswlib = MagicMock()
        mock_hnswlib.Index.side_effect = Exception("Index creation failed")

        with patch("code_indexer.utils.hnswlib_verification._hnswlib", mock_hnswlib):
            with pytest.raises(ImportError) as exc_info:
                verify_custom_hnswlib()

        error_msg = str(exc_info.value)
        assert "hnswlib is installed but failed to initialize" in error_msg
        assert "Try reinstalling" in error_msg

    def test_verify_custom_hnswlib_handles_import_error_from_module_level(self):
        """
        GIVEN hnswlib failed to import at module level (lines 18-19)
        WHEN verify_custom_hnswlib() is called
        THEN it raises ImportError with submodule instructions
        """
        from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

        # Simulate _hnswlib being None due to import failure
        with patch("code_indexer.utils.hnswlib_verification._hnswlib", None):
            with pytest.raises(ImportError) as exc_info:
                verify_custom_hnswlib()

        error_msg = str(exc_info.value)
        assert "hnswlib is not installed" in error_msg
        assert "git submodule update --init" in error_msg
        # Verify original error is mentioned
        assert "Original error:" in error_msg
