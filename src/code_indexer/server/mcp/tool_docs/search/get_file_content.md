---
name: get_file_content
category: search
required_permission: query_repos
tl_dr: Read file content from repository with metadata and token-based pagination to prevent LLM context exhaustion.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_path:
      type: string
      description: File path
    offset:
      type: integer
      minimum: 1
      description: 'Line number to start reading from (1-indexed). Optional. Default: read from beginning. Example: offset=100
        starts at line 100.'
    limit:
      type: integer
      minimum: 1
      description: 'Maximum number of lines to return. Optional. Default: token-limited chunk (up to 5000 tokens). Recommended:
        200-250 lines per request to stay within 5000 token budget. Token limits enforced even if you specify larger limit.
        Use metadata.requires_pagination to detect if more content exists.'
  required:
  - repository_alias
  - file_path
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    content:
      type: array
      description: Array of content blocks (MCP spec compliant)
      items:
        type: object
        properties:
          type:
            type: string
            enum:
            - text
            description: Content block type
          text:
            type: string
            description: File content as text
    metadata:
      type: object
      description: File metadata including pagination info
      properties:
        size:
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
        path:
          type: string
          description: Relative file path
        total_lines:
          type: integer
          description: Total lines in file
        returned_lines:
          type: integer
          description: Number of lines returned in this response
        offset:
          type: integer
          description: Starting line number (1-indexed) for returned content
        limit:
          type:
          - integer
          - 'null'
          description: Limit used (null if unlimited)
        has_more:
          type: boolean
          description: True if more lines exist beyond returned range. Use this to detect when pagination is needed.
        estimated_tokens:
          type: integer
          description: Estimated token count of returned content based on character length and chars_per_token ratio.
        max_tokens_per_request:
          type: integer
          description: 'Current token limit from server configuration (default: 5000).'
        truncated:
          type: boolean
          description: True if content was truncated due to token limit enforcement.
        truncated_at_line:
          type:
          - integer
          - 'null'
          description: Line number where truncation occurred (null if not truncated).
        requires_pagination:
          type: boolean
          description: True if file has more content to read (either due to truncation or more lines beyond current range).
        pagination_hint:
          type:
          - string
          - 'null'
          description: Helpful message with suggested offset value to continue reading (null if no more content).
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Read file content from repository with metadata and token-based pagination to prevent LLM context exhaustion. CRITICAL BEHAVIOR CHANGE: Default behavior (no offset/limit params) now returns FIRST CHUNK ONLY (up to 5000 tokens, ~200-250 lines), NOT entire file. Token limits enforced on ALL requests. QUICK START: get_file_content('backend-global', 'src/auth.py') returns first ~200-250 lines if file is large. Check metadata.requires_pagination to see if more content exists. USE PAGINATION: get_file_content('backend-global', 'large_file.py', offset=251, limit=250) reads next chunk. AUTOMATIC TRUNCATION: Content exceeding token budget is truncated. Check metadata.truncated and metadata.truncated_at_line. Use metadata.pagination_hint for navigation instructions. USE CASES: (1) Read source code after search_code identifies relevant files, (2) Inspect configuration files, (3) Review file content before editing, (4) Navigate large files efficiently with token budgets. TOKEN ENFORCEMENT: Default config: 5000 tokens max per request (~20000 chars at 4 chars/token). Small files returned completely. Large files returned in chunks. metadata.estimated_tokens shows actual token count of returned content. PAGINATION WORKFLOW: (1) Call without params to get first chunk, (2) Check metadata.requires_pagination, (3) If true, use metadata.pagination_hint offset value to continue, (4) Repeat until metadata.requires_pagination=false. OUTPUT FORMAT: Returns array of content blocks following MCP specification - each block has type='text' and text=file_content. Metadata includes file size, detected language, modification timestamp, pagination info, and token enforcement info (estimated_tokens, max_tokens_per_request, truncated, requires_pagination, pagination_hint). WHEN TO USE: After identifying target file via search_code, browse_directory, or list_files. WHEN NOT TO USE: (1) Need file listing -> use list_files or browse_directory, (2) Need to search content -> use search_code or regex_search first, (3) Need directory structure -> use directory_tree. TROUBLESHOOTING: File not found? Verify file_path with list_files or browse_directory. Permission denied? Check repository is activated and accessible. Content truncated unexpectedly? Check metadata.truncated and metadata.estimated_tokens - use offset/limit params to navigate. RELATED TOOLS: list_files (find files), search_code (search content), edit_file (modify content), browse_directory (list with metadata). EXAMPLE: {"repository_alias": "backend-global", "file_path": "src/auth.py"} returns {"success": true, "content": [{"type": "text", "text": "def authenticate(user, password):\n    ..."}], "metadata": {"size": 2048, "modified_at": "2024-01-15T10:30:00Z", "language": "python", "path": "src/auth.py", "total_lines": 85, "returned_lines": 85, "offset": 1, "limit": null, "has_more": false, "estimated_tokens": 450, "max_tokens_per_request": 5000, "truncated": false, "truncated_at_line": null, "requires_pagination": false, "pagination_hint": null}}. PAGINATION EXAMPLE: {"repository_alias": "backend-global", "file_path": "large_file.py", "offset": 251, "limit": 250} returns content starting at line 251 with metadata showing remaining lines.