---
name: directory_tree
category: search
required_permission: query_repos
tl_dr: Visual ASCII tree of directory structure (like 'tree' command).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias or full path.
    path:
      type: string
      description: Subdirectory to use as tree root (relative to repo root).
    max_depth:
      type: integer
      description: 'Maximum depth to display. Deeper directories show ''[...]'' indicator.'
      default: 3
      minimum: 1
      maximum: 10
    max_files_per_dir:
      type: integer
      description: 'Maximum files per directory before truncating. Directories with more show ''[+N more files]''.'
      default: 50
      minimum: 1
      maximum: 200
    include_patterns:
      type: array
      items:
        type: string
      description: Glob patterns for files to include. Only matching files shown; directories shown if they contain matches.
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns to exclude. Default excludes: .git, node_modules, __pycache__, .venv, .idea, .vscode. Additional
        patterns merged with defaults.'
    show_stats:
      type: boolean
      description: Show file/directory count statistics.
      default: false
    include_hidden:
      type: boolean
      description: 'Include hidden files/directories (starting with dot). Note: .git always excluded.'
      default: false
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    tree_string:
      type: string
      description: Pre-formatted tree output with ASCII characters
    root:
      type: object
      description: Root TreeNode with hierarchical structure
      properties:
        name:
          type: string
          description: Directory/file name
        path:
          type: string
          description: Relative path from repo root
        is_directory:
          type: boolean
          description: True if directory
        children:
          type:
          - array
          - 'null'
          description: Child nodes (null for files)
        truncated:
          type: boolean
          description: True if max_files exceeded
        hidden_count:
          type: integer
          description: Number of hidden children
    total_directories:
      type: integer
      description: Total number of directories
    total_files:
      type: integer
      description: Total number of files
    max_depth_reached:
      type: boolean
      description: Whether max_depth limit was reached
    root_path:
      type: string
      description: Filesystem path to tree root
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Visual ASCII tree of directory structure (like 'tree' command). WHEN TO USE: (1) Understand project layout, (2) Explore unfamiliar codebase, (3) Find where files are located. COMPARISON: directory_tree = visual hierarchy | browse_directory = flat list with metadata (size, language, dates). RELATED TOOLS: browse_directory (flat list with file details), get_file_content (read files). QUICK START: directory_tree('backend-global') returns visual file tree. EXAMPLE: directory_tree('backend-global', path='src', max_depth=2) Returns: {"success": true, "tree": "src/\n├── auth/\n│   ├── login.py\n│   └── logout.py\n├── api/\n│   └── routes.py\n└── main.py", "stats": {"directories": 3, "files": 4}}