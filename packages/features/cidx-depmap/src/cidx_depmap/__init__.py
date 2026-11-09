"""The dependency map (the ``cidx-depmap`` feature).

Its own distribution (``packages/features/cidx-depmap``), registered with the
code-indexer core as the ``depmap`` feature
(``cidx_depmap.feature:make_feature``). Importing this package loads nothing
of the server side.
"""

from cidx_depmap._version import __version__ as __version__
