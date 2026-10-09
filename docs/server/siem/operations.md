# SIEM Delivery: CIDX Operations

This guide is for the CIDX server administrator. It covers the CIDX side of
delivering audit events to Google Security Operations (SecOps, called the
Chronicle API by Google): configuration, the service-account credential,
arming, monitoring, halts and recovery, and decommissioning.

The other SIEM documents:

- [Google SecOps guide](secops-guide.md): the Google tenant side (service
  account, where to find the destination values, searching and alerting).
- [curl runbook](curl-runbook.md): every step of this guide as shell
  commands.
- [Event catalog](event-catalog.md): every delivered event and its UDM fields.

## What is delivered

CIDX sends selected audit events to the Chronicle API method `events:import`,
already converted to UDM. The scope is a code constant, not a setting
(`PILOT_ACTION_TYPES` in `services/siem_delivery/scope.py`):

- logins on every door (REST, MCP, Web), success and failure;
- MFA changes;
- group and permission changes;
- admin actions: user create, delete and password reset, MCP credential and
  API key creation, SSH key host assignment, elevation, impersonation.

SIEM delivery also reports on itself: every change of the SIEM settings
section, every change of the service-account credential and every SIEM admin
action is delivered to the configured destination. These self-reports are
captured whenever a destination is configured, even before delivery is armed.

The event catalog lists every action type and its fields.

## Delivery guarantee

- **Capture is best effort.** When capture of an event fails, the user's
  action still succeeds and the audit row is still written. The gap is
  counted, logged at ERROR and shown as a health reason
  (`SIEM capture failures since boot: <n>`).
- **Delivery is at least once.** An event can arrive in SecOps more than once.
  Each event's `metadata.product_log_id` is the CIDX audit event id; use it to
  remove duplicates.
