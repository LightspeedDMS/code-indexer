"""Pluggable TemporalEmbedder adapters (Story #1290 / Epic #1289).

Importing this package self-registers all first-class adapters into the
registry (see registry.py), so that any code importing
``code_indexer.services.temporal.embedders`` (directly or transitively) can
call ``create_embedder(name, config)`` for any first-class adapter name.
"""

from . import contextual  # noqa: F401  (import triggers self-registration)
