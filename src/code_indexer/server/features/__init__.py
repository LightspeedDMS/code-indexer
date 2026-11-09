"""Feature add-on loading (modular plan phase 3).

``loader`` discovers ``code_indexer.features`` entry points and validates them
against the SDK contract; ``registry`` holds what was loaded, runs the
startup/shutdown hooks and reports health. See
``docs/architecture/feature-sdk.md``.
"""