- **A duplicate response is never counted as delivered.** If SecOps answers
  HTTP 409, delivery halts until an admin acknowledges, re-batches or resumes
  (see [Halts and recovery](#halts-and-recovery)).
- **Order is not guaranteed.** Batches are built in capture order, but
  several batches per destination can be open at a time (normally up to
  three; splitting a rejected batch adds more) and failed batches are retried
  later. `metadata.event_timestamp` is when the action happened.
- **Nothing new is captured while delivery is disabled or not armed.** After
  a disable, a server process can still capture for at most 90 seconds. Those
  events are counted and delivered. An event captured later than that is a
  defect, counted and shown as a health reason.
- **Throttling.** An HTTP 429 from SecOps ends the current delivery cycle for
  the destination. The batch is retried no sooner than its `Retry-After`
  (seconds or an HTTP date), capped at one hour. Events stay queued.
- **Transient failures** (HTTP 500, 502, 503, 504, connection failures and
  timeouts) are retried with exponential backoff, capped at one hour.
- One request carries at most `max_batch_events` events and at most
  2,000,000 bytes. The exception is a single event larger than that (up to the
  4,000,000-byte per-event ceiling), which is sent alone.

## Configuration

SIEM delivery settings are runtime settings: they live in the server
database, not in `config.json`, and are changed in the Web UI, menu
**Config**, section **SIEM Delivery (Google SecOps)** (**Edit**, then
**Save**).

Every save of the section needs TOTP elevation when elevation enforcement is
on, and is audited as `config_changed` (delivered as a self-report). The audit
row records the values of `enabled`, `api_version`, `max_batch_events`,
`region` and the trusted-CA fingerprint; other fields are recorded by name
only. Leading and trailing spaces are trimmed from every text field.

| Field | Allowed values | Default | Notes |
|-------|----------------|---------|-------|
| `enabled` | Yes / No | No | The capture switch. Already captured events keep being delivered whatever it says, as long as a destination is configured. |
| `region` | One of the regions listed below. Required when enabled. | empty | The endpoint is derived as `https://chronicle.<region>.rep.googleapis.com`; there is no URL field. |
| `api_version` | `v1`, `v1beta`, `v1alpha` | `v1` | Not part of the destination identity. |
| `project_id` | 1 to 128 characters: letters, digits, `_`, `-`, starting with a letter or digit. Required when enabled. | empty | The Google Cloud project linked to the SecOps instance. |
| `location` | Same rule as `project_id`. Required when enabled. | empty | For SecOps, the same value as `region`. |
| `instance_id` | Same rule as `project_id`. Required when enabled. | empty | The SecOps customer ID. |
| `max_batch_events` | 1 to 1000 | 1000 | Most events per request. |
| `source_instance_label` | 1 to 64 characters: letters, digits, `.`, `_`, `-`. Required when enabled. | empty | Sent in every event as `additional.cidx_instance`, to tell CIDX servers apart. |
| `harness_endpoint` | Leave empty. | empty | Test receiver for CIDX's own automated tests. Accepted only in a non-production process whose fault-injection test gate is active; refused everywhere else. |

Regions accepted (`SECOPS_REGIONS` in `services/siem_delivery/destination.py`,
the regional endpoints of Google's
[Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide)
guide): `us`, `eu`, `africa-south1`, `asia-northeast1`, `asia-south1`,
`asia-southeast1`, `asia-southeast2`, `australia-southeast1`, `europe-west2`,
`europe-west3`, `europe-west6`, `europe-west9`, `europe-west12`,
`me-central1`, `me-central2`, `me-west1`, `northamerica-northeast2`,
`southamerica-east1`.

How the request URL is built from these fields, and why `region` and
`location` hold the same value, is in the
[Google SecOps guide](secops-guide.md#22-region-location-and-the-endpoint).

A validation error names the field, never its value, for example
`SIEM Delivery: invalid region (not a documented SecOps region)`.

The **destination** is the combination of `region`, `project_id`, `location`
and `instance_id`. Changing any of them makes a new destination, which
disarms delivery. Changing `api_version`, `max_batch_events` or
`source_instance_label` does not.

The [Google SecOps guide](secops-guide.md#2-values-for-the-cidx-administrator)
explains where to find each value in Google.

### Service-account credential

The SecOps service-account JSON key is set in the same Web UI section, under
**Service Account Credential**: paste the JSON, or upload the key file (one,
not both), and press **Set / Replace Credential**. **Remove Credential**
deletes it. There is no key-file path setting and no REST endpoint for it.
Setting, replacing and removing need TOTP elevation when enforcement is on.

The key is refused, and nothing changes, unless:

- it is valid JSON, a JSON object, and at most 64 KiB;
- `type` is `service_account`;
- `client_email`, `private_key`, `private_key_id` and `token_uri` are present
  and not empty; `client_email` looks like an address and `private_key_id`
  is letters and digits only;
- `token_uri` is exactly `https://oauth2.googleapis.com/token`;
- the private key loads and is an RSA key.

The form accepts at most 512 KiB of request body (HTTP 413 above that,
refused before parsing), one file and two fields.

How the key is handled:

- It is stored encrypted in the server database, one row shared by every
  node of a cluster. It is write-only: no page, API or log returns it.
- The status table, and `credential` in `GET /api/admin/siem-delivery/stats`,
  show only its identity: `client_email`, `private_key_id`, who set it and
  when.
- Every change is audited as `siem_credential_changed` with `change`
  (`set`, `replaced`, `removed`), `client_email` and `private_key_id`, and
  delivered as a self-report.
- Every token request reads the stored key, so a replacement made on any
  node applies everywhere at the next token request.
- Replacing or removing the key disarms delivery at once and clears the
  canary: re-arming needs a fresh canary and confirmation (see
  [Arming](#arming)). A canary that was being sent while the key changed is
  refused when it completes (HTTP 409; run it again).
- If a process cannot decrypt the stored key because the server's encryption
  key changed, its token probe reports `credential_key_mismatch` and it logs
  one WARNING. Upload the key again.
- The section has no key-file setting. A `service_account_key_path` value
  found in the stored section is ignored: each process logs one WARNING and
  never reads the file, and the destination has no credential until a key is
  uploaded.

Rotation, step by step, is in the
[Google SecOps guide](secops-guide.md#34-rotate-the-key).

### Additional trusted CA (optional)

Only for a TLS-inspecting proxy between CIDX and Google, or a test emulator.
Under **Additional Trusted CA (optional)**, paste or upload one or more PEM
CA certificates and press **Set / Replace Trusted CA**; **Remove Trusted CA**
deletes them.

- Each certificate must be a CA (basicConstraints CA=true) and not expired;
  the bundle may contain certificates only and is at most 256 KiB.
- The certificates are ADDED to the default trust (certifi, or
  `SSL_CERT_FILE`) for both outbound calls: the OAuth token exchange and
  `events:import`. Certificate and hostname verification always stay on.
- The proxy environment variables `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`
  and `NO_PROXY` are honoured.
- The page shows each certificate's subject, issuer, SHA-256 fingerprint and
  expiry, and the bundle's SHA-256.
- A change needs TOTP elevation when enforcement is on, and is audited as
  `config_changed` with the bundle fingerprint before and after.
- A change ends the configuration lifetime (see [Arming](#arming)): delivery
  disarms and needs a fresh canary. Set the CA before running the canary.

## Arming

Capture of pilot events starts only when delivery is **armed**. Arming
happens on a delivery loop cycle once all of these hold:

1. delivery is enabled with a complete destination;
2. a synthetic **canary** was sent to this destination, under the current
   UDM mapping version and configuration lifetime, and SecOps ACCEPTED it;
3. an admin confirmed that every canary event is visible in SecOps;
4. at least one server process is live, every live process has recently
   minted a token for the destination (`probe_result` `ok`), and in a cluster
   every active node has a live process.

The canary holds one event per UDM mapping entry plus one deliberately
unmapped event: 34 events with the current mapping (version 3). Its events
are sent directly; they are never queued.

### Order of work

1. Upload the key, and only if needed the trusted CA.
2. Fill the destination fields with `enabled` at No, and save. The canary
   can run while delivery is disabled, as long as a destination is
   configured.
3. Run the canary.
4. A SecOps analyst finds the canary events (see the
   [Google SecOps guide](secops-guide.md#4-verify-the-canary)); confirm them.
5. Set `enabled` to Yes and save.
6. Watch the ARMED row: delivery arms on a later loop cycle.

### Configuration lifetime

A canary confirmation is valid only for the configuration lifetime that
produced it. These changes end the lifetime: delivery disarms at once in the
process that made the change, other processes and nodes follow at their next
loop cycle (within about 30 seconds), and a fresh canary and confirmation are
needed:

- disabling delivery;
- clearing the destination, or changing it to other coordinates;
- changing the trusted CA;
- replacing or removing the service-account key.

Enabling delivery, and every other change of the section (for example
`max_batch_events` or the instance label), keeps a confirmed canary valid. A
canary or a confirmation from an earlier lifetime is refused with HTTP 409
("canary run is stale").

A CIDX upgrade that brings a new UDM mapping version does not disarm, but the
health reason `SIEM delivery canary not run for mapping version <n>` stays
until the canary is run again. Quarantined events are requeued once
automatically after such an upgrade.

### Arming from the Web UI

Config page, section **SIEM Delivery (Google SecOps)**, **Operations**:

1. The arming checklist explains the state row by row: enabled and the
   committed destination, the stored credential, the canary for this
   destination and mapping, the confirmed ids out of the expected ones,
   processes ready (aggregated over every live process, failing ones listed),
   nodes without a process (cluster), and **ARMED**. ARMED is authoritative;
   the other rows explain it. The checklist reads the committed configuration
   and the database, so a save made through any node shows at once. The
   "This process" line under it is a diagnostic for the serving process only.
2. **Run canary** sends the canary once and lists each `product_log_id` with
   its action and event type, plus the search for the whole run:
   `metadata.vendor_name = "CIDX" AND additional.fields["correlation_id"] = "canary-<run_id>"`.
   If SecOps rejected it, the checklist shows the rejection signature.
3. Tick the ids the analyst found, or paste them (separated by spaces, new
   lines or commas), and press **Confirm visible**. Arming needs every
   expected id, including the unmapped one; missing ones are named by action
   type, and the confirmation can be repeated.
4. Watch the ARMED row; the panel refreshes after every action, and
   **Refresh** reloads it.

The status table's **Capture state** reads, in order of progress:
`delivery disabled or no destination`, `awaiting canary`, `canary rejected`,
`canary accepted, N of M visible`, `awaiting process readiness`, `armed`.

The REST equivalent is in the [curl runbook](curl-runbook.md).

## Who can do what

- Reading the panels, `GET /api/admin/siem-delivery/stats` and
  `GET /api/admin/siem-delivery/quarantine` needs an admin; no elevation.
- Every action (canary, confirm visible, resume, requeue, acknowledge,
  re-batch, retarget, abandon), every save of the section and every credential
  or CA change needs an admin with TOTP elevation while
  `elevation_enforcement_enabled` is on (default off). The Web UI opens the
  TOTP prompt and replays the action. With enforcement off, these actions run
  without an elevation check. See
  [Login and elevation](../auth/login-and-elevation.md).
- Abandon through the Web UI requires typing `ABANDON` exactly; the REST
  abandon takes no body.
- Each action writes the same audit row on both doors; a refused action
  writes none.

Before connecting a real tenant, turn elevation enforcement on.

## Monitoring

### Health

SIEM problems only ever make a node DEGRADED, never unhealthy, so a SecOps
outage cannot take CIDX out of a load balancer. The reasons appear in
`failure_reasons` of the authenticated `GET /api/system/health`. The public
`GET /healthz` returns only the resulting status (DEGRADED still answers HTTP
200). `GET /health` does not include SIEM reasons. See
[Observability](../observability.md) for the health endpoints.

The reasons (`services/siem_delivery/health.py`):

| Reason | Meaning |
|--------|---------|
| `SIEM delivery not running in this process: <error>` | The delivery service failed to start in this process. |
| `SIEM delivery halted: <class>` | Delivery is halted (see [Halts and recovery](#halts-and-recovery)). |
| `SIEM delivery loop stalled in this process` | The delivery loop has not completed a cycle for several cycle intervals. |
| `SIEM delivery config never loaded in this process; capture INACTIVE here` | This process could never read the SIEM settings; it captures nothing. |
| `SIEM delivery config unreadable in this process; using last-known-good (the 90 s stale-capture bound is suspended here)` | The last read of the settings failed; the process keeps the previous ones. |
| `SIEM delivery backlog: oldest pending event is <n> s old` | The oldest undelivered event is older than 15 minutes. |
| `SIEM delivery: <n> events quarantined` | Events set aside after a rejection. |
| `SIEM delivery: <n> events unrecoverable (...)` | Events that could not be converted and whose audit row has aged out. |
| `SIEM delivery canary not run for mapping version <n>` | Armed, but the canary predates the current UDM mapping version. |
| `SIEM delivery backlog large or disk headroom low` | Backlog estimate over 1 GiB, or disk projected to fill within 24 hours. |
| `SIEM delivery: process <id> cannot mint SecOps tokens: <reason>` | A process's token probe failed: `credential_missing`, `credential_invalid`, `credential_key_mismatch`, `token_uri_not_allowed`, `token_rejected` or `token_endpoint_unreachable`. |
| `SIEM delivery: <n> events pending for unconfigured destination <key>` | Events wait for a destination that is no longer configured (retarget or abandon them). |
| `SIEM capture failures since boot: <n>` | Capture of some events failed in this process. |
| `SIEM delivery: <n> pilot events captured more than 90 s after a SIEM disable/change` | Late captures after a disable or destination change (a defect; report it). |

While delivery is disabled and nothing is pending or halted, no SIEM reason
is reported.

### Statistics and logs

- `GET /api/admin/siem-delivery/stats` returns fleet counts (`pending` =
  undelivered events, reported as 10,000+ above the cap; quarantined;
  delivered total; unconfigured destinations; backlog estimate), the
  credential identity, the capture state and canary status, the live
  processes and their `probe_result`, any halt (`class`, `signature`,
  `since`, `next_probe_at`, `batch_id`) and this process's liveness
  (`local_process`). The counts, the halt and the process list are read live
  from the database. The backlog estimates, and the counts the health reasons
  use, come from a snapshot refreshed about once a minute. The `capture` state
  and `configured_destination_key` come from the serving process's last loop
  cycle, so they can lag a save made on another node by up to one idle cycle
  (about 30 seconds).
- `GET /api/admin/siem-delivery/quarantine?limit=N` (1 to 1000, default 100)
  lists quarantined events: event id, action type, destination key, reason,
  sanitised signature and capture time.
- OTEL instruments under `cidx.siem.*`: gauges `pending`, `quarantined`,
  `oldest_pending_age_seconds`, `backlog_bytes_estimate`,
  `projected_hours_to_disk_full`, `halted`, `capture_active`, `unrecoverable`,
  `capture_after_boundary`, `capture_after_boundary_late`; counters
  `delivered`, `capture_failures`, `capture_skipped_snapshot_expired`,
  `reprojected`, `unmapped_action_type`, `unexpected_success_body`.
- Alert log lines: an ERROR `SIEM delivery alert: ...` when the oldest pending
  event is older than 60 minutes, a halt lasts more than 15 minutes, the
  backlog estimate passes 5 GiB, the disk is projected to fill within 12
  hours, or any capture failure happened; repeated at most hourly while it
  holds, then one INFO line on recovery. There is no built-in paging.
- Delivery runs as short `siem_delivery_tick` background jobs. They are
  hidden from the dashboard's recent-jobs panel.

## Halts and recovery

A request-wide failure halts delivery for the destination without
quarantining or dropping anything:

| Halt class | Cause | Clears |
|------------|-------|--------|
| `credential` | SecOps answered 401 or 403. | By itself once a probe succeeds, or by Resume. |
| `request_rejection` | HTTP 400 naming a field outside the events, or 404, 413, 415, 501. | By itself once a probe succeeds, or by Resume. |
| `unclassified` | Any other unexpected response. | By itself once a probe succeeds, or by Resume. |
| `row_rejection_burst` | SecOps rejected individual events more often than the quarantine cap allows. | By itself once a probe batch is accepted, or by Resume. |
| `local_validation_burst` | CIDX's own pre-send check failed for more events than the quarantine cap allows. | By itself once the failure that caused it is gone and the remaining failures fit the quarantine cap, or by Resume. |
| `duplicate_response` | SecOps answered 409. | Only by an admin: Acknowledge, Re-batch or Resume. |

While halted, a probe retries every 15 minutes (at once after an upgrade that
brings a new mapping version) and clears the halt by itself once the cause is
fixed, except for the duplicate-response halt.

**Quarantine.** When SecOps rejects specific events, CIDX finds them from the
field-violation paths of the error, or by splitting the batch in halves and
resending, sets them aside and keeps sending the rest. Events that fail CIDX's
own pre-send check are quarantined too. At most 5 events per hour are
quarantined fleet-wide; the next failure halts delivery instead.

**Duplicate response.** Check SecOps for the halted batch's events. Present:
**Acknowledge** (mark them delivered). Absent: **Re-batch** (send them again
in new batches, accepting possible duplicates). **Resume** also clears this
halt, but leaves the halted batch pending: it is sent again unchanged, so
duplicates are possible.

**Recovery from the Web UI** (Config page, SIEM section, **Operations**,
**Halts and recovery**): the halt (class, signature, since, next probe) with
**Resume**; the halted batch, always shown, with **Acknowledge** and
**Re-batch**; open batches, quarantined events (select and **Requeue
selected**) and stranded destinations, each paged with **More**; and
**Open destination by key** for any key. **Retarget / Abandon** opens a dialog
with the destination's region, project and instance and its pending, batched
and quarantined counts (shown as 10,000+ above the cap). Abandon is
irreversible and requires typing `ABANDON`; the result shows how many events
were abandoned.

**Recovery over REST** (all `POST`, admin with elevation):

| Endpoint | Effect |
|----------|--------|
| `/api/admin/siem-delivery/resume` | Clear any halt now (`{"resumed": false}` when nothing is halted). Needs a configured destination. |
| `/api/admin/siem-delivery/batches/{batch_id}/acknowledge` | Duplicate-response halt only: mark the halted batch delivered. |
| `/api/admin/siem-delivery/batches/{batch_id}/rebatch` | Duplicate-response halt only: send the halted batch's events again. |
| `/api/admin/siem-delivery/quarantine/requeue` | Body `{"event_uuids": [...]}`, at most 500 per call: return quarantined events to the queue. |
| `/api/admin/siem-delivery/destinations/{key}/retarget` | Move the undelivered events of a no-longer-configured destination to the configured one. |
| `/api/admin/siem-delivery/destinations/{key}/abandon` | Drop the undelivered events (pending, batched and quarantined) of a destination that is not the configured one. |

Retarget and abandon:

- Both refuse the configured destination with 409 ("rows already target the
  configured destination").
- Retarget needs a configured destination to move to; with none it answers
  409 and suggests abandoning instead. Abandon works even when nothing is
  configured.
- Both act only on events captured before they started. Retarget stops with
  409 if the configured destination changes while it runs; abandon stops with
  409 only if the key being abandoned becomes the configured destination.
- Both answer 409 "a send to this destination is in flight; retry shortly"
  while a send to that destination is in progress. A send left by a crashed
  node blocks them until its lease expires (10 minutes).
- Abandon is the way to dispose of quarantined events of a removed
  destination: requeue would only return them to the dead destination.

All recovery actions are audited and delivered as self-reports.

## Decommissioning

1. Resolve any halt first (Resume, or Acknowledge / Re-batch for a duplicate
   response). Resume needs a configured destination, and no probe runs
   without one, so a halt left open after step 2 stays reported.
2. Disable delivery and clear the destination fields, and save. The clearing
   save itself captures one self-report for the removed destination.
3. Remove the trusted CA (if any) and the service-account key.
4. Abandon the old destination key (shown in the stats document under
   `fleet.unconfigured_destinations`, and in the recovery panel).
5. Once those events are abandoned, every SIEM health reason clears.

Configuring the same destination again later starts a new configuration
lifetime: it arms only after a fresh canary and confirmation.

## Retention

The data-retention job prunes only finished SIEM rows: delivered events after
24 hours, abandoned and unrecoverable events after the audit-log retention
period, and finished batches after 24 hours. It deletes at most 500 rows per
transaction. Pending, batched and quarantined events are never deleted.

## Cluster behaviour

- The settings, the credential, the queue and the delivery state are in the
  shared database, so every node uses the same ones.
- Every delivery loop cycle reads the committed settings from the database,
  not the process's cached configuration, so a save made through any node
  takes effect everywhere within one cycle (30 seconds when idle). The
  process that made a change applies it at once.
- Arming requires every active node to have a live delivery process that can
  mint a token.
- The Config page's settings readout shows the configuration as the serving
  process last loaded it, so another node's page can lag a save by up to
  about 30 seconds. The same applies to the `capture` block of the stats
  document (see [Statistics and logs](#statistics-and-logs)). Only the arming
  checklist reads the committed configuration and does not lag.

## Real-tenant enablement checklist

Before enabling against a real tenant:

1. Turn TOTP elevation enforcement on (`elevation_enforcement_enabled`) and
   confirm that a SIEM action asks for elevation.
2. Use a dedicated service account that holds only the events-import
   permission (see the [Google SecOps guide](secops-guide.md#3-create-the-google-service-account)).
3. Upload the key (and, only if needed, the trusted CA), then configure the
   destination with `enabled` at No.
4. Run the canary.
5. Have each canary `product_log_id` found in SecOps, and confirm them.
6. Enable delivery and wait for the ARMED row.
7. Record the real shape of a SecOps 400 rejection and of any duplicate
   response.
8. Confirm the IAM permission name with your Google administrator.
9. Check what `principal.ip` holds behind your reverse proxy (see
   [Known limitations](secops-guide.md#7-known-limitations)).
10. Re-check the region list against Google's documentation.

The Web UI `session` cookie carries the `Secure` attribute whenever the
server's `host` setting is not a loopback address, so a deployment served
over plain `http` needs a TLS front door for browser use. The key and CA
uploads exist only as Web UI forms.

## How it works

A short map for readers of the code (`src/code_indexer/server/services/siem_delivery/`):

- **Capture.** `capture.py` is a hook inside the audit writers
  (`services/audit_log_service.py` for SQLite and
  `storage/postgres/audit_log_backend.py` for PostgreSQL). Events are
  selected and projected (`projection.py`) before the audit transaction; the
  queue row is then written inside the audit transaction under its own
  savepoint, so a failed queue insert never fails the audit write. Only typed,
  allowlisted values are projected.
- **Capture switch.** Each process decides from a snapshot that its delivery
  loop republishes every cycle; a snapshot older than 90 seconds captures
  nothing. Self-reports carry their destination explicitly
  (`boundary.py` computes it from the committed before and after
  configuration).
- **Arming.** `state_store.py` arms with one conditional database statement.
  Each configuration lifetime has an `arming_epoch` that no form can set; it is
  renewed by the changes listed under [Configuration lifetime](#configuration-lifetime),
  and when a canary is recorded its epoch, the stored credential's id and its
  run ordinal are checked, so a stale canary is refused.
- **Delivery.** `scheduler.py` runs the loop and submits `siem_delivery_tick`
  jobs; `claim.py`, `sender.py`, `classifier.py` and `completion.py` build,
  send and settle batches under leases. `udm.py` holds the UDM mapping
  (`UDM_MAPPING`, `MAPPING_VERSION`).
- **Timings** are code constants (`timings.py`), not settings. A process whose
  non-production fault-injection gate is active uses a compressed test
  profile (1 s loop, 10 s halt probe, 30 s lease, 10 s backlog threshold) so
  the end-to-end tests finish quickly; production processes cannot select
  it. The 90 s capture bound, the quarantine cap and the alert thresholds are
  never compressed.
