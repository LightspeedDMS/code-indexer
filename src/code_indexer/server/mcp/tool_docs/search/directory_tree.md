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
      description: 'Repository identifier: either an alias (e.g., ''my-project'') or full path (e.g., ''/home/user/repos/my-project'').
        Use list_global_repos to see available repositories and their aliases.'
    path:
      type: string
      description: 'Subdirectory to use as tree root (relative to repo root). Default: repository root. Examples: ''src''
        shows tree starting from src/, ''lib/utils'' shows tree starting from lib/utils/.'
    max_depth:
      type: integer
      description: 'Maximum depth of tree to display. Default: 3. Range: 1-10. Deeper directories show ''[...]'' indicator.
        Use 1 for top-level overview, higher values for detailed exploration.'
      default: 3
      minimum: 1
      maximum: 10
    max_files_per_dir:
      type: integer
      description: 'Maximum files to show per directory before truncating. Default: 50. Range: 1-200. Directories with more
        files show ''[+N more files]''. Use lower values for cleaner output on large directories.'
      default: 50
      minimum: 1
      maximum: 200
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include. Only matching files shown; directories shown if they contain matches.
        Default: all files. Examples: [''*.py''] for Python, [''*.ts'', ''*.tsx''] for TypeScript, [''Makefile'', ''*.mk'']
        for makefiles.'
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files/directories to exclude. Default excludes: .git, node_modules, __pycache__, .venv,
        .idea, .vscode. Additional patterns are merged with defaults. Examples: [''*.log'', ''dist/'', ''build/''].'
    show_stats:
      type: boolean
      description: 'Show statistics: file counts per directory, total files/dirs. Default: false. When true, adds summary
        like ''15 directories, 127 files''.'
      default: false
    include_hidden:
      type: boolean
      description: 'Include hidden files/directories (starting with dot). Default: false. Note: .git is always excluded regardless
        of this setting. Set to true to see .env, .gitignore, .eslintrc, etc.'
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