---
name: browse_directory
category: search
required_permission: query_repos
tl_dr: List files with metadata (size, language, modified date) - flat list for filtering/sorting.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias to browse. Use '-global' suffix aliases (e.g., 'myproject-global') for persistent global
        repositories, or activated repository aliases without suffix. Use list_repositories to discover available aliases.
    path:
      type: string
      description: 'Subdirectory path within the repository to browse (relative to repository root). Examples: ''src'', ''src/components'',
        ''tests/unit''. Omit or use empty string to browse from repository root.'
    recursive:
      type: boolean
      description: When true (default), returns all files in directory and subdirectories. When false, returns only immediate
        children of the specified directory (single level). Use recursive=false to explore directory structure level by level.
      default: true
    path_pattern:
      type: string
      description: 'Glob pattern to filter files. Combines with ''path'' parameter (pattern applied within the specified directory).
        Supports: * (any chars), ** (any path segments), ? (single char), [seq] (char class). Examples: ''*.py'' (Python files),
        ''test_*.py'' (test files), ''**/*.ts'' (TypeScript at any depth), ''src/**/index.js'' (index files under src).'
    language:
      type: string
      description: 'Filter by programming language. Supported languages: c, cpp, csharp, dart, go, java, javascript, kotlin,
        php, python, ruby, rust, scala, swift, typescript, css, html, vue, markdown, xml, json, yaml, bash, shell, and more.
        Can use friendly names or file extensions (py, js, ts, etc.).'
    limit:
      type: integer
      description: 'Maximum files to return. IMPORTANT: Start with limit=50-100 to conserve context tokens. Each file entry
        consumes tokens for path, size, and metadata. Only increase if you need comprehensive listing. Default 500 is high
        for most exploration tasks.'
      default: 500
      minimum: 1
      maximum: 500
    sort_by:
      type: string
      description: 'Sort order for results. Options: ''path'' (alphabetical by file path - default, good for exploring structure),
        ''size'' (by file size - useful for finding large files), ''modified_at'' (by modification time - useful for finding
        recently changed files).'
      enum:
      - path
      - size
      - modified_at
      default: path
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    structure:
      type: object
      description: Directory structure with files
      properties:
        path:
          type: string
          description: Directory path browsed
        files:
          type: array
          description: Array of file information objects
        total:
          type: integer
          description: Total number of files
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: List files with metadata (size, language, modified date) - flat list for filtering/sorting. WHEN TO USE: (1) Find files by pattern, (2) Filter by language/size, (3) Programmatic file listing. COMPARISON: browse_directory = flat list with metadata | directory_tree = visual ASCII hierarchy. RELATED TOOLS: directory_tree (visual hierarchy), get_file_content (read files), list_files (simple file listing). QUICK START: browse_directory('backend-global', path='src') lists files in src/ directory. EXAMPLE: browse_directory('backend-global', path='src/auth', language='python') Returns: {"success": true, "structure": {"path": "src/auth", "files": [{"path": "src/auth/login.py", "size_bytes": 2048, "modified_at": "2024-01-15T10:30:00Z", "language": "python", "is_indexed": true}], "total": 5}}