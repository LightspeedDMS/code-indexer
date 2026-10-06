# Auto-Update

Every CIDX Server node keeps itself current with a separate systemd service that pulls the tracked git branch,
reinstalls, re-applies the node's bootstrap provisioning, and restarts `cidx-server`. The application server never
pulls, builds or restarts itself; it only writes a restart request that the auto-updater executes.

This guide is for operators. Code: `src/code_indexer/server/auto_update/` (`run_once.py`, `service.py`,
`change_detector.py`, `deployment_lock.py`, `deployment_executor.py`, `templates/`).

## Units

Two units are installed into `/etc/systemd/system/` from `src/code_indexer/server/auto_update/templates/`:

`cidx-auto-update.service`:

```ini
[Service]
Type=oneshot
User={USER}
Environment="PATH={HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Environment="CIDX_SERVER_REPO_PATH={REPO_PATH}"
Environment="CIDX_AUTO_UPDATE_BRANCH={BRANCH}"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.run_once
PrivateTmp=yes
```

`cidx-auto-update.timer`: `OnBootSec=60`, `OnUnitActiveSec=60`. One run of the service is one polling iteration;
the timer starts the next one 60 seconds after the previous run.

| Variable | Meaning | When unset |
|----------|---------|------------|
| `CIDX_SERVER_REPO_PATH` | The repository checkout the server runs from | `/opt/code-indexer-repo` |
| `CIDX_AUTO_UPDATE_BRANCH` | Branch to track | `master` |
| `CIDX_DATA_DIR` | Server data directory (added by the auto-updater itself, see below) | `~/.cidx-server` of the user running the unit |

### Provisioning

- `scripts/install-cidx-server.sh` renders the service template (install user, home, checkout path,
  `--auto-update-branch` or `--branch`), copies the timer, and enables and starts the timer.
- On a node that has `cidx-server` but no auto-update units, run from inside the repository checkout (the command
  takes the checkout path from `git rev-parse --show-toplevel` in the current directory and the user from `$USER`):

  ```bash
  cd ~/code-indexer
  cidx server install-auto-update --branch master
  ```

- `cidx server uninstall-auto-update` stops and disables the timer and removes both unit files.
- `cidx server auto-update-status` shows the timer state, the last run and the last deployment result.

Install a node with the branch its environment tracks. A node installed with the default branch tracks `master`.

## What one run does

`run_once.main()` evaluates these cases in order and handles the first that applies:

1. **Retry.** If `~/.cidx-server/auto-update-status.json` says `pending_restart` or `failed`, run a full deployment
   (below) and restart the server. A run that fails writes `failed` again, so a failing deployment is retried every
   timer interval until it succeeds.
