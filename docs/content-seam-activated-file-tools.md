# Content seam: file tools on activated repositories

With `content_backend: delegated` a user's activated working copy lives on
workspace-mcp's storage at `<activated_repos_dir>/<user>/<alias>`; this pod has
no copy. With `content_backend: local` (the default) nothing here applies.

| Tool | Under delegated |
|---|---|
| `list_files`, `browse_directory` (activated alias) | registry gate for THIS user, then the content backend's manifest at the copy's path |
| `get_file_content` | registry gate, then fetch; `content_hash` is SHA-256 of the bytes (what edit/delete compare) |
| `create_file` / `edit_file` / `delete_file` | registry gate, then the content writer at the copy's path (create = must not exist, edit/delete = blob-sha CAS) |
| `regex_search` (activated alias) | the path goes to the delegated ripgrep backend unprobed |
| auto-watch after a write | skipped (nothing on the pod to watch); reindex with `trigger_reindex` |

Fail loud: a user without the activation gets not-found and nothing is fetched;
an unreachable backend or a missing copy raises (never an empty repository); no
writer parked refuses the write (never this pod's disk).

The listing cache keeps its 30 s TTL under delegated, the same staleness the
local walk cache already has.

## Cutover checklist (deployment)

* workspace-mcp `WSM_CONTENT_ALLOWED_ROOTS` covers the activated-repos root (reads).
* workspace-mcp `WSM_CONTENT_WRITE_ROOTS` covers the activated-repos root (writes).
* The content writer plugin is installed (startup refuses delegated without one).
