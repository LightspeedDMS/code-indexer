# SIEM Delivery (Google Security Operations)

CIDX can deliver pilot security events to Google Security Operations
(Chronicle `events:import`) as validated UDM events. This guide covers what is
delivered, the guarantee, configuration, arming, operation and the admin
actions.

For Google SecOps staff (where to find each setting's value in Google, the
service account, searching and alerting), see
[siem-secops-guide.md](siem-secops-guide.md). For every delivered event and
its UDM fields, see [siem-secops-event-catalog.md](siem-secops-event-catalog.md).

## What is delivered

Pilot scope only (a code constant, not a setting):

- logins on every door (REST, MCP, Web), success and failure;
- MFA changes;
- group and permission changes;
- admin actions (user create/delete/password reset, credential and key
  creation, elevation, impersonation).

SIEM delivery also reports on itself. Every change to the `siem_delivery` config section, and every
SIEM admin action, is delivered with an explicit destination.

## Guarantee

Best-effort capture, using a mechanism that is durable when it succeeds,
followed by at-least-once delivery attempts for what was captured.

- The queue row is written in the SAME database transaction as the audit row
  (a per-row savepoint inside `insert_events`, both backends). If the queue
  insert fails, the action still proceeds and the audit row still commits. The
  gap is counted (`siem.capture_failures`), logged at ERROR (rate-limited) and
  shown as a DEGRADED health reason.
- Delivery is at-least-once: duplicates are possible and allowed. Group by
  `metadata.productLogId` (the audit event uuid) for uniqueness. A duplicate
  RESPONSE from SecOps never counts as delivery (`DUPLICATE_POLICY = HALT`).
- Nothing is captured while delivery is disabled or not yet armed. After a
  disable, a process can still capture for at most 90 s (its snapshot age
  bound). Those rows are counted (`capture_after_boundary`) and delivered.
  A row captured more than 90 s after a disable, clear, reset or destination
  change (and before capture legitimately resumes) is a defect, counted as
  `capture_after_boundary_late`. Each closing interval ends at the next
  enable of (or change back to) that destination, and it is recounted on
  every stats refresh until 90 s after it ends, so late-arriving rows are
  still caught.

## Configuration (Web UI Config: "SIEM Delivery (Google SecOps)")

| Field | Notes |
|-------|-------|
| `enabled` | Controls capture. Already captured rows keep being delivered. |
| `region` | One of the SecOps regions documented in Google's [Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide) guide: `us`, `eu`, `africa-south1`, `asia-northeast1`, `asia-south1`, `asia-southeast1`, `asia-southeast2`, `australia-southeast1`, `europe-west2`, `europe-west3`, `europe-west6`, `europe-west9`, `europe-west12`, `me-central1`, `me-central2`, `me-west1`, `northamerica-northeast2`, `southamerica-east1`. The endpoint is derived as `https://chronicle.<region>.rep.googleapis.com`; there is no free URL field. |
| `api_version` | `v1`, `v1beta` or `v1alpha`. |
| `project_id`, `location`, `instance_id` | Single path segments (`[A-Za-z0-9][A-Za-z0-9_-]*`). |
| `max_batch_events` | 1 to 1000. |
| `source_instance_label` | Carried as `additional.cidx_instance`. |
| `harness_endpoint` | Test receiver only: accepted ONLY in a process whose non-production fault-injection gate is active (loopback origin; the key's `token_uri` must then be `<harness_endpoint>/token`). |

Every SIEM loop cycle reads the COMMITTED configuration from the database,
not the process's cached config, so a save made through any worker or node
takes effect everywhere on the next cycle.

### Service-account credential (Web UI only)

The SecOps service-account JSON key is pasted or uploaded in the same Web UI
section ("Set / Replace Credential"); there is no key-file path and no other
way to configure it. Setting, replacing and removing it need TOTP elevation,
like every other admin secret change.

- Validation on save: the JSON parses; `type` is `service_account`;
  `client_email`, `private_key`, `private_key_id` and `token_uri` are present;
  the private key loads; `token_uri` is Google's token endpoint (behind the
  fault-injection gate, a loopback `<harness origin>/token` is also accepted).
  A rejected key changes nothing.
