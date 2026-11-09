"""SCIP code intelligence (the ``cidx-scip`` feature).

Its own distribution (``packages/features/cidx-scip``), registered with the
code-indexer core as the ``scip`` feature (``cidx_scip.feature:make_feature``).
The engine (index generation, the SCIP database, queries) imports nothing
heavy at package import; the server side lives in ``cidx_scip.server`` and is
loaded only by the core, through the feature's hooks.
"""

from cidx_scip._version import __version__ as __version__
