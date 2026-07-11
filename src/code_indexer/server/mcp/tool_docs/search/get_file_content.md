---
name: get_file_content
category: search
required_permission: query_repos
tl_dr: Read file content with metadata and line-offset pagination to avoid context exhaustion.
slim_description: "Read file content from an indexed repository with line-offset pagination (default ~5000 tokens / ~200-250 lines per response)."
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
    file_content:
      type: array
      description: Array of file content blocks. Named file_content (not content) to avoid collision with the MCP protocol CallToolResult.content wrapper field.
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
        next_offset:
          type:
          - integer
          - 'null'
          description: >
            Line number to pass as offset in the next call to continue reading
            (equals offset + returned_lines). Null when has_more is false (no
            more content). This is the canonical pagination cursor -- always
            prefer next_offset over manually computing offset + returned_lines.
            cache_handle / byte-envelope pagination are retired; next_offset is
            the single pagination signal for this tool (Bug #1080).
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Read file content from an indexed repository with line-offset pagination. Default returns FIRST CHUNK ONLY (up to ~5000 tokens, ~200-250 lines), NOT the entire file.

PAGINATION CONTRACT (Bug #1080 - line-offset only): Check metadata.has_more in response. If true, call again with offset=metadata.next_offset. Repeat until has_more=false. All pagination is line-based -- no byte-envelopes, no cache_handle, no page numbers.

- metadata.next_offset: canonical next-page cursor (offset + returned_lines, or null when done)
- metadata.returned_lines: number of complete lines in this response (never cut mid-line)
- metadata.has_more: true if more lines exist beyond the returned range
- metadata.truncated: true if content was cut by the token budget (same as has_more when token limit fires)

BUDGET OVERRUN: A single line larger than the token budget is always returned whole (one-response overrun is allowed to guarantee termination).

QUICK START: get_file_content(repository_alias='backend-global', file_path='src/auth.py') returns first ~250 lines.
For next chunk: get_file_content(repository_alias='backend-global', file_path='src/auth.py', offset=metadata.next_offset)

TOKEN BUDGET: ~5000 tokens max per response. Small files returned completely. Large files chunked automatically. metadata.estimated_tokens shows actual size.