- Storage: one row of table `siem_delivery_credential` (SQLite `groups.db`
  solo, PostgreSQL cluster, migration `055`), so every node uses it. The key is
  AES-256 encrypted (`services/token_encryption.py`). The encryption key is:
  - cluster (PostgreSQL): derived from the SHARED JWT secret row in
    `cluster_secrets` (the same helper LLM lease state uses, with a SIEM-only
    salt), so every node derives the same key whatever its local files;
  - solo (SQLite): derived from the node's `.encryption_key_salt`, as for CI
    tokens and git credentials.

  The key is derived on first use (by the delivery loop or an admin action,
  off the startup path) and cached for the process.

  Only `client_email`, `private_key_id`, who set it and when, and a key-check
  value (HMAC of the encryption key) are stored in clear. When a process's
  key does not match the stored key-check (a rotated cluster JWT secret, or a
  changed solo salt) it probes `credential_key_mismatch`, logs one WARNING
  naming the cause, and the key must be re-uploaded; this is never reported
  as an invalid key.
- Only RSA service-account keys are accepted (google-auth signs RS256).
- The forms read at most 512 KiB of request body (larger: HTTP 413, refused
  before parsing), one file and two fields; the key JSON itself is capped at
  64 KiB.
- Write-only: no page, API or log returns the key. The status table and
  `GET /api/admin/siem-delivery/stats` (`credential`) show the identity only.
- Audit: `siem_credential_changed` with `change` (`set`, `replaced`,
  `removed`), `client_email` and `private_key_id` only. It is a SIEM
  self-report, delivered to the configured destination.
- Delivery reads the stored key on every token request, so a Replace or Remove
  made on any node applies at the next request everywhere. Without a usable
  credential a process probes `credential_missing` or `credential_invalid`
  and capture does not arm.
- Replacing or removing a stored key disarms delivery in the same
  transaction and clears the canary: re-arming needs a fresh canary and
  confirmation (see Arming). A canary sent while the key was being replaced
  is refused when recorded (HTTP 409; run it again).
- A `service_account_key_path` value saved by 12.79.0 is IGNORED: each process
  logs one WARNING, never reads that file, and the destination counts as
  "credential missing" until a key is uploaded. The next save of the section
  drops the field.

### Additional trusted CA (optional)

For a TLS-inspecting proxy, or a test emulator that presents Google's
hostnames with a test-CA certificate, CA certificates (PEM, one or more) can
be pasted or uploaded in the same section ("Set / Replace Trusted CA"). They
are public, so they are stored in the `siem_delivery` config section
(`trusted_ca_pem`, plus `trusted_ca_fingerprint`, the SHA-256 over the DER of
every certificate).

- They are ADDED to the default trust the clients use (httpx's default
  context: certifi, or `SSL_CERT_FILE`) for BOTH outbound legs, the OAuth
  token exchange and `events:import`. Certificate and hostname verification
  are always on; nothing can skip verification.
- The environment proxy is still honoured (`HTTPS_PROXY` / `HTTP_PROXY` /
  `ALL_PROXY`, bypassed per `NO_PROXY`), so a TLS-inspecting proxy works.
- The CA bundle is capped at 256 KiB (512 KiB request body, HTTP 413 above).
- Rolling upgrade: a 12.79.0 node that saves the SIEM section during the
  upgrade drops the CA fields (it does not know them). Set the CA again once
  every node runs this release.
- Test harness only: behind the fault-injection gate the harness endpoint
  may be `https://<loopback>:<port>`, so Phase 7 drives delivery through a
  loopback TLS front with a test CA.
- Validation on save: one or more X.509 certificates, certificates only, each
  a CA (basicConstraints CA=true) and not expired.
- Setting, replacing and removing need TOTP elevation and are recorded as a
  `config_changed` row whose values carry the fingerprint before and after.
