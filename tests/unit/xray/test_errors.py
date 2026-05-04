"""Tests for xray/errors.py: XRayExtrasNotInstalled error class."""

import pytest


class TestXRayExtrasNotInstalled:
    """Tests for XRayExtrasNotInstalled exception."""

    def test_is_import_error(self) -> None:
        """XRayExtrasNotInstalled must be a subclass of ImportError."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        assert issubclass(XRayExtrasNotInstalled, ImportError)

    def test_can_be_raised_and_caught_as_import_error(self) -> None:
        """XRayExtrasNotInstalled can be caught as ImportError."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        with pytest.raises(ImportError):
            raise XRayExtrasNotInstalled("tree_sitter")

    def test_message_contains_package_name(self) -> None:
        """The error message includes the missing package name."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        exc = XRayExtrasNotInstalled("tree_sitter_languages")
        assert "tree_sitter_languages" in str(exc)

    def test_default_message_mentions_install_hint(self) -> None:
        """The error message provides a pip install hint."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        exc = XRayExtrasNotInstalled("tree_sitter")
        msg = str(exc)
        assert "pip install" in msg or "install" in msg.lower()

    def test_package_name_attribute(self) -> None:
        """XRayExtrasNotInstalled exposes the package name as an attribute."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        exc = XRayExtrasNotInstalled("tree_sitter")
        assert exc.package == "tree_sitter"

    def test_different_package_names(self) -> None:
        """Works for any package name, not just tree_sitter."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        exc = XRayExtrasNotInstalled("some_other_package")
        assert exc.package == "some_other_package"
        assert "some_other_package" in str(exc)
