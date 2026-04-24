---
name: browse_directory
category: search
required_permission: query_repos
tl_dr: List files with metadata (size, language, modified date) - flat list for filtering/sorting.
---

TL;DR: List files with metadata (size, language, modified date) - flat list for filtering/sorting. WHEN TO USE: (1) Find files by pattern, (2) Filter by language/size, (3) Programmatic file listing. COMPARISON: browse_directory = flat list with metadata | directory_tree = visual ASCII hierarchy. RELATED TOOLS: directory_tree (visual hierarchy), get_file_content (read files), list_files (simple file listing). QUICK START: browse_directory('backend-global', path='src') lists files in src/ directory. EXAMPLE: browse_directory('backend-global', path='src/auth', language='python') Returns: {"success": true, "structure": {"path": "src/auth", "files": [{"path": "src/auth/login.py", "size_bytes": 2048, "modified_at": "2024-01-15T10:30:00Z", "language": "python", "is_indexed": true}], "total": 5}}