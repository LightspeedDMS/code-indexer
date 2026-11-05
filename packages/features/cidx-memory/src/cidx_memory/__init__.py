"""Shared technical memories (the ``cidx-memory`` feature).

Its own distribution (``packages/features/cidx-memory``), registered with the
code-indexer core as the ``memory`` feature
(``cidx_memory.feature:make_feature``). Importing this package loads nothing
of the server side.
"""

from cidx_memory._version import __version__ as __version__
