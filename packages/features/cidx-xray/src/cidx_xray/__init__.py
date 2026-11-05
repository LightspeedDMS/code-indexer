"""X-Ray: Precision AST-Aware Code Search (the ``cidx-xray`` feature).

This package provides the core AST search engine using lazy-loaded tree-sitter.
Import tree_sitter and tree_sitter_languages only occur at AstSearchEngine
instantiation time, ensuring CLI startup is unaffected when X-Ray is not used.

It is its own distribution (``packages/features/cidx-xray``) and registers
with the code-indexer core as the ``xray`` feature
(``cidx_xray.feature:make_feature``). The core's old import path,
``code_indexer.xray``, is a compatibility shim over this package.
"""

from cidx_xray._version import __version__ as __version__
