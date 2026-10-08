# Auto-Updater and Pace-Maker Invariants

Rules for the per-node auto-updater and the pace-maker guard. Operator view of the deployment steps, restart
requests and pace-maker modes: [Auto-Update](../../server/auto-update.md). Index of all invariant groups:
[README](README.md).

## Idempotent deployment

- Any bootstrap change (systemd unit, environment, PATH, file locations, mounts, service wiring) must be automated in
  both the installer (`scripts/install-cidx-server.sh`, `src/code_indexer/server/auto_update/templates/`) and an
  idempotent self-heal step in `DeploymentExecutor` (`src/code_indexer/server/auto_update/deployment_executor.py`).
  Nothing re-renders an already-deployed unit otherwise, so an installer-only fix leaves running nodes broken.
- The auto-updater runs the code installed when its process started. A deploy-time fix therefore takes effect on the
  deploy after the one that installs it.
- Examples of self-heal steps: `_ensure_cow_storage_mount_options` (NFSv3 `nolock` fstab entry),
  `_ensure_activated_repos_symlink_for_cow_daemon` (creates
  `~/.cidx-server/data/activated-repos -> {cow_daemon.mount_point}/activated-repos`; leaves an existing real directory
  with data untouched and logs the manual command), `_ensure_malloc_arena_max` (adds or removes
  `Environment=MALLOC_ARENA_MAX=2` according to the bootstrap flag).

## Launch settings

- `host`, `port`, `workers` and `log_level` are runtime settings in the database, not bootstrap keys.
  `ConfigService.materialize_launch_config()` writes them to `~/.cidx-server/launch.json`; the auto-updater's
  `_ensure_launch_config()` validates them and rewrites `--host`/`--port`/`--workers` in the live `ExecStart` with a
  token-bounded regular expression (so `--workers 1` is never confused with `--workers 10`), restarts the server and
  records what it applied in `applied_launch.json`.
- Consumers that need the worker count the running process was started with read the applied value
  (`server/services/applied_worker_count.py`: live `ExecStart --workers`, then `applied_launch.json`, then 1), not the
  saved target.

## Deployment lock

- `deployment_lock.get_default_lock_path()` is the only source of the lock path:
  `{CIDX_DATA_DIR or ~/.cidx-server}/cidx-auto-update.lock`. Never under `/tmp`: systemd `PrivateTmp=yes` isolates
  `/tmp` per unit.
- Creating the lock file is fail-soft: an `OSError` logs `GIT-GENERAL-003` and the deploy proceeds. A lock failure
  must never make a node permanently un-updatable. A live lock held by a running process still blocks.

## Pace-maker guard

- The auto-updater installs or updates pace-maker (`_ensure_pace_maker_installed`). A fresh install leaves its master
  switch off; updates never touch its configuration.
- Configuration is split: `pace_maker_clone_path` is a bootstrap key; `pace_maker_mode` is a runtime setting
  (default `"disabled"`).
- `enforce_pace_maker_config()` (`src/code_indexer/server/services/pace_maker_guard.py`) is three-way: `disabled`
  never touches pace-maker, `on` enforces pacing-only mode, `off` turns the master switch off.
- It is called from `ClaudeInvoker.invoke()` and `ResearchAssistantService._run_claude_background()`, not from the
  Codex invoker. Failures are logged and never raised.
