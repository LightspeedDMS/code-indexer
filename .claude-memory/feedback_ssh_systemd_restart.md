---
name: Server restart must use systemd, never kill+nohup
description: NEVER use kill -15 && nohup for CIDX server restarts — causes SSH lockups. Use systemd only.
type: feedback
---

NEVER use `kill -15 && nohup ...` for CIDX server restarts. Causes SSH lockups.

Use systemd instead:
```bash
mcp__ssh__ssh_exec: echo "PASSWORD" | sudo -S systemctl restart cidx-server
mcp__ssh__ssh_exec: systemctl status cidx-server --no-pager
```

For server passwords, read `.local-testing`.

**Why:** kill+nohup restarts caused SSH sessions to hang/lock up, requiring manual intervention to recover. Systemd handles process lifecycle correctly.

**How to apply:** Any time you need to restart cidx-server on a remote machine, use `systemctl restart cidx-server` via MCP SSH exec. Never kill the process manually.