- The UI shows each certificate's subject, issuer, SHA-256 fingerprint and
  expiry. The combined trust is built once per bundle and picked up through
  the committed-configuration read on the next cycle.
- A change (set, replace or remove) does not change the destination key,
  but it ends the configuration lifetime: delivery disarms and needs a fresh
  canary (see Arming). Set the CA before running the canary.

## Arming

Capture starts only after one atomic arming statement succeeds. That statement requires:

1. a synthetic canary (one event per UDM mapping entry plus one unmapped
   event), sent once and ACCEPTED;
2. every canary `productLogId` confirmed visible in SecOps search;
3. at least one live server process, every live process able to mint a token
   for the destination, and (cluster) every active node represented.

A destination change disarms. A new mapping version does not disarm, but it
shows a DEGRADED reason until the canary is re-run.

The order is: configure (key, optional CA, destination fields with
`enabled` off), run the canary, confirm it, then enable; capture arms on a
later cycle. The canary runs while delivery is disabled, as long as a
destination is configured.

A canary confirmation is valid only for the configuration lifetime that
produced it (Bug #2018). The lifetime is the committed section's
`arming_epoch`, a token no form can set: every configuration change carries
it over from the committed pre-image and renews it when the destination is
disabled, cleared or changed, or the trusted CA changes
(`siem_delivery/boundary.py` `carry_arming_epoch`). The canary records the
epoch it ran under (`siem_delivery_state.canary_config_epoch`, PostgreSQL
migration 056); the arming statement and the fence require it to equal the
committed epoch (`state_store.CANARY_CONFIRMED_FOR`), so a newer version of
another lifetime disarms and a stale confirmation is refused. Replacing or
removing the service-account key clears the canary and disarms in its own
transaction. A canary is recorded only if, under the state-row lock, the key
it was sent with is still the stored one, its lifetime is still the
committed one, and no run issued later was recorded; otherwise nothing
changes and the action answers 409. Runs are ordered by a durable,
strictly increasing ordinal taken under the state-row lock before the send
(`canary_issued_seq` issues it, `canary_run_seq` keeps the recorded run's;
PostgreSQL migration 057), never by clocks, so two runs started in the same
clock tick are still ordered. Enabling, and every other change
of the section, keeps a confirmed canary valid.

Fail closed at once: `state_store.capture_active` also requires the state's
canary epoch to equal the committed one, and the scheduler subscribes to the
config service's commits (`ConfigService.register_on_commit_callback`, at
`register_process`). A SIEM-section commit, or a credential change, in a
process re-applies the fence, the capture snapshot and the status view in
that process immediately (`scheduler.apply_committed_change`); the snapshot
publish is monotonic in the config version, so a slower cycle that read an
older version cannot re-arm it. Other processes and nodes follow at their
next cycle (`cycle_idle_seconds`, 30 s). Events they capture in that window
are bounded by that cycle and by the 90 s capture-snapshot age
(`CAPTURE_SNAPSHOT_MAX_AGE`). They are counted as captures after the
boundary (`capture_after_boundary`, see Capture above) only when the change
is a disable, a clear, a destination change or a reset (the boundary kinds
`stats.py` counts); after a trusted-CA change or a credential replacement or
removal they are NOT counted.

A configuration saved before the epoch existed has the empty epoch, so an
already armed destination stays armed across the upgrade until its next
lifetime-ending change. During a mixed-version cluster upgrade, re-run the
canary once all nodes are upgraded: older nodes do not take a canary run
number, so run ordering holds only when every node runs this release.

### Arming from the Web UI

Config page, "SIEM Delivery (Google SecOps)" section, "Operations":

1. The arming checklist explains the state, row by row: enabled and the
   committed destination (with its config version), the stored credential,
   the canary for this destination and mapping, confirmed ids of expected,
   processes ready (aggregated over every live process, failing ones listed),
   nodes without a process (cluster), and ARMED. ARMED is authoritative; the
   other rows explain it. The checklist reads the COMMITTED configuration and
   the database, so a save made through any node shows immediately; the
   "this process" line below it is a per-process diagnostic only.
