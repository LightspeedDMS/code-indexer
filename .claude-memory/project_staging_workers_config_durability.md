---
name: project_staging_workers_config_durability
description: "Durable uvicorn worker-count on the cluster lives in the DB runtime config (server_config runtime.workers), NOT config.json; set it via the web-UI form POST /admin/config/server over HTTPS (session cookie is Secure) — there is no JSON /api/admin/config PUT. Three materialization layers."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9f2bc45f-0085-4f49-90f3-7e65bdd67bcf
---

Setting the uvicorn worker count DURABLY on the CIDX cluster (v11.23.0+): the authoritative knob is the **DB runtime setting** `server_config` `runtime.workers`, NOT `config.json`. `config.json` is bootstrap-only and its `workers` key is STRIPPED on startup (reverts to absent). So editing config.json does not persist a worker count.

Three-layer materialization (target -> materialized -> applied):
1. DB `server_config` `runtime.workers` = the durable TARGET (survives everything).
2. Per-node `launch.json` = materialized target + `target_restart_generation`; regenerated from the DB `workers` by a ~30s config-reload poller AND at each startup.
3. Per-node `applied_launch.json` + systemd ExecStart `--workers N` = the APPLIED value.
A `workers` change does NOT bump the restart generation, so it does not auto-restart; it applies on the next restart. Deploy/auto-update restart reads `applied_launch.json`; an APPLY/admin restart reads `launch.json` (re-materialized from DB). If DB and applied drift (seen live: DB=4 while applied=2), a future generation-bumped restart will jump to the DB value — keep them aligned.

HOW to set it via the front door (the `.local-testing` s12 recipe assuming a JSON `PUT /api/admin/config` is STALE — that endpoint 404s):
- It is the **web-UI form** `POST /admin/config/server` (field `workers`), requiring: a **session cookie** (from `POST /login`), a CSRF token (`_csrf`), and **TOTP step-up elevation** via `POST /auth/elevate-ajax` (after `POST /admin/mfa/challenge/verify`).
- The `session` cookie is `Secure`, so this flow ONLY works over the **HTTPS** front door (the public DDNS URL), NOT over the plain-http LAN HAProxy endpoint (which is fine for MCP Basic-auth calls but cannot carry the Secure session cookie).
- Verify materialization by SSH read-back of `launch.json`/`applied_launch.json`/ExecStart on all nodes, and DB `server_config` runtime.workers.

Contrast: the MCP front door over plain-http LAN HAProxy works for MCP Basic-auth tool calls (list_global_repos, search_code, add_golden_repo, check_health, etc.) but NOT for the REST admin-config write (needs the Secure session over HTTPS). See [[project_cluster_auto_updater_service]], [[feedback_cluster_aware_state_only]], [[project_cluster_temporal_metadata_pg_backed]].
