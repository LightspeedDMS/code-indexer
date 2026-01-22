---
name: regex_search
category: search
required_permission: query_repos
tl_dr: Direct pattern search on files without index - comprehensive but slower.
---

TL;DR: Direct pattern search on files without index - comprehensive but slower. WHEN TO USE: (1) Find exact text/identifiers: 'def authenticate_user', (2) Complex patterns: 'class.*Controller', (3) TODO/FIXME comments, (4) Comprehensive search when you need ALL matches (not approximate). WHEN NOT TO USE: (1) Conceptual queries like 'authentication logic' -> use search_code(semantic), (2) Fast repeated searches -> use search_code(fts) which is indexed. COMPARISON: regex_search = comprehensive/slower (searches files directly) | search_code(fts) = fast/indexed (may miss unindexed files) | search_code(semantic) = conceptual/approximate (finds by meaning, not text). RELATED TOOLS: search_code (pre-indexed semantic/FTS search), git_search_diffs (find code changes in git history). QUICK START: regex_search('backend-global', 'def authenticate') finds all function definitions. EXAMPLE: regex_search('backend-global', 'TODO|FIXME', include_patterns=['*.py'], context_lines=1) Returns: {"success": true, "matches": [{"file_path": "src/auth.py", "line": 42, "content": "# TODO: add input validation", "context_before": ["def login(user):"], "context_after": ["    pass"]}], "total_matches": 3}