2. "Run canary" sends the synthetic events once and lists each
   `product_log_id` with its action and event type, plus the run-wide SecOps
   search: `metadata.vendor_name = "CIDX" AND
   additional.fields["correlation_id"] = "canary-<run_id>"`.
3. The analyst finds the ids in SecOps; tick them, or paste them (separated
   by spaces, new lines or commas), and press "Confirm visible".
4. Watch the ARMED row (the panel refreshes after every action; "Refresh"
   reloads it).

REST alternative: `POST /api/admin/siem-delivery/canary`, then
`POST /api/admin/siem-delivery/canary/confirm-visible` with
`{"canary_run_id": ..., "visible_product_log_ids": [...]}`.

### Who can do what

- Reading the panels (and REST `GET /stats`, `GET /quarantine`) needs an
  admin; no elevation.
- Every action (canary, confirm visible, resume, requeue, acknowledge,
  re-batch, retarget, abandon) needs an admin with TOTP elevation while
  `elevation_enforcement_enabled` is ON, on both the Web and REST doors
  (the Web opens the TOTP modal and replays the action). With enforcement
  OFF, actions pass through exactly like every other elevated route.
- Abandon through the Web requires typing `ABANDON` exactly; REST abandon
  takes no body.
- Each action writes the same audit row on both doors; a refused action
  writes none.

## Operation and visibility

- `GET /api/admin/siem-delivery/stats`: fleet counts (`pending` = undelivered
  rows, capped at 10,000+), quarantine, durable counters, capture state, halt,
  and this process's liveness.
- `GET /api/admin/siem-delivery/quarantine?limit=N`: quarantined rows (event
  uuid, reason, sanitised signature).
- `GET /api/system/health` (authenticated): SIEM reasons appear in
  `failure_reasons` and are DEGRADED only. A SecOps outage never makes a
  node unhealthy. The public `/healthz` returns the resulting status only
  (DEGRADED answers HTTP 200); `GET /health` does not include SIEM reasons.
- OTEL: `cidx.siem.*` gauges (pending, quarantined, oldest pending age,
  backlog estimate, halted, capture active, unrecoverable, capture after
  boundary) and counters (delivered, capture failures, unmapped types, ...).
- Operator alert signals: an ERROR log when a threshold is first crossed (backlog older
  than 60 min, halted for more than 15 min, backlog over 5 GB, disk headroom
  under 12 h, any capture failure), repeated at most hourly, then one INFO line
  on recovery. There is no built-in paging.
- Delivery runs as short `siem_delivery_tick` background jobs; like x-ray
  searches, they are hidden from the dashboard's recent-jobs panel.
- A 429 from SecOps ends the current tick for the destination; the batch is
  retried no sooner than its `Retry-After` (seconds or an HTTP date, capped at
  one hour).

## Halts and probes

Request-wide failures halt delivery without quarantining anything. These are
request-level 400s, 401/403, 404/413/415/501, a duplicate response, and
unclassified responses. A probe retries each halt class on its own schedule
and clears the halt by itself once the cause is fixed. The one exception is
the duplicate-response halt, which needs an admin decision.

