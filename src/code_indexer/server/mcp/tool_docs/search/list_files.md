---
name: list_files
category: search
required_permission: query_repos
tl_dr: List all files in a repository with metadata (size, modified_at, language,
  is_indexed).
---

List all files in a repository with metadata (size, modified_at, language, is_indexed). Useful for exploring repository structure before searching.

USE CASE: Inventory repository contents, check indexing status, or find files by path pattern before using get_file_content or search_code.

EXAMPLE: list_files(repository_alias='backend-global', path='src', path_pattern='*.py')