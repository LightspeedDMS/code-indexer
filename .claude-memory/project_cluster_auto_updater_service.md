---
name: project_cluster_auto_updater_service
description: The CIDX auto-updater is a SEPARATE systemd service (cidx-auto-update.service + timer), NOT part of cidx-server — the cluster installer must provision it and set CIDX_AUTO_UPDATE_BRANCH (staging nodes track staging, else default master)
metadata:
  type: project
---

Server updates are NOT applied by cidx-server itself. They are applied by a separate systemd oneshot `cidx-auto-update.service` fired by `cidx-auto-update.timer` every 60s, which runs `python3 -m code_indexer.server.auto_update.run_once`. cidx-server only writes a restart signal; if the auto-update units are missing, the node NEVER self-updates and the server logs `launch_restart_generation target > applied ... check cidx-auto-update service status` indefinitely.

`run_once` reads the tracked branch from the `CIDX_AUTO_UPDATE_BRANCH` env var (unset -> defaults to `master`). It git-fetches that branch; on a remote-vs-local diff it does git pull -> build + pip install (hnswlib submodule, ripgrep, Rust toolchain if missing, pace-maker, Claude CLI) -> systemd/config hardening (MALLOC_ARENA_MAX, sudoers self-restart rule, memory overcommit, swap) -> restart cidx-server. When the update changes the updater's OWN code, it self-restarts mid-deploy via a pending-redeploy marker; a `Failed with result 'signal'` journal line at that seam is EXPECTED, not an error. First deploy on a fresh node is heavy (compiles hnswlib + installs Rust, several minutes); later deploys are fast.

`scripts/install-cidx-server.sh` provisions BOTH units (fixed in v11.21.0 — the earlier installer created only `cidx-server.service`, leaving fresh cluster nodes with no auto-updater). It renders the service from `src/code_indexer/server/auto_update/templates/cidx-auto-update.service` and threads the branch via `--branch` / `--auto-update-branch`. STAGING NODES MUST be installed/retrofitted with the branch set to `staging`, or they silently track `master`. Retrofit an existing node with the shipped CLI: `cidx server install-auto-update --branch staging` (the `--branch` option exists as of v11.21.0).

Verified live: once the timer was installed on all three staging nodes, each pulled and deployed a pushed `staging` release hands-free. Operate via `systemctl list-timers cidx-auto-update.timer` (next fire) and `journalctl -u cidx-auto-update -f` (deploy log). See [[project_nfs_host_down_hangs_systemd]].
