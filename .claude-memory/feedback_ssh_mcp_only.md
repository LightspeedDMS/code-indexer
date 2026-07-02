---
name: SSH connections must use MCP SSH tools, never Bash ssh
description: NEVER use ssh command via Bash tool — causes authentication failures. Use MCP SSH tools only.
type: feedback
---

NEVER use `ssh` command via Bash tool. Causes authentication failures.

Use MCP SSH tools for ALL remote connections:
- `mcp__ssh__ssh_connect` / `ssh_disconnect` - Session management
- `mcp__ssh__ssh_exec` - Remote commands
- `mcp__ssh__ssh_upload_file` / `ssh_download_file` - SFTP transfers

**Why:** Direct ssh via Bash fails authentication in Claude Code's sandboxed environment. MCP SSH tools handle auth correctly.

**How to apply:** Any time you need to connect to a remote server, use the MCP SSH tools. Never shell out to `ssh` directly.
