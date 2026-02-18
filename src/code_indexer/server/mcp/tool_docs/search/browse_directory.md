---
name: browse_directory
category: search
required_permission: query_repos
tl_dr: List files with metadata (size, language, date) - flat list for filtering.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias to browse.
    path:
      type: string
      description: Subdirectory path to browse (relative to repo root).
    recursive:
      type: boolean
      description: When true (default), returns all files in directory and subdirectories. When false, returns only immediate
        children (single level).
      default: true
    path_pattern:
      type: string
      description: 'Glob pattern to filter files. Supports: * (any chars), ** (any path segments), ? (single char), [seq] (char
        class).'
    language:
      type: string
      description: 'Filter by programming language name or extension (e.g., ''python'', ''py'', ''js'', ''typescript'').'
    limit:
      type: integer
      description: 'Maximum files to return. IMPORTANT: Start with 50-100 to conserve context tokens. Default 500 is high for
        most tasks.'
      default: 500
      minimum: 1
      maximum: 500
    sort_by:
      type: string
      description: 'Sort order: ''path'' (alphabetical), ''size'' (by file size), ''modified_at'' (by modification time).'
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