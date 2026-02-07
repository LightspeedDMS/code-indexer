"""
hnswlib verification utility.

Validates that the installed hnswlib package has the custom check_integrity()
method available, which is only present in the code-indexer build from the
third_party/hnswlib submodule.

This verification ensures users are running the correct hnswlib version and
provides helpful error messages if they accidentally install the PyPI version.
"""

import logging
from typing import NoReturn

# Import hnswlib at module level for better testability
try:
    import hnswlib as _hnswlib
except ImportError:
    _hnswlib = None  # type: ignore

logger = logging.getLogger(__name__)


def verify_custom_hnswlib() -> bool:
    """
    Verify that hnswlib has the custom check_integrity() method.

    Returns:
        bool: True if verification succeeds

    Raises:
        ImportError: If hnswlib is not installed
        AttributeError: If hnswlib is missing check_integrity() method
    """
    if _hnswlib is None:
        _raise_hnswlib_not_installed(ImportError("No module named 'hnswlib'"))

    # Create a minimal test index to check for check_integrity method
    try:
        test_index = _hnswlib.Index(space="l2", dim=128)
    except Exception as e:
        logger.warning(f"Failed to create test hnswlib Index: {e}")
        # If we can't create an index, hnswlib is installed but broken
        raise ImportError(
            "hnswlib is installed but failed to initialize. "
            "Try reinstalling: pip uninstall hnswlib && pip install -e ."
        ) from e

    # Check for check_integrity method
    if not hasattr(test_index, "check_integrity"):
        _raise_missing_check_integrity()

    logger.debug("hnswlib verification passed: check_integrity() method available")
    return True


def _raise_hnswlib_not_installed(original_error: ImportError) -> NoReturn:
    """Raise helpful error when hnswlib is not installed."""
    error_msg = (
        "hnswlib is not installed. Code-indexer requires building hnswlib from "
        "source using the third_party/hnswlib submodule.\n\n"
        "Installation steps:\n"
        "1. Initialize submodule: git submodule update --init\n"
        "2. Install in development mode: pip install -e .\n\n"
        "The submodule contains a custom hnswlib build with check_integrity() method.\n"
        f"Original error: {original_error}"
    )
    raise ImportError(error_msg) from original_error


def _raise_missing_check_integrity() -> NoReturn:
    """Raise helpful error when check_integrity method is missing."""
    error_msg = (
        "hnswlib.Index does not have check_integrity() method.\n\n"
        "This means you're using the PyPI version of hnswlib instead of the "
        "custom code-indexer build from the submodule.\n\n"
        "To fix this:\n"
        "1. Uninstall PyPI version: pip uninstall hnswlib\n"
        "2. Initialize submodule: git submodule update --init\n"
        "3. Reinstall code-indexer: pip install -e .\n\n"
        "The third_party/hnswlib submodule contains a custom build with "
        "check_integrity() method required for index integrity validation."
    )
    raise AttributeError(error_msg)
