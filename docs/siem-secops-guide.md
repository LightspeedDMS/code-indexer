# Google SecOps Guide for cidx Audit Events

This guide is for security operations staff who run Google Security
Operations (SecOps). Google's API for SecOps is still called the Chronicle
API. The guide explains how to connect a cidx server (code-indexer server) to
SecOps, how to find its events, and how to build detections and dashboards on
them.

Companion document for the cidx operator: [SIEM Delivery](siem-delivery.md).
Every statement about cidx in this guide was checked against the cidx source
code. Every statement about Google products links to Google's documentation.
Where something could not be checked, the guide says so.

## Terms used in this guide

- **cidx**: the code-indexer server. It writes an audit log of security
  actions (logins, MFA changes, permission changes, admin actions).
- **UDM (Unified Data Model)**: the common event format of SecOps. Every event
  has fields such as `metadata.event_type`, `principal` (who acted) and
  `target` (what was acted on). See Google's
  [UDM field list](https://docs.cloud.google.com/chronicle/docs/reference/udm-field-list).
- **Parser**: SecOps code that turns a raw log line into UDM.
- **Customer ID**: the ID of your SecOps instance, a GUID such as
  `00000000-0000-0000-0000-000000000000`.
- **Service account**: a Google Cloud identity for a program, not a person.
  cidx uses one to call the Chronicle API.
- **JSON key**: a file that holds a service account's private key. cidx signs
  its token requests with it.
- **YARA-L 2.0**: the SecOps language for searches, dashboards and detection
  rules.

## The key fact: no parser is needed

cidx sends events that are **already in UDM**. It calls the Chronicle API
method [`events:import`](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.events/import)
with a body of this shape:

```json
{"inlineSource": {"events": [{"udm": {"metadata": {}, "principal": {}}}]}}
```

Google's
[Manage prebuilt parsers](https://docs.cloud.google.com/chronicle/docs/event-processing/manage-parser-updates)
page says that log types like UDM, "ingested using the udmevents API or
without raw logs", "don't require or have associated parsers since they are
already in a structured format". `events:import` sends no raw log at all. The
[parser extensions](https://docs.cloud.google.com/chronicle/docs/event-processing/using-parser-extensions)
page describes extensions as extracting fields "from raw log data".

Google's sentence names the `udmevents` API, not `events:import`, so the
following is an inference from it, not a Google statement about
`events:import`: **you do not need to write a parser. The events arrive
already in UDM.** A custom parser or a parser extension has no raw log to
work on for these events.

What you DO need to do:

1. Understand the fields cidx fills (section 5 and the
   [event catalog](siem-secops-event-catalog.md)).
2. Find the events with SIEM Search (section 6).
3. Write YARA-L detection rules and dashboards (section 6).

How to read field names in this guide:

- **camelCase and snake_case are the same fields.** The JSON cidx sends uses
  camelCase (`productLogId`). SecOps Search and rules use snake_case
  (`metadata.product_log_id`). The
  [protobuf JSON mapping](https://protobuf.dev/programming-guides/json/) maps
  field names to lowerCamelCase, and parsers accept both forms.
- **`additional.X` is shorthand** for the key `X` inside the `additional`
  container, for example `additional.cidx_instance`. In Search, type
  `additional.fields["cidx_instance"] = "cidx-example-1"`
  ([search syntax reference](https://docs.cloud.google.com/chronicle/docs/investigation/search-syntax-reference)).
  In a rule, follow Google's documented
  [map syntax](https://docs.cloud.google.com/chronicle/docs/yara-l/expressions)
  form `$e.udm.additional.fields["cidx_instance"] = "cidx-example-1"`. Only
  keys with string values work this way.

## 1. What cidx sends and how

### 1.1 What is captured

The scope is fixed in the cidx code. There is no setting to widen or narrow
it. cidx captures these audit actions (the "pilot scope"):

| Group | cidx action types |
|-------|-------------------|
| Logins on every door (REST, MCP, Web), success and failure | `authentication_success`, `authentication_failure` |
| MFA changes | `mfa_activated`, `mfa_disabled`, `mfa_recovery_codes_regenerated`, `mfa_secret_regenerated_cross_user` |
| Group and permission changes | `user_role_changed`, `user_group_assign`, `user_group_change`, `repo_access_grant`, `repo_access_revoke` |
| Admin actions | `user_created`, `user_deleted`, `user_password_reset_by_admin`, `mcp_credential_created`, `mcp_credential_revoked`, `api_key_created`, `ssh_key_host_assigned`, `elevation_granted`, `elevation_failed`, `impersonation_set`, `impersonation_cleared`, `impersonation_denied` |

SIEM delivery also reports on itself ("self-report" events):

- every change to the cidx `siem_delivery` settings (`config_changed`).
  Changes to other settings alone are not sent. But a reset of all settings,
  or one save across several sections, that touches a SIEM key is sent in
  full: target `*`, every changed key, and the allowlisted values of other
  sections too (see
  [`config_changed`](siem-secops-event-catalog.md#config_changed));
- every change of the service-account key (`siem_credential_changed`);
- every SIEM admin action (`siem_canary_sent`,
  `siem_canary_visibility_confirmed`, `siem_quarantine_requeued`,
  `siem_delivery_resumed`, `siem_batch_acknowledged`, `siem_batch_rebatched`,
  `siem_destination_retargeted`, `siem_destination_abandoned`).

Self-report events are captured whenever a destination is configured, even
before delivery is armed (section 4). So the first cidx events you see in
SecOps are often `config_changed` and `siem_canary_sent`.

Only typed values leave cidx. Each event carries fixed attributes (actor,
outcome, front door, peer address, correlation id, node id, auth method) plus
the `details` fields that cidx's read allowlist names for that action type.
No free text, message, URL or exception text is copied.

### 1.2 When delivery starts

cidx captures pilot events only after it is **armed**. Arming needs all of:

1. A synthetic **canary** batch, sent once and accepted by SecOps. It holds
   one event per UDM mapping entry plus one deliberately unmapped event
   (34 events with the current mapping).
2. A person confirming that **every** canary event is visible in SecOps
   search.
3. At least one live cidx server process, every live process able to get a
   token for the destination, and (in a cluster) every active node
   represented.

Section 4 walks through the canary. Nothing is captured while delivery is
disabled or not yet armed.

A canary confirmation is valid only for the configuration lifetime that
produced it. Any of these ends the lifetime: disabling or clearing the
destination, removing or replacing the service-account credential, changing
the trusted CA, or moving the destination to other coordinates. Delivery
disarms at once in the server process that made the change (other processes
and nodes follow at their next loop cycle, within about 30 seconds), and a
fresh canary and confirmation are needed before it arms again. While
delivery is disabled or has no destination the status reads `inactive`;
once it is enabled with a destination it reads `awaiting canary` until the
new canary is confirmed. Enabling delivery, and any other change of the
section (for example `max_batch_events` or the instance label), keeps a
confirmed canary valid.

Upgrade note: a destination that was already armed when this release was
installed stays armed until its next lifetime-ending change, even if its
canary predates the configuration it now runs with.

### 1.3 Delivery guarantees

- **At least once.** An event can arrive more than once. To count unique
  events, group by `metadata.product_log_id` (the cidx audit event UUID).
- **Duplicate halt policy.** cidx treats HTTP 409 as a duplicate response.
  Google's `events:import` reference documents no 409, so this response is
  unverified. On a 409, cidx does NOT count the batch as delivered. It halts
  delivery and waits for a cidx admin to decide: acknowledge the batch (the
  events are confirmed present in SecOps) or rebatch it (send again and
  accept possible duplicates).
- **Capture is best effort.** The queue row is written in the same database
  transaction as the audit row. If that insert fails, the user's action still
  succeeds; cidx counts the gap and reports it on its health page.
- **After a disable**, a cidx process normally keeps capturing for at most
  90 seconds. cidx counts those events and still delivers them. The
  bound is suspended while a process runs on its last-known-good config
  (its health says so), and captures later than 90 s are counted and shown
  in `GET /api/system/health` (`failure_reasons`).
- **Ordering.** cidx builds batches from its queue in capture order, but it
  does not promise arrival order: it keeps up to three open batches per
  destination, retries failed batches later, and can requeue quarantined
  events. Sort on `metadata.event_timestamp`, which is the time the action
  happened in cidx.
- **Throttling.** On HTTP 429 from SecOps, cidx stops the current cycle for
  that destination and retries no sooner than the `Retry-After` value
  (capped at one hour). Events stay queued.
- A single request holds at most `max_batch_events` events and at most about
  2,000,000 bytes.

## 2. Configure cidx, field by field

The settings live in the cidx Web UI: menu **Config**, section **SIEM
Delivery (Google SecOps)**. Click **Edit**, change the fields, click
**Save**. A cidx admin does this.

Every save of this section needs TOTP elevation (a fresh MFA code). Every
save is audited as `config_changed` and sent to SecOps (see
[`config_changed`](siem-secops-event-catalog.md#config_changed) in the event
catalog). Values
are recorded in the audit row only for `enabled`, `api_version`,
`max_batch_events`, `region` and the trusted-CA fingerprint; the other fields
are recorded by name only. cidx trims leading and trailing spaces from every
text field.

### 2.1 Where to find your SecOps values

Google's
[RBAC page](https://docs.cloud.google.com/chronicle/docs/administration/rbac)
says the **Profile** page in SecOps settings shows your customer ID, Google
Cloud project number and Google Cloud project ID, and that the customer ID is
in the **Organization Details** section. Google's collection guides (for
example [this one](https://docs.cloud.google.com/chronicle/docs/ingestion/default-parsers/wiz-io))
give the path as **SIEM Settings > Profile**. The menu label can differ by
console version; if you cannot find it, check with your Google admin.

### 2.2 The fields

| Field | What it is | Allowed values | Default | Where to find it | Example |
|-------|-----------|----------------|---------|------------------|---------|
| `enabled` | The capture switch. Events already captured keep being delivered whatever this says. | Yes / No | No | Your decision | Yes |
| `region` | The SecOps region. cidx builds the endpoint from it. | One of the 18 regions in the next list. Required when `enabled` is Yes. | empty | The region your SecOps instance was provisioned in. Ask your Google admin if unsure. | `us` |
| `api_version` | Chronicle API version in the URL. | `v1`, `v1beta`, `v1alpha` | `v1` | Use `v1` unless told otherwise. | `v1` |
| `project_id` | The Google Cloud project linked to your SecOps instance. | 1 to 128 characters, letters, digits, `_` and `-`, starting with a letter or digit. Required when enabled. | empty | SecOps Profile page (project ID). | `example-secops-project` |
| `location` | The location in the API path. | Same pattern as `project_id`. Required when enabled. | empty | Same value as the region (see 2.4). | `us` |
| `instance_id` | Your SecOps customer ID. | Same pattern as `project_id`. Required when enabled. | empty | SecOps Profile page, Organization Details, Customer ID. | `00000000-0000-0000-0000-000000000000` |
| `max_batch_events` | Most events per request. | Whole number 1 to 1000 | 1000 | Leave the default unless you have a reason. | `1000` |
| `source_instance_label` | A name for this cidx server. Sent in every event as `additional.cidx_instance`. Use it to tell several cidx servers apart. | 1 to 64 characters: letters, digits, `.`, `_`, `-`. Required when enabled. | empty | You choose it. | `cidx-example-1` |
| `harness_endpoint` | Test receiver for cidx's own automated tests. **Leave empty.** | Accepted only on a non-production cidx process with the fault-injection test gate on; refused otherwise. | empty | Not applicable | (empty) |

Allowed regions (the same list as Google's
[Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide)
guide, checked on 2026-10-02): `us`, `eu`, `africa-south1`,
`asia-northeast1`, `asia-south1`, `asia-southeast1`, `asia-southeast2`,
`australia-southeast1`, `europe-west2`, `europe-west3`, `europe-west6`,
`europe-west9`, `europe-west12`, `me-central1`, `me-central2`, `me-west1`,
`northamerica-northeast2`, `southamerica-east1`.

A validation error names the field, never the value, for example
`SIEM Delivery: invalid region (not a documented SecOps region)`.

### 2.3 How the endpoint URL is built

There is no free URL field. cidx builds the URL from the fields:

```text
https://chronicle.<region>.rep.googleapis.com/<api_version>/projects/<project_id>/locations/<location>/instances/<instance_id>/events:import
```

Example:

```text
https://chronicle.us.rep.googleapis.com/v1/projects/example-secops-project/locations/us/instances/00000000-0000-0000-0000-000000000000/events:import
```

Why there is no URL field: cidx only sends to a documented Google regional
endpoint, and each path part must be a single validated segment. A typo or a
pasted URL cannot redirect audit data elsewhere. The URL shape matches
Google's
[Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide)
guide: `https://[service_endpoint]/[api_version]/projects/[project_id]/locations/[location]/instances/[instance_id]/...`.

### 2.4 Region and location

They are two fields, but for SecOps they hold the same value. Google's
migration guide defines `location` as "The location of your project
(region); same as the regional endpoints", and its own example uses
`chronicle.us.rep.googleapis.com` with `locations/us`. cidx does not check
that they match, so set both carefully.

Changing `region`, `project_id`, `location` or `instance_id` makes a new
destination. A new destination disarms delivery, and you must run the canary
again. Changing `api_version` does not change the destination.

### 2.5 Service-account credential

Below the settings is **Service Account Credential**. Paste the JSON key into
the text box, or upload the key file, then click **Set / Replace
Credential**. Section 3 covers how to create the key. **Remove Credential**
deletes it. Both need TOTP elevation. Replacing or removing a stored key
disarms delivery and needs a fresh canary (section 1.2).

### 2.6 Additional trusted CA (optional)

Below the credential is **Additional Trusted CA (optional)**. Use it only if
your network has a TLS-inspecting proxy between cidx and Google. Paste or
upload the proxy's CA certificates (PEM), then click **Set / Replace Trusted
CA**.

- Each certificate must be a CA (basicConstraints CA=true) and not expired.
- The bundle is limited to 256 KiB.
- The certificates are ADDED to cidx's default trust for both outbound calls
  (the token request and `events:import`). Certificate and hostname checks
  always stay on.
- cidx honours the `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY`
  environment variables.
- Changes need TOTP elevation and are audited as `config_changed` with the
  bundle's SHA-256 fingerprint before and after.
- Setting, replacing or removing the bundle disarms delivery and needs a
  fresh canary (section 1.2). Set it before the canary.

## 3. Create the Google service account

### 3.1 Which permission it needs

The `events:import` reference says the call needs the IAM permission
`chronicle.events.import` on the parent resource, and one of the OAuth scopes
`https://www.googleapis.com/auth/cloud-platform` or
`https://www.googleapis.com/auth/chronicle`. cidx requests the `chronicle`
scope.

Google's
[Chronicle roles reference](https://docs.cloud.google.com/iam/docs/roles-permissions/chronicle)
lists these predefined roles as containing `chronicle.events.import`:
Owner (`roles/owner`), Editor (`roles/editor`), Chronicle API Admin
(`roles/chronicle.admin`), Chronicle API Editor (`roles/chronicle.editor`),
Admin (`roles/admin`) and Writer (`roles/writer`). All of them grant far
more than importing events.

For least privilege, create a **custom role** that contains only
`chronicle.events.import`. Google's
[custom role support table](https://docs.cloud.google.com/iam/docs/custom-roles-permissions-support)
marks `chronicle.events.import` as `SUPPORTED`.

### 3.2 Step by step

Do these steps in the Google Cloud project linked to your SecOps instance
(the project ID from section 2.1).

1. **Check that the Chronicle API is enabled** in the project. Google's
   [Configure a Google Cloud project](https://docs.cloud.google.com/chronicle/docs/onboard/configure-cloud-project)
   page describes enabling it under **APIs & Services**.
2. **Create the custom role** (Google:
   [Create and manage custom roles](https://docs.cloud.google.com/iam/docs/creating-custom-roles)):
   1. In the Google Cloud console, go to the **Roles** page.
   2. Select the project at the top of the page.
   3. Click **Create custom role**.
   4. Enter a title (for example `cidx SIEM event import`), a description and
      an ID.
   5. Click **Add Permissions**, select `chronicle.events.import`, click
      **Add Permissions**, then create the role.
3. **Create the service account** (Google:
   [Create service accounts](https://docs.cloud.google.com/iam/docs/service-accounts-create)):
   1. Go to the **Create service account** page and select the project.
   2. Enter a name, for example `secops-sa`. The console suggests an ID; it
      cannot be changed later. The account's email becomes
      `secops-sa@example-secops-project.iam.gserviceaccount.com`.
   3. Click **Create and continue**.
   4. In the role step, choose the custom role from step 2. Click
      **Continue**, then **Done**.
4. **Or grant the role afterwards** (Google:
   [Manage access](https://docs.cloud.google.com/iam/docs/granting-changing-revoking-access)):
   go to the **IAM** page, select the project, click **Grant Access**, enter
   the service account email, select the custom role, save.
5. **Create a JSON key** (Google:
   [Create and delete service account keys](https://docs.cloud.google.com/iam/docs/keys-create-delete)):
   1. Go to the **Service accounts** page and select the project.
   2. Click the service account's email address.
   3. Open the **Keys** tab.
   4. Click **Add key**, then **Create new key**.
   5. Select **JSON** and click **Create**. The file downloads once; Google
      cannot download it again.

   If key creation is blocked, the organization policy constraint
   `iam.disableServiceAccountKeyCreation` is probably enforced. Google says
   that for organizations created on or after May 3, 2024 it is enforced by
   default. An organization policy administrator must exempt the project;
   check with your Google admin.

### 3.3 Paste the key into cidx

1. Open the file in a text editor and check the `token_uri` line (see 3.4).
2. In the cidx Web UI section **SIEM Delivery (Google SecOps)**, paste the
   whole file into **Paste the service-account key JSON**, or choose it under
   **Or upload the key file**. Use one, not both.
3. Click **Set / Replace Credential** and complete the TOTP prompt.
4. The status table now shows **Service account credential** as
   `<client_email> (key id <private_key_id>), set by <admin> at <time>`.
5. Delete the downloaded file from the workstation once cidx accepted it.

### 3.4 What cidx checks

cidx rejects the key, and changes nothing, unless:

- the text is valid JSON, at most 64 KiB, and a JSON object;
- `type` is `service_account`;
- `client_email`, `private_key`, `private_key_id` and `token_uri` are present
  and not empty;
- `client_email` looks like an email address and `private_key_id` is letters
  and digits only;
- `token_uri` is exactly `https://oauth2.googleapis.com/token`;
- the private key loads and is an RSA key.

**Check `token_uri` before you paste.** cidx accepts only
`https://oauth2.googleapis.com/token`. On Google's
[key creation page](https://docs.cloud.google.com/iam/docs/keys-create-delete),
the Console example key shows
`"token_uri": "https://accounts.google.com/o/oauth2/token"`, while the gcloud
example shows `"token_uri": "https://oauth2.googleapis.com/token"`. This
guide walks the Console path. If your downloaded file carries the older
value, cidx refuses it with
`token_uri is not the allowed token endpoint`. Whether current key files ever
carry the older value was not verified; report it to the cidx maintainers if
you see it.

### 3.5 How cidx stores the key

- One row in the cidx database, shared by every cidx node.
- The key is AES-256 encrypted. Only `client_email`, `private_key_id`, who
  set it and when are stored in clear, plus a random `credential_id` and an
  HMAC key-check value (used to detect a changed encryption key). Neither is
  secret.
- It is write-only: no page, API or log shows the key again.
- The change is audited as `siem_credential_changed` with `change` (`set`,
  `replaced`, `removed`), `client_email` and `private_key_id`. That event is
  sent to SecOps.
- Every token request reads the stored key, so a replace on any node applies
  everywhere at the next request.

### 3.6 Rotate the key

This follows Google's
[key rotation](https://docs.cloud.google.com/iam/docs/key-rotation) steps.

1. Create a new JSON key for the same service account (step 3.2.5).
2. Paste it into cidx with **Set / Replace Credential**. cidx records
   `siem_credential_changed` with `change` = `replaced`. Replacing the key
   ends the configuration lifetime the canary was confirmed for: delivery
   disarms at once. Events captured before keep being delivered with the new
   key; new events are not captured until delivery is armed again.
3. Run the canary and confirm it again (section 4.1, steps 2 to 4); cidx
   re-arms on a later loop cycle.
4. In the Google Cloud console, disable the old key and watch cidx health
   (section 7) and SecOps for new cidx events.
5. When delivery works, delete the old key: **Service accounts**, the
   account, **Keys** tab, delete the old key.

Plan the rotation for a quiet period: between step 2 and the confirmation in
step 3, cidx does not capture new events for SecOps.

### 3.7 Security advice

From Google's
[best practices for managing service account keys](https://docs.cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys):

- Grant only `chronicle.events.import` (the custom role above).
- Don't leave keys in temporary locations, don't pass them between users,
  don't commit them to source code repositories.
- Rotate keys routinely.
- Use a dedicated service account for each cidx deployment, so a
  `siem_credential_changed` event and the Google IAM audit trail point to one
  system.

## 4. Turn it on (arming)

### 4.1 Steps

A cidx admin and a SecOps analyst do this together.

The order is: configure, canary, confirm, then enable. The canary runs while
`enabled` is still No, as long as a destination is configured.

1. **cidx admin:** upload the key (section 2.5) and, only if needed, the
   additional trusted CA (section 2.6). Then fill all fields (section 2),
   leave `enabled` at No, and save. Check that **This process** shows
   `probe=ok` (this process can get a token).
2. **cidx admin:** in the same SIEM Delivery section, under **Operations**,
   press **Run canary** and confirm. While TOTP elevation enforcement is on,
   cidx asks for a TOTP code first (the action is then replayed). The arming
   panel lists every expected `product_log_id` with its action and event
   type, and the run-wide search for this run (see 4.2). If the canary is
   rejected, the checklist shows its signature.
3. **SecOps analyst:** find each ID (see 4.2) and tell the cidx admin which
   ones are visible.
4. **cidx admin:** tick the visible IDs, or paste them into the box
   (separated by spaces, new lines or commas), and press **Confirm
   visible**. The result shows "Confirmed N of M"; arming needs every
   expected ID, including the unmapped one. If some are missing, the
   checklist names their action types; wait and confirm again.
5. **cidx admin:** set `enabled` to Yes and save. Enabling keeps the
   confirmed canary valid.
6. Watch the **ARMED** row of the checklist. cidx arms on a later loop cycle,
   once every live process also has a fresh `probe=ok` (the "Processes
   ready" row lists any that do not).

If the configuration lifetime ends before or after arming (section 1.2:
disable, clear, new destination, key replaced or removed, trusted CA
changed), delivery disarms and steps 2 to 4 must be repeated: the status
reads `inactive` while delivery is disabled or has no destination, and
`awaiting canary` once it is enabled again. A canary or confirmation of an
earlier lifetime is refused as stale (HTTP 409).

#### 4.1a Without the Web UI (REST)

The same steps over REST. The complete operator walkthrough with curl --
variables, REST login with MFA, the web session with CSRF (the key and CA
uploads exist only as Web UI forms), TOTP elevation per session, configure,
canary, the SecOps or emulator search, confirm, operate, recovery and
decommission -- is the
[SIEM Delivery curl runbook](siem-secops-curl-runbook.md).

The Web UI `session` cookie is `Secure` whenever the server is not bound to
localhost, so browsers and curl send it back only over `https`. A deployment
served over plain `http` needs a TLS front door for browser and curl
web-form use (public issue #2004).

1. **cidx admin:** upload the key and fill all fields with `enabled` at No,
   as in step 1 above (Web UI forms; see the runbook for the curl form
   posts).
2. **cidx admin:** send the canary. This REST call needs an admin token and
   TOTP elevation (`POST /auth/elevate` with `{"totp_code": "123456"}` on the
   same token first; see [TOTP elevation](totp-elevation.md)):

   ```bash
   curl -s -X POST https://cidx.example.com/api/admin/siem-delivery/canary \
     -H "Authorization: Bearer $TOKEN"
   ```

   A successful answer looks like:

   ```json
   {
     "canary_run_id": "6c1d2e3f-0000-4000-8000-000000000001",
     "result": "accepted",
     "result_signature": null,
     "expected_product_log_ids": ["00000000-0000-4000-8000-000000000001", "..."],
     "event_count": 34,
     "mapping_version": 2
   }
   ```

   If `result` is `rejected`, `result_signature` has the form
   `status|google|class|path`: the HTTP status, the Google status code, the
   cidx outcome class and a sanitised field path, for example
   `400|INVALID_ARGUMENT|row_rejection|unindexed`. Google's message text is
   never stored.
3. **SecOps analyst:** find each ID from `expected_product_log_ids` (see 4.2)
   and tell the cidx admin which ones are visible.
4. **cidx admin:** confirm (also needs TOTP elevation):

   ```bash
   curl -s -X POST https://cidx.example.com/api/admin/siem-delivery/canary/confirm-visible \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"canary_run_id": "6c1d2e3f-0000-4000-8000-000000000001",
          "visible_product_log_ids": ["00000000-0000-4000-8000-000000000001", "..."]}'
   ```

   The answer lists `confirmed`, `expected_count`, `confirmed_count` and
   `missing_action_types`. Arming needs every expected ID, including the
   unmapped one. If some are missing, wait and confirm again with the full
   list. A `409` "canary run is stale" answer means the configuration
   lifetime changed after the canary (section 1.2): run it again.
5. **cidx admin:** set `enabled` to Yes (Web UI form).
6. cidx arms on a later loop cycle, once every live process also has a
   fresh `probe=ok`. Until then **Capture state** may read
   `awaiting process readiness`. Then it becomes `armed`.

TOTP codes are single-use per 30-second step for the account, and the web
session and the Bearer token are elevated separately: elevate them with codes
from two DIFFERENT 30-second steps, or the second elevation fails with
`401 elevation_failed`.

### 4.2 What the canary events look like and how to find them

Each canary event:

- has `metadata.vendor_name` = `CIDX` and `metadata.product_name` =
  `cidx-server`;
- has `metadata.product_log_id` = one of the expected IDs;
- has `principal.application` = `cidx-server` and `principal.ip` =
  `192.0.2.10` (a documentation-only address);
- uses sample values such as `example-user`, `example-repo-global` and
  `example-id` (for opaque ids such as group names and key ids);
- has `additional.cidx_canary` = true and `additional.correlation_id` =
  `canary-<canary_run_id>`.

To find them, open **Investigation > SIEM Search** (Google:
[Understand search](https://docs.cloud.google.com/chronicle/docs/investigation/udm-search)),
set the time range to cover the canary, and search:

```text
metadata.vendor_name = "CIDX" AND metadata.product_log_id = "00000000-0000-4000-8000-000000000001"
```

Or list the whole run:

```text
metadata.vendor_name = "CIDX" AND additional.fields["correlation_id"] = "canary-6c1d2e3f-0000-4000-8000-000000000001"
```

The `additional.fields["key"] = "value"` form is documented in Google's
[search syntax reference](https://docs.cloud.google.com/chronicle/docs/investigation/search-syntax-reference)
(string values only; `correlation_id` is a string). If it returns nothing on
your tenant, search by `metadata.product_log_id`.

Canary events are synthetic. Exclude them from detections with
`metadata.product_log_id` lists or by ignoring events where
`principal.ip = "192.0.2.10"` and `principal.application = "cidx-server"`.

## 5. Event catalog

The full catalog is a separate document:
[cidx Event Catalog for Google SecOps](siem-secops-event-catalog.md). For
every event it lists the meaning, the UDM fields cidx fills, the
`cidx_details` keys and an example UDM event. Its section
[Fields every event carries](siem-secops-event-catalog.md#2-fields-every-event-carries)
lists the fields common to all events and which `additional` fields rules can
match.

Summary:

| cidx event (`metadata.product_event_type`) | `metadata.event_type` | Outcome field | Alert idea |
|---|---|---|---|
| `authentication_success` | `USER_LOGIN` | `security_result.action` ALLOW | Logins at odd hours, new accounts logging in |
| `authentication_failure` | `USER_LOGIN` | BLOCK | Repeated failures for one account |
| `elevation_granted` | `USER_LOGIN` | ALLOW | Elevation by unexpected admins |
| `elevation_failed` | `USER_LOGIN` | BLOCK | Repeated failed MFA step-up |
| `mfa_activated` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Informational |
| `mfa_disabled` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Any MFA removal |
| `mfa_recovery_codes_regenerated` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Review |
| `mfa_secret_regenerated_cross_user` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Any (one user reset another user's MFA) |
| `user_role_changed` | `USER_CHANGE_PERMISSIONS` | ALLOW; BLOCK on a failed attempt | Any change to `admin`; failed admin action |
| `user_group_assign` | `GROUP_MODIFICATION` | often absent | Review |
| `user_group_change` | `GROUP_MODIFICATION` | often absent | Moves into admin groups |
| `repo_access_grant` | `USER_RESOURCE_UPDATE_PERMISSIONS` | often absent | Review |
| `repo_access_revoke` | `USER_RESOURCE_UPDATE_PERMISSIONS` | often absent | Review |
| `user_created` | `USER_CREATION` | ALLOW; BLOCK on a failed attempt | New admins; failed admin action |
| `user_deleted` | `USER_DELETION` | ALLOW; BLOCK on a failed attempt | Bulk deletions; failed admin action |
| `user_password_reset_by_admin` | `USER_CHANGE_PASSWORD` | ALLOW; BLOCK on a failed attempt | Any |
| `mcp_credential_created` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Created for someone else |
| `mcp_credential_revoked` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Informational |
| `api_key_created` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Review |
| `ssh_key_host_assigned` | `USER_UNCATEGORIZED` | ALLOW; BLOCK on a failed attempt | Review |
| `impersonation_set` | `USER_UNCATEGORIZED` | often absent | Any |
| `impersonation_cleared` | `USER_UNCATEGORIZED` | often absent | Informational |
| `impersonation_denied` | `USER_UNCATEGORIZED` | BLOCK | Any |
| `config_changed` (only when a SIEM setting is touched) | `SETTING_MODIFICATION` | ALLOW or BLOCK | Any |
| `siem_credential_changed` | `SETTING_MODIFICATION` | ALLOW | Any |
| `siem_canary_sent` | `STATUS_UPDATE` | ALLOW | Informational |
| `siem_canary_visibility_confirmed` | `STATUS_UPDATE` | ALLOW | Informational |
| `siem_quarantine_requeued` | `STATUS_UPDATE` | ALLOW | Informational |
| `siem_delivery_resumed` | `STATUS_UPDATE` | ALLOW | Review |
| `siem_batch_acknowledged` | `STATUS_UPDATE` | ALLOW | Review |
| `siem_batch_rebatched` | `STATUS_UPDATE` | ALLOW | Review |
| `siem_destination_retargeted` | `STATUS_UPDATE` | ALLOW | Any |
| `siem_destination_abandoned` | `STATUS_UPDATE` | ALLOW | Any (audit events dropped on purpose) |
| any other type (fallback) | `GENERIC_EVENT` | as recorded | Any (mapping gap) |

Outcome rule: cidx's `outcome` `success` becomes `security_result.action`
`ALLOW`; `failure` and `denied` become `BLOCK`; `attempted` or no outcome
means no `security_result` at all. "Often absent" marks the older audit
writers (group, repository access, impersonation), which record an outcome
only when it is given or the type name ends in `_success`, `_failure` or
`_denied`. A failed MFA or admin action is sent as `BLOCK`, usually without
`cidx_details` and often without a target; see the catalog's
[Summary](siem-secops-event-catalog.md#1-summary).

## 6. Searching and alerting

Open **Investigation > SIEM Search**, set the **Time Range**, type the query,
click **Run Search** (Google:
[Understand search](https://docs.cloud.google.com/chronicle/docs/investigation/udm-search)).
The examples join conditions with `AND`, as Google's
[search syntax reference](https://docs.cloud.google.com/chronicle/docs/investigation/search-syntax-reference)
documents. Case: that reference says string comparisons in YARA-L 2.0 are
case-sensitive by default and that `nocase` makes them case-insensitive.
Google's rules editor page adds that the Search web interface sets its Case
sensitivity option to Off by default, so add `nocase` to rule conditions
copied from Search. cidx values are lowercase except `CIDX` and the UDM enum
values (`USER_LOGIN`, `BLOCK`), so type them exactly as shown.

### 6.1 Search examples

All cidx events:

```text
metadata.vendor_name = "CIDX" AND metadata.product_name = "cidx-server"
```

Failed logins:

```text
metadata.vendor_name = "CIDX" AND metadata.event_type = "USER_LOGIN" AND security_result.action = "BLOCK"
```

Admin and permission changes:

```text
metadata.vendor_name = "CIDX" AND metadata.product_event_type = /^(user_role_changed|user_group_assign|user_group_change|repo_access_grant|repo_access_revoke|user_created|user_deleted|user_password_reset_by_admin|impersonation_set)$/
```

Failed admin and MFA actions:

```text
metadata.vendor_name = "CIDX" AND security_result.action = "BLOCK" AND metadata.event_type != "USER_LOGIN"
```

MFA changes:

```text
metadata.vendor_name = "CIDX" AND metadata.product_event_type = /^mfa_/
```

Events of one cidx server (map syntax; see the note in the catalog's
[Fields every event carries](siem-secops-event-catalog.md#2-fields-every-event-carries)):

```text
metadata.vendor_name = "CIDX" AND additional.fields["cidx_instance"] = "cidx-example-1"
```

Adapt these to your environment. The regular expression form `/.../` follows
Google's
[rule examples](https://docs.cloud.google.com/chronicle/docs/yara-l/yara-l-2-0-examples).

### 6.2 Detection rules (YARA-L 2.0)

Create rules in **Detections > Rules & detections**, **Rules editor** tab,
**New** (Google:
[Edit rules in the Rules Editor](https://docs.cloud.google.com/chronicle/docs/detection/manage-all-rules)).
The structure (`meta`, `events`, `match`, `condition`, `#e` counts, `over`
windows) follows Google's
[Get started with YARA-L](https://docs.cloud.google.com/chronicle/docs/detection/yara-l-2-0-syntax)
and
[rule examples](https://docs.cloud.google.com/chronicle/docs/yara-l/yara-l-2-0-examples).
These rules were not run against a live tenant. Test them in the rules
editor and adapt them to your environment.

Repeated failed logins for one account:

```text
rule cidx_repeated_failed_logins {
  meta:
    author = "example-secops-team"
    description = "Five or more failed cidx logins for one account in 10 minutes"
    severity = "Medium"
  events:
    $e.metadata.vendor_name = "CIDX"
    $e.metadata.product_event_type = "authentication_failure"
    $e.target.user.userid = $user
  match:
    $user over 10m
  condition:
    #e >= 5
}
```

Failures with an unknown account name have no `target.user`, so this rule
does not count them. Grouping by `principal.ip` is not useful until issue
#2006 is fixed (section 8).

A role change (including promotion to admin):

```text
rule cidx_user_role_changed {
  meta:
    author = "example-secops-team"
    description = "A cidx user's role changed"
    severity = "High"
  events:
    $e.metadata.vendor_name = "CIDX"
    $e.metadata.product_event_type = "user_role_changed"
  condition:
    $e
}
```

The new role is in `additional.cidx_details.new_role`, a nested value that
map syntax probably cannot read (see
[Fields every event carries](siem-secops-event-catalog.md#2-fields-every-event-carries)).
Read it in the alert's event view.

Changes to SIEM delivery itself:

```text
rule cidx_siem_delivery_changed {
  meta:
    author = "example-secops-team"
    description = "cidx SIEM delivery settings, key or queue were changed"
    severity = "High"
  events:
    $e.metadata.vendor_name = "CIDX"
    $e.metadata.product_event_type = /^(config_changed|siem_credential_changed|siem_destination_abandoned|siem_destination_retargeted|siem_delivery_resumed|siem_batch_acknowledged|siem_batch_rebatched)$/
  condition:
    $e
}
```

Also consider a rule or dashboard for silence: if cidx normally sends logins
every hour, an hour with none can mean delivery stopped.

Dashboards: the same filters work as dashboard queries. Count by
`metadata.product_event_type` and by `additional.fields["cidx_instance"]`
for an overview.

## 7. Health and troubleshooting

### 7.1 What the cidx operator sees

**`GET /api/system/health`.** SIEM problems only ever make the node
DEGRADED, never unhealthy. A SecOps outage does not take cidx down. The
reasons appear in this authenticated endpoint's `failure_reasons`. The
public load-balancer probe `/healthz` returns the resulting status only
(DEGRADED still answers HTTP 200), and `GET /health` does not include SIEM
reasons. Reasons include:

- `SIEM delivery not running in this process: <error>`
- `SIEM delivery config unreadable in this process; using last-known-good (the 90 s stale-capture bound is suspended here)`
- `SIEM delivery halted: <class>`
- `SIEM delivery loop stalled in this process`
- `SIEM delivery config never loaded in this process; capture INACTIVE here`
- `SIEM delivery backlog: oldest pending event is <n> s old` (after 15
  minutes)
- `SIEM delivery: <n> events quarantined`
- `SIEM delivery: <n> events unrecoverable (...)`
- `SIEM delivery canary not run for mapping version <n>`
- `SIEM delivery backlog large or disk headroom low`
- `SIEM delivery: process <id> cannot mint SecOps tokens: <reason>`, where
  reason is `credential_missing`, `credential_invalid`,
  `credential_key_mismatch`, `token_uri_not_allowed`, `token_rejected` or
  `token_endpoint_unreachable`
- `SIEM delivery: <n> events pending for unconfigured destination <key>`
- `SIEM capture failures since boot: <n>`
- `SIEM delivery: <n> pilot events captured more than 90 s after a SIEM disable/change`

**Stats.** `GET /api/admin/siem-delivery/stats` (admin token) returns the
fleet counts (`pending`, quarantine, delivered total), the credential
identity, the capture state and canary status, the live processes and their
token probe results, and any halt (`class`, `signature`, `since`,
`next_probe_at`, `batch_id`). The Web UI status table shows a summary.

**Halts.** A request-wide failure halts delivery without dropping anything:
HTTP 400 at request level, 401 or 403 (`credential`), 404, 413, 415, 501,
409 (`duplicate_response`), and unclassified responses. cidx re-probes every
15 minutes and clears a halt by itself once the cause is fixed. The
duplicate halt is the exception: an admin must acknowledge or rebatch the
batch (section 1.3). `POST /api/admin/siem-delivery/resume` clears any halt
at once.

Halt classes, as they appear in `GET /api/system/health` and in
`siem_delivery_resumed`:

- `request_rejection`: SecOps refused the whole request for a request-level
  reason (HTTP 400 whose error names a field outside the events, or 404,
  413, 415, 501).
- `credential`: SecOps answered 401 or 403 (token or permission problem).
- `duplicate_response`: SecOps answered 409; an admin must decide.
- `unclassified`: any other unexpected response.
- `row_rejection_burst`: SecOps rejected specific events more often than the
  quarantine cap allows.
- `local_validation_burst`: cidx's own pre-send check failed for more events
  than the quarantine cap allows.

**Quarantine.** Google's `events:import` reference says an error in one
event rejects the whole request. When SecOps rejects a request because of
specific events, cidx finds them from the field-violation paths in the
error, or by splitting the batch in half and resending each half. It sets
those events aside ("quarantine") and keeps sending the rest. cidx's own
pre-send check also quarantines events it cannot send. At most 5 events per
hour are quarantined fleet-wide; the next failure halts delivery instead.
`GET /api/admin/siem-delivery/quarantine` lists them.
`POST /api/admin/siem-delivery/quarantine/requeue` with the body
`{"event_uuids": ["..."]}` (at most 500 per call) retries them.

**Retarget and abandon.** Events captured for a destination that is no longer
configured wait. An admin either moves them to the current destination
(`.../destinations/<key>/retarget`) or drops them (`.../abandon`). Both are
audited and sent to SecOps.

**Alert logs.** cidx logs an ERROR when the oldest pending event is older than
60 minutes, a halt lasts more than 15 minutes, the backlog passes 5 GB,
projected disk headroom drops under 12 hours, or any capture failure happens.
It repeats at most hourly and logs one INFO line on recovery. There is no
built-in paging.

### 7.2 What SecOps staff should check

1. **The service account.** It exists, is enabled, and its key was not
   disabled or deleted. A `credential` halt (HTTP 401 or 403) points here.
2. **The role grant.** The account holds a role with
   `chronicle.events.import` on the SecOps project (section 3).
3. **The Chronicle API** is enabled in that project.
4. **The destination values.** Region, project, location and customer ID
   match your instance. A `request_rejection` halt with HTTP 404 in its
   signature can point here.
5. **Quota.** Google's
   [quotas and burst limits](https://docs.cloud.google.com/chronicle/docs/ingestion/burst-limits)
   page says push ingestion over the burst limit is rejected with HTTP 429.
   cidx keeps such events queued and retries (section 1.3).
6. **Ingestion monitoring.** Google's
   [Check data ingestion health](https://docs.cloud.google.com/chronicle/docs/ingestion/ingestion-overview)
   page describes the Health Hub and the Data Ingestion dashboard, which
   refresh about every 15 minutes. Whether events sent with `events:import`
   appear there, and under which log type, was not verified. Google's
   [Ingestion API](https://docs.cloud.google.com/chronicle/docs/reference/ingestion-api)
   page says events sent through its `udmevents` method get
   `metadata.log_type = "UDM"`; whether `events:import` does the same was
   not verified. Searching `metadata.vendor_name = "CIDX"` works regardless.
7. **Silence.** If cidx events stop, ask the cidx operator for
   `GET /api/system/health` and the stats document before changing anything
   on the Google side. A recent key replacement, trusted-CA change or
   disable also stops capture until a fresh canary is confirmed (section
   1.2).

## 8. Known limitations

- **`principal.ip` is the reverse proxy's address, not the client's.** cidx
  records the immediate peer of the HTTP connection. Behind a reverse proxy
  that is the proxy. Do not use `principal.ip` for client attribution, geo
  lookups or IP-based rules yet. Public issue #2006 tracks the fix, which is
  required before production use with a real tenant.
- **Pilot scope only.** Logins on every door, MFA changes, permission and
  group changes, and the admin actions in section 1.1. Refused-access events
  (HTTP 403 on normal requests) are not included yet. Other cidx audit
  events (for example general configuration changes outside the SIEM section,
  repository management) are not sent.
- **Mapping version.** The set of UDM mappings has a version, currently 2.
  It is not in each event; `additional.schema_version` (currently 1) is the
  version of the cidx payload, a different thing. When a cidx upgrade brings
  a new mapping version: delivery stays armed, cidx health shows
  `SIEM delivery canary not run for mapping version <n>` until the canary is
  run again, and quarantined events are requeued once automatically (with a
  `siem_quarantine_requeued` event, `trigger` = `mapping_version_change`,
  only when at least one event was requeued). A new mapping can change the
  `metadata.event_type` of a cidx event. Rules that match on
  `metadata.product_event_type` (the cidx name) do not depend on that.
- **Departures from Google's UDM usage guide.** Google's
  [UDM usage guide](https://docs.cloud.google.com/chronicle/docs/unified-data-model/udm-usage)
  asks for the following, and cidx departs from it:
  - `security_result.category` = `AUTH_VIOLATION` on failed `USER_LOGIN`
    events; cidx sends only `security_result.action`.
  - An authentication extension on `USER_LOGIN`; cidx sends none on
    `elevation_granted` and `elevation_failed`, and only the type (not the
    mechanism) on logins.
  - `network` and `network.http` details for logins over HTTP; cidx sends
    none.
  - A target resource of type `SETTING` for `SETTING_MODIFICATION`; cidx
    uses type `config`, in `target.resource.type`, which the guide calls
    deprecated in favour of `resource_type`.
  - A principal machine identifier for `STATUS_*` and `SETTING_*` events;
    cidx system self-reports carry only `principal.application`.

  The canary does not prove SecOps accepts every shape. It sends only
  successful events from a system actor that has an IP. It sends no `BLOCK`
  results, no user principals and no system events without an IP (such as
  an automatic `siem_quarantine_requeued`).
- **Not yet run against a real tenant.** The cidx operator's
  [real-tenant checklist](siem-delivery.md#real-tenant-enablement-checklist) still lists items to confirm on
  a real SecOps tenant, including the exact rejection format, the duplicate
  response and the IAM permission name.
- **Structured details.** `additional.cidx_details` is a nested object.
  Google documents map access in rules only for string values, so its
  contents may only be readable in the event viewer.
