---
name: git_search_diffs
category: git
required_permission: query_repos
tl_dr: Find when specific code was added/removed in git history (pickaxe search).
---

TL;DR: Find when specific code was added/removed in git history (pickaxe search). WHAT IS PICKAXE? Git's term for searching code CHANGES (not commit messages). Finds commits where text was introduced or deleted. WHEN TO USE: (1) 'When was this function added?', (2) 'Who introduced this bug?', (3) Track code pattern evolution. WHEN NOT TO USE: Search commit messages -> use git_search_commits instead. WARNING: Can be slow on large repos (may take 1-3+ minutes). Start with limit=5. RELATED TOOLS: git_search_commits (searches commit messages), git_blame (who wrote current code), git_show_commit (view commit details).