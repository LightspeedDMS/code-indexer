# SIEM Delivery (Google Security Operations)

CIDX can deliver pilot security events to Google Security Operations
(Chronicle `events:import`) as validated UDM events. This guide covers what is
delivered, the guarantee, configuration, arming, operation and the admin
actions.

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
  expiry. A change does not change the destination key (no disarm); the
  combined trust is built once per bundle and picked up through the
  committed-configuration read on the next cycle.

## Arming

Capture starts only after one atomic arming statement succeeds. That statement requires:

1. a synthetic canary (one event per UDM mapping entry plus one unmapped
   event), sent once and ACCEPTED: `POST /api/admin/siem-delivery/canary`;
2. every canary `productLogId` confirmed visible in SecOps search:
   `POST /api/admin/siem-delivery/canary/confirm-visible` with
   `{"canary_run_id": ..., "visible_product_log_ids": [...]}`;
3. at least one live server process, every live process able to mint a token
   for the destination, and (cluster) every active node represented.

A destination change disarms. A new mapping version does not disarm, but it
shows a DEGRADED reason until the canary is re-run.

## Operation and visibility

- `GET /api/admin/siem-delivery/stats`: fleet counts (`pending` = undelivered
  rows, capped at 10,000+), quarantine, durable counters, capture state, halt,
  and this process's liveness.
- `GET /api/admin/siem-delivery/quarantine?limit=N`: quarantined rows (event
  uuid, reason, sanitised signature).
- `/health`: SIEM reasons are DEGRADED only. A SecOps outage never makes a
  node unhealthy.
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
the duplicate-response halt, which needs an admin decision:

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

1. Grant a dedicated identity only the events-import permission.
2. Configure the region, project, location and instance, and upload the
   service-account key in the Web UI.
3. Run the canary.
4. Search each canary `productLogId` in SecOps and submit the confirmation.
5. Record the real 400 body shape and any duplicate response.
6. Confirm that the IAM permission name is correct.
7. Verify that `principal.ip` is the client address.
8. Re-verify the region list against Google's documentation.
