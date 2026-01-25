---
name: list_files
category: search
required_permission: query_repos
tl_dr: List all files in repository with metadata (size, modified_at, language, is_indexed).
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
      description: 'How to aggregate file listings across multiple repositories. ''global'' (default): Returns files sorted
        by repo then path - shows all files in order. ''per_repo'': Distributes limit evenly across repos - ensures balanced
        representation (e.g., limit=30 across 3 repos returns ~10 from each repo).'
    path:
      type: string
      description: 'Directory path to list files from (optional). Lists all files IN the specified directory. Example: path=''src/auth''
        lists files matching ''src/auth/**/*'' pattern.'
    recursive:
      type: boolean
      default: true
      description: 'Whether to recursively list files in subdirectories (default: true). When true, uses ''**/*'' pattern.
        When false, uses ''*'' pattern (only direct children).'
    path_pattern:
      type: string
      description: 'Optional glob pattern to filter files within the directory specified by ''path''. Example: path=''src'',
        path_pattern=''*.py'' lists files matching ''src/**/*.py''. If ''path'' is not specified, applies pattern to entire
        repository.'
    response_format:
      type: string
      enum:
      - flat
      - grouped
      default: flat
      description: 'Response format for omni-search (multi-repo) results. Only applies when repository_alias is an array.


        ''flat'' (default): Returns all results in a single array, each with source_repo field.

        Example response: {"results": [{"file_path": "src/auth.py", "source_repo": "backend-global", "content": "...", "score":
        0.95}, {"file_path": "Login.tsx", "source_repo": "frontend-global", "content": "...", "score": 0.89}], "total_results":
        2}


        ''grouped'': Groups results by repository under results_by_repo object.

        Example response: {"results_by_repo": {"backend-global": {"count": 1, "results": [{"file_path": "src/auth.py", "content":
        "...", "score": 0.95}]}, "frontend-global": {"count": 1, "results": [{"file_path": "Login.tsx", "content": "...",
        "score": 0.89}]}}, "total_results": 2}


        Use ''grouped'' when you need to process results per-repository or display results organized by source.'
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

TL;DR: List all files in repository with metadata (size, modified_at, language, is_indexed). Returns flat list of files with filtering options. QUICK START: list_files('backend-global') returns all files. USE CASES: (1) Inventory repository contents, (2) Check indexing status across files, (3) Find files by path pattern. FILTERING: Use path parameter to scope to directory (path='src/auth'). OMNI-SEARCH: Pass array of aliases (['backend-global', 'frontend-global']) to list files across multiple repos. AGGREGATION: Use aggregation_mode='per_repo' for balanced representation, 'global' for sorted results. RESPONSE FORMATS: 'flat' (default) returns single array with source_repo field, 'grouped' organizes by repository. WHEN NOT TO USE: (1) Need file content -> use get_file_content, (2) Need directory tree view -> use browse_directory or directory_tree, (3) Need to search file content -> use search_code or regex_search. OUTPUT: Returns array of file objects with path, size_bytes, modified_at, language, is_indexed fields. TROUBLESHOOTING: Empty results? Check repository_alias with list_global_repos. RELATED TOOLS: browse_directory (tree view with directories), get_file_content (read file), directory_tree (recursive directory structure). EXAMPLE: {"repository_alias": "backend-global", "path": "src", "path_pattern": "*.py"} returns {"success": true, "files": [{"path": "src/main.py", "size_bytes": 1234, "modified_at": "2024-01-15T10:30:00Z", "language": "python", "is_indexed": true}, {"path": "src/utils.py", "size_bytes": 856, "modified_at": "2024-01-14T09:15:00Z", "language": "python", "is_indexed": true}], "total_files": 12}