2. **Restart request.** If `~/.cidx-server/restart.signal` exists and is younger than 120 seconds, delete it, apply
   the target launch settings (see [Restart requests and launch settings](#restart-requests-and-launch-settings)),
   restart the server, and write `applied_launch.json`. An older signal is deleted without a restart.
3. **Forced redeploy.** If `~/.cidx-server/pending-redeploy` exists, run a full deployment and restart, then delete
   the marker.
4. **Change detection.** Run `git fetch origin <branch>` and compare `HEAD` with `origin/<branch>`. If they differ,
   take the deployment lock (`~/.cidx-server/cidx-auto-update.lock`; a second concurrent run skips), run a full
   deployment, and restart the server if it succeeded.

## Deployment steps

`DeploymentExecutor.execute()` runs these steps in order. "Fatal" means the deployment stops, the status file says
`failed`, and the server is not restarted. Every other step logs a WARNING or ERROR and the deployment continues.
Each self-heal step is idempotent: it checks the current state and changes it only when it differs, so it repairs
nodes provisioned by older installers.

| # | Step (method) | What it ensures | Fatal |
|---|---------------|-----------------|-------|
| 0 | `TMPDIR` | Sets `TMPDIR` for all child processes (the unit runs with `PrivateTmp=yes`) | - |
| 1 | `git_pull()` | `git pull origin <branch>` in the checkout | yes |
| 1.1 | self-restart | If the pull changed the auto-updater's own code: import-tests the new `run_once`, writes status `pending_restart` and the `pending-redeploy` marker, restarts `cidx-auto-update`, and returns. The next run (case 1) finishes the deployment on the new code. If the import test fails, the status becomes `failed` and nothing restarts | - |
| 1.5 | `git_submodule_update()` | `third_party/hnswlib` submodule is initialized (falls back to a standalone clone under `/var/tmp/cidx-hnswlib`) | no |
| 1.6 | `_build_hnswlib_with_fallback()` | The custom hnswlib fork is built into the server's Python (skipped when the submodule commit equals the last built one) | yes |
| 1.7 | `_ensure_cli_hnswlib_capability()` | The CLI's separate Python environment also has the fork (see [Custom hnswlib Build](hnswlib-custom-build.md)) | no |
| 1.8 | `_ensure_cli_dependencies_synced()` | The CLI's Python environment has all current dependencies | no |
| 2 | `pip_install()` | The package and its dependencies are installed into the server's Python | yes |
| 3 | `_ensure_launch_config("DEPLOY")` | `cidx-server` `ExecStart` `--host/--port/--workers` match `applied_launch.json`; when that file is missing the live `ExecStart` is preserved | no |
| 4 | `_ensure_cidx_repo_root()` | `CIDX_REPO_ROOT` in the `cidx-server` unit | no |
| 5 | `_ensure_git_safe_directory()` | git `safe.directory` entry for the `cidx-server` unit's `WorkingDirectory` (the checkout), for the unit's `User=` | no |
| 5.5 | `_ensure_git_safe_directory_wildcard()` | `safe.directory = *` for the service user | no |
| 5.6 | `_ensure_safe_directory_entries_deduplicated()` | Duplicate `safe.directory` entries collapsed | no |
| 6 | `_ensure_auto_updater_uses_server_python()` | `cidx-auto-update` runs the same Python as `cidx-server` | no |
| 6.5 | `_ensure_data_dir_env_var()` | `CIDX_DATA_DIR` in `cidx-auto-update`, so both units use the same signal and marker paths | no |
| 6.55 | `_ensure_auto_update_service_has_cli_path()` | `Environment="PATH=..."` in `cidx-auto-update` | no |
| 6.6 | `_ensure_malloc_arena_max()` | `MALLOC_ARENA_MAX=2` present in `cidx-server` when bootstrap `enable_malloc_arena_max` is true (default), removed when false | no |
| 6.65 | `ensure_nodejs()` | A pinned Node.js LTS under `/opt/node`, and `/opt/node/bin` on the `cidx-server` `PATH` | no |
| 6.7 | `_ensure_codex_cli_installed()` | Codex CLI installed or updated through npm | no |
| 7 | `ensure_ripgrep()` | ripgrep installed (x86_64 Linux) | no |
| 7.1 | `ensure_scip_python()` | scip-python installed through npm | no |
| 8 | `_ensure_sudoers_restart()` | A sudoers rule in `/etc/sudoers.d/cidx-server` letting the service user restart `cidx-server` without a password | no |
| 9 | `_ensure_memory_overcommit()` | `vm.overcommit_memory=1`, persisted in `/etc/sysctl.d/99-cidx-memory.conf` | no |
| 10 | `_ensure_swap_file()` | A 4 GB `/swapfile` when no swap is active (best effort) | no |
| 11 | `_ensure_claude_cli_updated()` | An installed Claude CLI is at the latest version | no |
| 12 | `_ensure_pace_maker_installed()` | pace-maker cloned or pulled and installed (see [pace-maker](#pace-maker)) | no |
| 13 | `_ensure_claude_cli_installed()` | Claude CLI installed when absent from `PATH` | no |
| 14 | `_ensure_nfs_research_symlinks()` | Cluster nodes with an `ontap.mount_point`: `~/.claude` and `~/.cidx-server/research` are symlinks into that mount | no |
| 14.5 | `_ensure_activated_repos_symlink_for_cow_daemon()` | `clone_backend: cow-daemon`: `~/.cidx-server/data/activated-repos` is a symlink to `<cow_daemon.mount_point>/activated-repos` | no |
| 14.6 | `_ensure_daemon_storage_path()` | `clone_backend: cow-daemon`: `cow_daemon.daemon_storage_path` is filled in `config.json` when empty, from `CIDX_COW_DAEMON_STORAGE_PATH` or, on the daemon host, the `base_path` in `/etc/cow-storage-daemon/config.json` when the cidx-server user can read that file; never overwrites a set value | no |
| 14.7 | `_ensure_golden_repos_symlink_for_cow_daemon()` | `clone_backend: cow-daemon`: `~/.cidx-server/data/golden-repos` is a symlink to `<cow_daemon.mount_point>/golden-repos` | no |
| 14.8 | `_ensure_cow_storage_mount_options()` | `clone_backend: cow-daemon`: the `/etc/fstab` entry for `cow_daemon.mount_point` is type `nfs` with `vers=3,nolock`; after a rewrite it tries an unmount/mount cycle and leaves a busy mount alone | no |
| 14.9 | `_ensure_cow_daemon_user_in_service_group()` | On the CoW daemon host only: the daemon's OS user belongs to its `service_group` and the running daemon carries that group | no |
| 15 | `_ensure_systemd_claude_path()` | `~/.local/bin` on the `cidx-server` `PATH` | no |
| 16 | `_ensure_rust_toolchain()` | Rust toolchain installed, `/opt/rust/bin` on the `cidx-server` `PATH`, and the `xray-cli` binary built | yes |

The symlink steps (14.5, 14.7) migrate an existing non-empty real directory by moving it aside to
`<dir>.legacy.bug<N>` before creating the symlink, and roll back if the symlink cannot be created. A symlink that
points elsewhere is re-pointed. The installer's `ensure_cow_symlink` performs the same steps on fresh installs; see
[CoW Storage Setup](cow-storage-setup.md).

The first deployment on a new host compiles hnswlib, installs Node.js and the Rust toolchain, and builds
`xray-cli`; it takes several minutes. Most systemd and sudo operations use a 120-second timeout with bounded retry
(`SYSTEMD_OP_TIMEOUT_SECONDS`), because that first deployment can keep the host busy enough to slow `systemctl`.
Several sudo calls run without a timeout, among them: the final `sudo systemctl restart cidx-server` in
`restart_server()`; the `sudo tee` unit-file writes and `sudo systemctl daemon-reload` calls of several unit
self-heals (launch settings, `CIDX_REPO_ROOT`, the `PATH` entries); and the `sudo mkdir` / `sudo chown` that prepare
`/opt/node` and `/opt/rust`. A host where sudo or systemd hangs can therefore hold a deployment indefinitely.

After a deployment that changed the auto-updater's own code (step 1.1), the server can be restarted twice: the
retry run (case 1) deploys and restarts but does not delete the `pending-redeploy` marker, so the next run
(case 3) deploys and restarts again. The second restart is redundant but harmless apart from interrupting jobs
again.

## Restart and drain

After a successful deployment, and for every restart request, `restart_server()`:

1. Mints a short-lived admin JWT from the server's own signing secret (`~/.cidx-server/.jwt_secret` in standalone
   mode, the `cluster_secrets` table in cluster mode) and calls `POST /api/admin/maintenance/enter` on the server
   URL taken from `applied_launch.json` or the live `ExecStart`.
2. Polls `GET /api/admin/maintenance/drain-status` every 10 seconds until it reports `drained: true`, for at most
   the server's recommended drain timeout (`GET /api/admin/maintenance/drain-timeout`: 1.5 x
   `resource_config.git_refresh_timeout`, 5400 seconds with the 3600-second default; 7200 seconds when the endpoint
   cannot be reached). It stops waiting after three consecutive connection errors (the server is already down).
   On timeout it logs each running job at WARNING and continues.
3. Runs `sudo systemctl restart cidx-server`.

Maintenance mode is held in the memory of the server process, so the restart clears it.

Limitation in the current code: drain status counts running and queued jobs only from job trackers registered
with the maintenance service (`MaintenanceState.register_job_tracker`), and no production code registers one. The
drain step therefore reports `drained: true` at once, and jobs still running when the restart happens are
interrupted. They are marked `interrupted` when the server starts again (they do not count as failures in
`/health`). Resubmit them, or schedule deployments when no long job is running. See
[Maintenance and Jobs](maintenance-and-jobs.md).

## Restart requests and launch settings

`host`, `port`, `workers` and `log_level` are runtime settings (Web UI, Configuration, Server section). Changing
them does not restart anything. The values reach the running process this way:

- The Web UI restart action (`POST /admin/restart`, elevation required) writes `~/.cidx-server/launch.json` with
  the target values and, under systemd, writes `~/.cidx-server/restart.signal`. In cluster mode it instead
  advances a shared restart generation; each node's server sees the generation ahead of its
  `applied_launch.json`, writes `launch.json` and its own `restart.signal`.
- The next auto-update run (case 2 above) rewrites the `cidx-server` `ExecStart` flags from `launch.json`
  (`_ensure_launch_config("APPLY")`), restarts the server, and records the applied values and generation in
  `applied_launch.json`. If the values fail validation the restart is skipped.
- A normal code deployment uses `applied_launch.json` (step 3), so it never applies a saved but not yet requested
  change.

A server whose requested generation stays ahead of the applied one logs, after about ten consecutive 30-second
polls, a WARNING ending in `check cidx-auto-update service status`. That message means the auto-update timer is
missing or stopped on that node.

## pace-maker

pace-maker throttles Claude CLI usage. Installation and enforcement are separate:

- **Installation (bootstrap).** The installer and step 12 clone `claude-pace-maker` into the service user's home
  (`~/claude-pace-maker`), or pull it if present, and run its `install.sh` with `NONINTERACTIVE=1`. On a fresh
  clone they run `pace-maker off`, so a new node starts with the master switch off. Updates never change
  pace-maker's own configuration. Both record the checkout path as the bootstrap key `pace_maker_clone_path` in
  `config.json`. Any failure is logged and the deployment continues.
- **Enforcement (runtime).** The runtime setting `pace_maker_mode` (Web UI configuration, default `disabled`) is
  checked before every Claude CLI invocation by `ClaudeInvoker.invoke()` and by Research Assistant background runs
  (`enforce_pace_maker_config()` in `src/code_indexer/server/services/pace_maker_guard.py`). Codex invocations are
  not guarded.

| `pace_maker_mode` | Behaviour before each Claude CLI call |
|-------------------|---------------------------------------|
| `disabled` | Never touches pace-maker |
| `on` | Ensures pacing-only mode: master on, 5-hour and weekly limits on; tempo, intent validation, TDD, reminders, Langfuse, memory localization and danger-bash off. Corrects drift and logs a WARNING |
| `off` | Turns the master switch off if pace-maker reports itself active |

The guard never raises and does nothing when the `pace-maker` command is not on `PATH`.

## Operator rules

Do:

- Keep `cidx-auto-update.timer` enabled on every node. Without it the node never updates, Web UI restarts and
  launch-setting changes never apply.
- Install each node with the branch its environment tracks (`--branch`, `--auto-update-branch`, or
  `cidx server install-auto-update --branch`), and keep the checkout on that branch.
- Change bootstrap keys in `config.json` and restart promptly; change everything else in the Web UI. Every Web UI
  settings save rewrites `config.json` from the bootstrap values the running process loaded at its start, so do
  not save settings between editing `config.json` and the restart.
- Watch a deployment with `journalctl -u cidx-auto-update -f`.

Do not:

- Commit or leave uncommitted changes in the server checkout; `git pull` fails and the deployment is retried
  every minute.
- Edit the `ExecStart` flags, managed `Environment=` lines or `MALLOC_ARENA_MAX` of `cidx-server` by hand, or edit
  `launch.json`/`applied_launch.json`; the next run rewrites them.
- Rely on the drain step to protect long-running jobs (see [Restart and drain](#restart-and-drain)).
- Restart `cidx-server` with anything other than systemd.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Node never advances to the new version | `systemctl list-timers cidx-auto-update.timer`; `systemctl show cidx-auto-update.service -p Environment` for the tracked branch |
| Same deployment repeats every minute | `~/.cidx-server/auto-update-status.json` says `failed`; the journal shows the failing step and its `DEPLOY-GENERAL-*` code |
| `Could not obtain auth token for maintenance mode` | The JWT secret is not readable by the unit's user, or (cluster) `postgres_dsn` is missing; the restart still happens |
| `cannot resolve cidx-server URL` | Neither `applied_launch.json` nor a `cidx-server` unit with `--host/--port` exists; re-run the installer |
| Web UI restart does nothing | The timer is not running, or the signal aged past 120 seconds before a run picked it up |

Confirm the applied version after a deployment: `GET /health` (authenticated) returns a `version` field, and
`cidx --version` prints the installed CLI version.
