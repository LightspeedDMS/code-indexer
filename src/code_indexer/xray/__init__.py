"""X-Ray: Precision AST-Aware Code Search.

This package provides the core AST search engine using lazy-loaded tree-sitter.
Import tree_sitter and tree_sitter_languages only occur at AstSearchEngine
instantiation time, ensuring CLI startup is unaffected when X-Ray is not used.
"""
