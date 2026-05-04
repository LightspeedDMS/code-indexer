---
name: feedback_convert_tool_docs_destructive
description: NEVER run tools/convert_tool_docs.py — it regenerates 165 tool_docs/*.md files from TOOL_REGISTRY metadata WITHOUT inputSchema, silently breaking the entire MCP tool surface
type: feedback
originSessionId: b9d30933-4310-4720-b1e5-ecdb8e30a6b6
---
NEVER run `tools/convert_tool_docs.py` (or have a subagent run it) without explicit user approval and a full diff review.

**Why**: The script regenerates `src/code_indexer/server/mcp/tool_docs/*.md` files from `TOOL_REGISTRY` Python metadata, but the regenerated files OMIT the `inputSchema` blocks. `build_tool_registry()` in `tool_doc_loader.py` filters out any tool whose doc has no `inputSchema` — so after running the script, MCP `tools/list` returns near-empty (only tools whose docs were edited by hand AFTER conversion are kept).

**Past incident (2026-05-03 session, Epic #968 Story #972 work)**: A code-surgeon ran `convert_tool_docs.py` while resolving a duplicate `admin/xray_search.md` cleanup. Result: 133 tool_docs files mass-mutated (5728 lines deleted), TOOL_REGISTRY collapsed from 134 tools to 1 (only the new `xray_explore` survived). Required `git checkout development -- src/code_indexer/server/mcp/tool_docs/` to recover.

**How to apply**:
- If a subagent task description suggests running `convert_tool_docs.py` or `verify_tool_docs.py` for cleanup, REJECT or rewrite the prompt to use direct .md edits instead
- If any session needs new tool docs added: hand-write the .md file under the appropriate category subdirectory with full inputSchema YAML
- The verify script (`verify_tool_docs.py`) is read-only and safe to run for diagnostics, but its "extra docs" / "missing" output reflects pre-existing technical debt across ~134 admin/ duplicates — don't try to "fix" by running the conversion script
- Duplicate-name handling in `tool_doc_loader.py` was downgraded to `logger.warning + continue` (first-occurrence-wins) so duplicates produce warnings but don't crash startup