Recovery from the Web UI (Config page, SIEM section, "Operations", "Halts and
recovery"): the halt (class, signature, since, next probe) with Resume; the
halted batch, always shown even when it is beyond the first page, with
Acknowledge and Re-batch; open batches, quarantined rows (select and Requeue)
and stranded destinations, each paged with "More"; and "Open destination by
key" for any key. Retarget and Abandon open a dialog with the destination's
region, project and instance and its pending, batched and quarantined counts,
labelled "currently queued; may grow until the action runs" (shown as
10,000+ beyond the cap). Abandon is irreversible and requires typing
`ABANDON`; the result shows the exact number of events abandoned.

REST alternative:

- `POST /api/admin/siem-delivery/batches/{batch_id}/acknowledge`: the events
  are confirmed present in SecOps; mark them delivered.
- `POST /api/admin/siem-delivery/batches/{batch_id}/rebatch`: re-send in new
  batches (accepting possible duplicates).
- `POST /api/admin/siem-delivery/resume`: clear any halt now.

Row-specific rejections quarantine only the rejected events (bisecting when
no index is reported). The quarantine count is capped fleet-wide at 5 per
hour: the next failure halts instead. A local-validation halt clears itself
only when the failures still present could be quarantined within the current
window, which is never reset by a probe. Quarantined rows are requeued
automatically once per mapping-version change (even when nothing else is
pending), and on demand through
`POST /api/admin/siem-delivery/quarantine/requeue`.

Rows captured for a destination that is no longer configured wait until an
admin calls `POST /api/admin/siem-delivery/destinations/{key}/retarget` or
`/abandon`. Both refuse the currently configured destination (409). All admin
actions are audited.

- `abandon` works for any destination key that is not the configured one,
  including when NOTHING is configured (decommissioning). It resolves every
  undelivered row of that key: pending, batched AND quarantined. Abandon is
  the path for a quarantined row on a removed destination; the quarantine
  requeue action would only return it to pending on the dead destination.
- Both snapshot the key's highest queue id when they start and only ever
  touch rows at or below it (rows captured afterwards, e.g. after the key is
  configured again, are never moved). In every transaction, under the SIEM
  state-row lock, they re-read the COMMITTED destination from the database
  (never a process's cached view) and stop with 409 once the abandoned key
  is configured (retarget: once its target is no longer the configured
  destination). Configuration saves take no SIEM lock, so a save that
  commits inside one round's (millisecond) window is not serialised with
  that round: a known, documented limitation.
- Both refuse with 409 "a send to this destination is in flight; retry
  shortly" while any open batch of the key holds an unexpired sender lease
  (claims take their lease under the same state-row lock, so the check is
  race-free). A lease left by a crashed node blocks them only until it
  expires (`lease_seconds`: 10 minutes; 30 s in the test harness profile).
- `retarget` needs a configured destination to move the rows to; with none it
  answers 409 "no SIEM destination is configured to retarget to; configure the
  new destination first, or abandon the rows instead".

Decommissioning: disable delivery and clear the destination fields, then
abandon the old key (the clearing save itself captures one row for the removed
destination). Once those rows are abandoned, every SIEM health reason clears.
Configuring the same destination again later starts a new configuration
lifetime: it arms only after a fresh canary and confirmation.

## Retention

`DataRetentionScheduler` prunes only terminal SIEM rows. These are delivered
rows after 24 h, abandoned or unrecoverable rows after the audit retention,
and terminal batches after 24 h. It runs in paced transactions of at most 500
rows, and pending, batched and quarantined rows are never deleted.

## Test harness timing profile

A process whose non-production fault-injection gate is active uses a
compressed timing profile (1 s loop, 10 s halt probe, 30 s lease, 10 s
DEGRADED backlog age) so end-to-end tests against the mock receiver finish
quickly. Production processes cannot select it, and there is no setting. The
90 s capture bound, the quarantine cap and the operator alert thresholds are never
compressed.

## Real-tenant enablement checklist

Before enabling against a real tenant:

1. Turn TOTP elevation enforcement ON (`elevation_enforcement_enabled`) and
   confirm that a SIEM action asks for elevation.
2. Grant a dedicated identity only the events-import permission.
3. Upload the service-account key (and, only if needed, the trusted CA) in
   the Web UI, then configure the region, project, location and instance
   with `enabled` off.
4. Run the canary.
5. Search each canary `productLogId` in SecOps and submit the confirmation.
6. Enable delivery, and wait for the ARMED row.
7. Record the real 400 body shape and any duplicate response.
8. Confirm that the IAM permission name is correct.
9. Verify that `principal.ip` is the client address.
10. Re-verify the region list against Google's documentation.

The same steps from a shell are in the
[operator curl runbook](siem-secops-curl-runbook.md). The Web UI `session`
cookie is `Secure` whenever the server is not bound to localhost, so a
deployment served over plain `http` needs a TLS front door for browser and
curl web-form use (the key and CA uploads exist only as Web forms; public
issue #2004).
