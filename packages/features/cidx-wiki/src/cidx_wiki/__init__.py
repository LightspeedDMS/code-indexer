"""The wiki (the ``cidx-wiki`` feature): repository markdown as articles.

Its own distribution (``packages/features/cidx-wiki``), registered with the
code-indexer core as the ``wiki`` feature (``cidx_wiki.feature:make_feature``).
Importing this package loads nothing of the server side.
"""

from cidx_wiki._version import __version__ as __version__
