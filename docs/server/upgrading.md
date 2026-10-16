# Upgrading CIDX Server

This is the entry point for moving an installed node to a newer version. A node upgrades by moving its repository
checkout to the tip of the branch it tracks; the auto-updater performs every step. For how the auto-updater works
in detail, see [Auto-Update](auto-update.md).

## Automatic upgrade (default)

A node installed with `scripts/install-cidx-server.sh` has `cidx-auto-update.timer`. Every 60 seconds the
auto-updater fetches the tracked branch (`CIDX_AUTO_UPDATE_BRANCH`, default `master`) and, when `origin/<branch>`
differs from the checkout's `HEAD`, deploys it and restarts the server. Publishing a new version to the tracked
branch is therefore the whole upgrade: each node picks it up within about a minute and deploys independently.

Check what a node tracks:

```bash
systemctl show cidx-auto-update.service -p Environment
```

A node without the timer never upgrades. Add it from inside the checkout:

```bash
cd ~/code-indexer
cidx server install-auto-update --branch master
```

## Upgrading on demand

To run the check and deployment now instead of waiting for the timer:

```bash
sudo systemctl start cidx-auto-update.service
journalctl -u cidx-auto-update -f
```

To redeploy the current checkout even though the branch has not moved (for example after fixing a host problem
that made the last deployment fail), create the redeploy marker; the next run performs a full deployment and
restart, then deletes the marker:

```bash
touch ~/.cidx-server/pending-redeploy
sudo systemctl start cidx-auto-update.service
```

Do not upgrade by running `git pull` in the checkout by hand. The auto-updater then sees `HEAD` equal to
`origin/<branch>`, skips the deployment steps (pip install, hnswlib build, self-heals, restart), and the server keeps
running the old code against new files.

Re-running the installer also upgrades a node, but it rewrites the `cidx-server` unit from its own flags (port,
workers, branch) and so can replace launch settings changed in the Web UI. Prefer the auto-updater.

## What a new version changes

| Area | What happens | Operator action |
|------|--------------|-----------------|
| Code and dependencies | `git pull`, the hnswlib fork build, `pip install`, the Rust `xray-cli` build | None |
| Host provisioning | Every `_ensure_*` self-heal step re-runs and repairs units, `PATH`, symlinks, mount options and tools (see [Deployment steps](auto-update.md#deployment-steps)) | None |
| PostgreSQL schema | Pending migrations apply at server startup under an advisory lock; the log shows `Applied N SQL schema migration(s)`; a failed migration stops startup | None |
| SQLite schema | Tables and columns are created in place at startup | None |
| Runtime settings | A setting added in the new version starts with its default; a setting already stored keeps its stored value, even if the new version ships a different default | Review new settings in the Web UI |
| Bootstrap settings (`config.json`) | Unchanged; the auto-updater adds only `pace_maker_clone_path` and, for the CoW backend, a missing `cow_daemon.daemon_storage_path` | None |

Migrations only add tables, columns and indexes, so cluster nodes on adjacent versions can share one database while
they upgrade one by one.

The restart interrupts jobs that are still running; they are marked `interrupted` at the next start. See
[Restart and drain](auto-update.md#restart-and-drain) and plan upgrades around long indexing jobs.

## Release notes

- `CHANGELOG.md` at the repository root lists the changes in every version.
- Each version is tagged `v<MAJOR>.<MINOR>.<HOTFIX>`, and CI publishes a GitHub release for it.
- The running version is `__version__` in `src/code_indexer/__init__.py`.

## Verify after an upgrade

```bash
cidx server auto-update-status                 # last run and deployment result
cat ~/.cidx-server/auto-update-status.json     # status: success | failed | in_progress | pending_restart
curl -s http://localhost:8000/healthz
journalctl -u cidx-server --since "15 min ago" | grep -E "schema migration|ERROR"
```

The authenticated `GET /health` response includes `version`; compare it with `CHANGELOG.md`. In a cluster, check
every node: each upgrades on its own.

## Rolling back

There is no supported downgrade. The auto-updater moves a node only forward to the tracked branch tip, and schema
migrations are not reversed. To undo a bad release, publish a fixed version to the tracked branch; nodes pick it up
like any other upgrade.

## Historical upgrade guides

These describe upgrades between old major versions. They are not maintained and do not describe current behaviour:

- [v7 to v8](../archive/migration-to-v8.md)
- [v9 to v10](../archive/migration-to-v10.md)
