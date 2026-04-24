---
name: get_file_content
category: search
required_permission: query_repos
tl_dr: Read file content from an indexed repository with token-based pagination.
---

Read file content from an indexed repository with token-based pagination. Default returns FIRST CHUNK ONLY (up to ~5000 tokens, ~200-250 lines), NOT the entire file.

PAGINATION: Check metadata.requires_pagination in response. If true, call again with offset from metadata.pagination_hint. Repeat until requires_pagination=false.

QUICK START: get_file_content(repository_alias='backend-global', file_path='src/auth.py') returns first ~250 lines.
For next chunk: get_file_content(repository_alias='backend-global', file_path='src/auth.py', offset=251, limit=250)

TOKEN BUDGET: ~5000 tokens max per response. Small files returned completely. Large files chunked automatically. metadata.estimated_tokens shows actual size.

COMPOSITE REPOS: For composite repositories, include source_repo parameter to specify which component repo contains the file.