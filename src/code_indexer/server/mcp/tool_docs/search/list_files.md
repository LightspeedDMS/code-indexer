---
name: list_files
category: search
required_permission: query_repos
tl_dr: List all files in a repository with metadata (size, date, language, indexed).
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository alias(es): String for single repo, array for omni-list-files across multiple repos.'
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      default: global
      description: 'Multi-repo aggregation. ''global'' (default): top N by score across all repos. ''per_repo'': distributes
        N evenly across repos. IMPORTANT: limit=10 with 3 repos returns 10 TOTAL (not 30). per_repo distributes as 4+3+3=10.'
    path:
      type: string
      description: Directory path to list files from.
    recursive:
      type: boolean
      default: true
      description: 'Recursively list subdirectories. When true, uses **/* pattern. When false, uses * pattern (direct children
        only).'
    path_pattern:
      type: string
      description: Glob pattern to filter files within directory specified by path.
    response_format:
      type: string
      enum:
      - flat
      - grouped
      default: flat
      description: 'Multi-repo result format. ''flat'' (default): single array with source_repo field per result. ''grouped'':
        results organized under results_by_repo by repository.'
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    files:
      type: array
      description: List of files in repository
      items:
        type: object
        properties:
          path:
            type: string
            description: Relative file path
          size_bytes:
            type: integer
            description: File size in bytes
          modified_at:
            type: string
            description: ISO 8601 last modification timestamp
          language:
            type:
            - string
            - 'null'
            description: Detected programming language
          is_indexed:
            type: boolean
            description: Whether file is indexed
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

List all files in a repository with metadata (size, modified_at, language, is_indexed). Useful for exploring repository structure before searching.

USE CASE: Inventory repository contents, check indexing status, or find files by path pattern before using get_file_content or search_code.

EXAMPLE: list_files(repository_alias='backend-global', path='src', path_pattern='*.py')