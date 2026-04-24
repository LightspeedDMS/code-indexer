---
name: directory_tree
category: search
required_permission: query_repos
tl_dr: Visual ASCII tree of directory structure (like 'tree' command).
---

TL;DR: Visual ASCII tree of directory structure (like 'tree' command). WHEN TO USE: (1) Understand project layout, (2) Explore unfamiliar codebase, (3) Find where files are located. COMPARISON: directory_tree = visual hierarchy | browse_directory = flat list with metadata (size, language, dates). RELATED TOOLS: browse_directory (flat list with file details), get_file_content (read files). QUICK START: directory_tree('backend-global') returns visual file tree. EXAMPLE: directory_tree('backend-global', path='src', max_depth=2) Returns: {"success": true, "tree": "src/\n├── auth/\n│   ├── login.py\n│   └── logout.py\n├── api/\n│   └── routes.py\n└── main.py", "stats": {"directories": 3, "files": 4}}