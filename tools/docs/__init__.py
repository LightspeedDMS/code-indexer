"""Reference-documentation generators (Story #2082).

Each module renders one file under ``docs/reference/`` from code, so the
reference cannot drift from the implementation. Run from the repository root
with this tree's sources on the path::

    PYTHONPATH=src python3 -m tools.docs.cli_reference [--check]
    PYTHONPATH=src python3 -m tools.docs.mcp_tools [--check]
    PYTHONPATH=src python3 -m tools.docs.error_codes [--check]

Without ``--check`` the file is (re)written; with it the command exits 1 when
the committed file is stale. ``lint.sh`` runs all three checks.
"""
