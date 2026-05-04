"""Error classes for the X-Ray AST search engine."""


class XRayExtrasNotInstalled(ImportError):
    """Raised when a required tree-sitter package is not installed.

    Provides a helpful pip install hint so users know exactly what to install.
    """

    def __init__(self, package: str) -> None:
        self.package = package
        super().__init__(
            f"X-Ray requires '{package}' which is not installed. "
            f"Install it with: pip install {package}"
        )
