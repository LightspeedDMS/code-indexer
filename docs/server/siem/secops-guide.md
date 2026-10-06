# Google SecOps Guide for CIDX Audit Events

This guide is for security operations staff who run Google Security
Operations (SecOps). Google's API for SecOps is still called the Chronicle
API. It covers the Google side of connecting a CIDX server (code-indexer
server): the values the CIDX administrator needs, the service account and its
key, checking the canary, and searching and alerting on CIDX events.

The CIDX side (settings, arming, health, recovery) is in
[SIEM Delivery: CIDX Operations](operations.md). Every delivered event and its
UDM fields are in the [event catalog](event-catalog.md). Statements about CIDX
were checked against the CIDX source code; statements about Google products
link to Google's documentation, and where something could not be checked the
guide says so.

## Terms used in this guide

- **CIDX**: the code-indexer server. It writes an audit log of security
  actions (logins, MFA changes, permission changes, admin actions).
- **UDM (Unified Data Model)**: the common event format of SecOps. Every event
  has fields such as `metadata.event_type`, `principal` (who acted) and
  `target` (what was acted on). See Google's
  [UDM field list](https://docs.cloud.google.com/chronicle/docs/reference/udm-field-list).
- **Parser**: SecOps code that turns a raw log line into UDM.
- **Customer ID**: the ID of your SecOps instance, a GUID such as
  `00000000-0000-0000-0000-000000000000`.
- **Service account**: a Google Cloud identity for a program, not a person.
  CIDX uses one to call the Chronicle API.
- **JSON key**: a file that holds a service account's private key. CIDX signs
  its token requests with it.
- **YARA-L 2.0**: the SecOps language for searches, dashboards and detection
  rules.

## The key fact: no parser is needed

CIDX sends events that are **already in UDM**. It calls the Chronicle API
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
following is an inference, not a Google statement about `events:import`: you
do not need to write a parser, and a custom parser or a parser extension has
no raw log to work on for these events.

Field names in this guide follow the conventions explained at the top of the
[event catalog](event-catalog.md): camelCase in the JSON CIDX sends,
snake_case in Search and rules, and `additional.X` as shorthand for a key of
the `additional` container (matched with `additional.fields["X"]`, string
values only; see the catalog's
[Fields every event carries](event-catalog.md#2-fields-every-event-carries)).

## 1. What CIDX sends

- **Scope.** A fixed set of login, MFA, permission and admin events, listed in
  [CIDX Operations](operations.md#what-is-delivered); every event type is in
  the [event catalog](event-catalog.md#1-summary).
- **Self-reports.** Changes to the CIDX SIEM delivery settings
  (`config_changed`), to its service-account key (`siem_credential_changed`)
  and every SIEM admin action (`siem_*`). They are captured as soon as a
  destination is configured, before delivery is armed, so the first CIDX
  events you see are often `config_changed` and `siem_canary_sent`.
- **Typed values only.** Each event carries fixed attributes (actor, outcome,
  front door, peer address, correlation id, node id, authentication method)
  and the details fields CIDX allowlists for that action type. No free text,
  message, URL or exception text is sent.
- **At least once.** An event can arrive more than once; group by
  `metadata.product_log_id` (the CIDX audit event id) to count unique events.
- **No arrival order.** Sort on `metadata.event_timestamp`, the time the
  action happened in CIDX.
- **Arming.** CIDX captures the scoped events only after a canary batch was
  accepted by SecOps and someone confirmed every canary event is visible in
  SecOps search (section 4). Details are in
  [CIDX Operations](operations.md#arming).

The [event catalog](event-catalog.md#1-summary) maps every CIDX event to its
`metadata.event_type` and outcome field.

## 2. Values for the CIDX administrator

The CIDX administrator needs four values from you, plus the service-account
key (section 3).

| CIDX field | What it is | Where to find it |
|------------|-----------|------------------|
| `region` | The region your SecOps instance was provisioned in, for example `us`. | Ask your Google administrator if unsure. CIDX accepts the regions of Google's [Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide) guide. |
| `project_id` | The Google Cloud project ID linked to your SecOps instance, for example `example-secops-project`. | The SecOps **Profile** page. |
| `location` | The location in the API path. | The same value as `region` (see 2.2). |
| `instance_id` | Your SecOps customer ID. | The SecOps **Profile** page, **Organization Details**, **Customer ID**. |

Also agree on a `source_instance_label` per CIDX server, for example
`cidx-example-1`. It is sent in every event as `additional.cidx_instance` and
tells several CIDX servers apart.

### 2.1 Where to find your SecOps values

Google's
[RBAC page](https://docs.cloud.google.com/chronicle/docs/administration/rbac)
says the **Profile** page in SecOps settings shows your customer ID, Google
Cloud project number and Google Cloud project ID, and that the customer ID is
in the **Organization Details** section. Google's collection guides (for
example [this one](https://docs.cloud.google.com/chronicle/docs/ingestion/default-parsers/wiz-io))
give the path as **SIEM Settings > Profile**. The menu label can differ by
console version; if you cannot find it, check with your Google administrator.

### 2.2 Region, location and the endpoint

CIDX has no URL field. It builds the request URL from the values:

```text
https://chronicle.<region>.rep.googleapis.com/<api_version>/projects/<project_id>/locations/<location>/instances/<instance_id>/events:import
```

For example:

```text
https://chronicle.us.rep.googleapis.com/v1/projects/example-secops-project/locations/us/instances/00000000-0000-0000-0000-000000000000/events:import
```

This shape matches Google's
[Migrate to Chronicle API](https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide)
guide. That guide defines `location` as "The location of your project
(region); same as the regional endpoints", and its own example uses
`chronicle.us.rep.googleapis.com` with `locations/us`. CIDX does not check
that region and location match, so give the same value for both.

Changing any of the four values makes a new destination: CIDX stops capturing
until the canary is run and confirmed again.

## 3. Create the Google service account

### 3.1 Which permission it needs

The `events:import` reference says the call needs the IAM permission
`chronicle.events.import` on the parent resource, and one of the OAuth scopes
`https://www.googleapis.com/auth/cloud-platform` or
`https://www.googleapis.com/auth/chronicle`. CIDX requests the `chronicle`
scope.

Google's
[Chronicle roles reference](https://docs.cloud.google.com/iam/docs/roles-permissions/chronicle)
lists these predefined roles as containing `chronicle.events.import`:
Owner (`roles/owner`), Editor (`roles/editor`), Chronicle API Admin
(`roles/chronicle.admin`), Chronicle API Editor (`roles/chronicle.editor`),
Admin (`roles/admin`) and Writer (`roles/writer`). All of them grant far more
than importing events.

For least privilege, create a **custom role** that contains only
`chronicle.events.import`. Google's
[custom role support table](https://docs.cloud.google.com/iam/docs/custom-roles-permissions-support)
marks `chronicle.events.import` as `SUPPORTED`.

### 3.2 Step by step

Do these steps in the Google Cloud project linked to your SecOps instance.

1. **Check that the Chronicle API is enabled** in the project. Google's
   [Configure a Google Cloud project](https://docs.cloud.google.com/chronicle/docs/onboard/configure-cloud-project)
   page describes enabling it under **APIs & Services**.
2. **Create the custom role** (Google:
   [Create and manage custom roles](https://docs.cloud.google.com/iam/docs/creating-custom-roles)):
   1. In the Google Cloud console, go to the **Roles** page.
   2. Select the project at the top of the page.
   3. Click **Create custom role**.
   4. Enter a title (for example `CIDX SIEM event import`), a description and
      an ID.
   5. Click **Add Permissions**, select `chronicle.events.import`, click
      **Add Permissions**, then create the role.
3. **Create the service account** (Google:
   [Create service accounts](https://docs.cloud.google.com/iam/docs/service-accounts-create)):
   1. Go to the **Create service account** page and select the project.
   2. Enter a name, for example `cidx-siem-import`. The console suggests an
      ID; it cannot be changed later.
   3. Click **Create and continue**.
   4. In the role step, choose the custom role from step 2. Click
      **Continue**, then **Done**.
4. **Or grant the role afterwards** (Google:
   [Manage access](https://docs.cloud.google.com/iam/docs/granting-changing-revoking-access)):
   go to the **IAM** page, select the project, click **Grant Access**, enter
   the service account's email, select the custom role, save.
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
   check with your Google administrator.

### 3.3 Hand the key to the CIDX administrator

The CIDX administrator pastes or uploads the key in the CIDX Web UI (see
[CIDX Operations](operations.md#service-account-credential), which lists
everything CIDX checks). CIDX stores it encrypted and never shows it again;
only the service account's email and key id are displayed. Hand the file over
through a channel your organization approves for secrets, and delete every
copy once CIDX has accepted it.

**Check `token_uri` first.** CIDX accepts only
`"token_uri": "https://oauth2.googleapis.com/token"` and otherwise refuses
the key with `token_uri is not the allowed token endpoint`. On Google's
[key creation page](https://docs.cloud.google.com/iam/docs/keys-create-delete),
the Console example key shows
`"token_uri": "https://accounts.google.com/o/oauth2/token"`, while the gcloud
example shows `"token_uri": "https://oauth2.googleapis.com/token"`. Whether
current key files ever carry the older value was not verified; report it to
the CIDX maintainers if you see it. CIDX also requires an RSA key.

### 3.4 Rotate the key

This follows Google's
[key rotation](https://docs.cloud.google.com/iam/docs/key-rotation) steps.

1. Create a new JSON key for the same service account (step 3.2.5).
2. The CIDX administrator replaces the key in CIDX. CIDX records
   `siem_credential_changed` with `change` = `replaced`. Replacing the key
   stops capture of new events until the canary is run and confirmed again;
   events captured before keep being delivered with the new key.
3. The CIDX administrator runs the canary and you confirm it (section 4).
4. Disable the old key in the Google Cloud console, and watch SecOps for new
   CIDX events.
5. When delivery works, delete the old key (**Service accounts**, the
   account, **Keys** tab).

Plan the rotation for a quiet period: between steps 2 and 3, CIDX captures no
new events for SecOps.

### 3.5 Security advice

From Google's
[best practices for managing service account keys](https://docs.cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys):

- Grant only `chronicle.events.import` (the custom role above).
- Don't leave keys in temporary locations, don't pass them between users,
  don't commit them to source code repositories.
- Rotate keys routinely.
- Use a dedicated service account for each CIDX deployment, so a
  `siem_credential_changed` event and the Google IAM audit trail point to one
  system.

## 4. Verify the canary

Before CIDX captures anything beyond self-reports, its administrator sends a
synthetic **canary** batch: one event per UDM mapping entry plus one
deliberately unmapped event (34 events with the current mapping). Your job is
to find every canary event and tell the CIDX administrator which ones are
visible. CIDX arms only when every expected id is confirmed.

Each canary event:

- has `metadata.vendor_name` = `CIDX` and `metadata.product_name` =
  `cidx-server`;
- has `metadata.product_log_id` = one of the ids the CIDX administrator gives
  you;
- has `principal.application` = `cidx-server` and `principal.ip` =
  `192.0.2.10` (a documentation-only address);
- uses sample values such as `example-user`, `example-repo-global` and
  `example-id`;
- has `additional.cidx_canary` = true and `additional.correlation_id` =
  `canary-<canary_run_id>`.

Open **Investigation > SIEM Search** (Google:
[Understand search](https://docs.cloud.google.com/chronicle/docs/investigation/udm-search)),
set the time range to cover the canary, and search one id:

```text
metadata.vendor_name = "CIDX" AND metadata.product_log_id = "00000000-0000-4000-8000-000000000001"
```

Or the whole run:

```text
metadata.vendor_name = "CIDX" AND additional.fields["correlation_id"] = "canary-6c1d2e3f-0000-4000-8000-000000000001"
```

If the run-wide search returns nothing on your tenant, search by
`metadata.product_log_id`.

Canary events are synthetic. Exclude them from detections by
`metadata.product_log_id`, or by ignoring events where
`principal.ip = "192.0.2.10"` and `principal.application = "cidx-server"`.

## 5. Searching and alerting

Open **Investigation > SIEM Search**, set the **Time Range**, type the query,
click **Run Search** (Google:
[Understand search](https://docs.cloud.google.com/chronicle/docs/investigation/udm-search)).
The examples join conditions with `AND`, as Google's
[search syntax reference](https://docs.cloud.google.com/chronicle/docs/investigation/search-syntax-reference)
documents. That reference says string comparisons in YARA-L 2.0 are
case-sensitive by default and that `nocase` makes them case-insensitive.
Google's rules editor page adds that the Search web interface sets its case
sensitivity option to Off by default, so add `nocase` to rule conditions
copied from Search. CIDX values are lowercase except `CIDX` and the UDM enum
values (`USER_LOGIN`, `BLOCK`), so type them exactly as shown.

### 5.1 Search examples

All CIDX events:

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

Events of one CIDX server:

```text
metadata.vendor_name = "CIDX" AND additional.fields["cidx_instance"] = "cidx-example-1"
```

Adapt these to your environment. The regular expression form `/.../` follows
Google's
[rule examples](https://docs.cloud.google.com/chronicle/docs/yara-l/yara-l-2-0-examples).

### 5.2 Detection rules (YARA-L 2.0)

Create rules in **Detections > Rules & detections**, **Rules editor** tab,
**New** (Google:
[Edit rules in the Rules Editor](https://docs.cloud.google.com/chronicle/docs/detection/manage-all-rules)).
The structure (`meta`, `events`, `match`, `condition`, `#e` counts, `over`
windows) follows Google's
[Get started with YARA-L](https://docs.cloud.google.com/chronicle/docs/detection/yara-l-2-0-syntax)
and
[rule examples](https://docs.cloud.google.com/chronicle/docs/yara-l/yara-l-2-0-examples).
Test every rule in the rules editor and adapt it to your environment before
relying on it.

Repeated failed logins for one account:

```text
rule cidx_repeated_failed_logins {
  meta:
    author = "example-secops-team"
    description = "Five or more failed CIDX logins for one account in 10 minutes"
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
does not count them. Do not group by `principal.ip` until you have checked
what it holds in your deployment (section 7).

A role change (including promotion to admin):

```text
rule cidx_user_role_changed {
  meta:
    author = "example-secops-team"
    description = "A CIDX user's role changed"
    severity = "High"
  events:
    $e.metadata.vendor_name = "CIDX"
    $e.metadata.product_event_type = "user_role_changed"
  condition:
    $e
}
```

The new role is in `additional.cidx_details.new_role`; read it in the alert's
event view (see the catalog's
[Fields every event carries](event-catalog.md#2-fields-every-event-carries)).

Changes to SIEM delivery itself:

```text
rule cidx_siem_delivery_changed {
  meta:
    author = "example-secops-team"
    description = "CIDX SIEM delivery settings, key or queue were changed"
    severity = "High"
  events:
    $e.metadata.vendor_name = "CIDX"
    $e.metadata.product_event_type = /^(config_changed|siem_credential_changed|siem_destination_abandoned|siem_destination_retargeted|siem_delivery_resumed|siem_batch_acknowledged|siem_batch_rebatched)$/
  condition:
    $e
}
```

Also consider a rule or dashboard for silence: if CIDX normally sends logins
every hour, an hour with none can mean delivery stopped.

Dashboards: the same filters work as dashboard queries. Count by
`metadata.product_event_type` and by `additional.fields["cidx_instance"]` for
an overview.

## 6. Troubleshooting from the SecOps side

When CIDX events stop or never arrive, first ask the CIDX administrator for
the SIEM health reasons and the halt class (see
[CIDX Operations](operations.md#monitoring)). Then check:

1. **The service account** exists, is enabled, and its key was not disabled
   or deleted. A CIDX `credential` halt (SecOps answered HTTP 401 or 403), or
   a token probe result of `token_rejected`, points here.
2. **The role grant.** The account holds a role with
   `chronicle.events.import` on the SecOps project (section 3).
3. **The Chronicle API** is enabled in that project.
4. **The destination values.** Region, project, location and customer ID
   match your instance. A CIDX `request_rejection` halt with HTTP 404 in its
   signature can point here.
5. **Quota.** Google's
   [quotas and burst limits](https://docs.cloud.google.com/chronicle/docs/ingestion/burst-limits)
   page says push ingestion over the burst limit is rejected with HTTP 429.
   CIDX keeps such events queued and retries after the `Retry-After` delay.
6. **Ingestion monitoring.** Google's
   [Check data ingestion health](https://docs.cloud.google.com/chronicle/docs/ingestion/ingestion-overview)
   page describes the Health Hub and the Data Ingestion dashboard, which
   refresh about every 15 minutes. Whether events sent with `events:import`
   appear there, and under which log type, was not verified. Google's
   [Ingestion API](https://docs.cloud.google.com/chronicle/docs/reference/ingestion-api)
   page says events sent through its `udmevents` method get
   `metadata.log_type = "UDM"`; whether `events:import` does the same was not
   verified. Searching `metadata.vendor_name = "CIDX"` works regardless.
7. **A recent CIDX change.** A key replacement, a trusted-CA change, a
   disable or a destination change stops capture until a fresh canary is
   confirmed.

Google's `events:import` reference says an error in one event rejects the
whole request. CIDX then sets the offending events aside (quarantine) and
keeps sending the rest; the CIDX administrator can list and requeue them.

## 7. Known limitations

- **`principal.ip` may be the reverse proxy's address.** CIDX records the
  peer address of the HTTP connection as the server sees it. Behind a reverse
  proxy that can be the proxy, not the client. Check what it holds in your
  deployment before using it for client attribution, geo lookups or IP-based
  rules.
- **Scope.** Only the events in section 1 are sent. Refused-access events
  (HTTP 403 on normal requests) and other CIDX audit events (for example
  configuration changes outside the SIEM section, or repository management)
  are not sent.
- **Mapping version.** The set of UDM mappings has a version, currently 3. It
  is not in each event; `additional.schema_version` (currently 1) is the
  version of the CIDX payload, a different thing. When a CIDX upgrade brings a
  new mapping version, delivery stays armed, CIDX health asks for the canary
  to be run again, and quarantined events are requeued once automatically
  (with a `siem_quarantine_requeued` event, `trigger` =
  `mapping_version_change`, only when at least one event was requeued). A new
  mapping can change the `metadata.event_type` of a CIDX event; rules that
  match on `metadata.product_event_type` (the CIDX name) do not depend on
  that.
- **Departures from Google's UDM usage guide.** Google's
  [UDM usage guide](https://docs.cloud.google.com/chronicle/docs/unified-data-model/udm-usage)
  asks for the following, and CIDX departs from it:
  - `security_result.category` = `AUTH_VIOLATION` on failed `USER_LOGIN`
    events; CIDX sends only `security_result.action`.
  - An authentication extension on `USER_LOGIN`; CIDX sends none on
    `elevation_granted` and `elevation_failed`, and only the type (not the
    mechanism) on logins.
  - `network` and `network.http` details for logins over HTTP; CIDX sends
    none.
  - A target resource of type `SETTING` for `SETTING_MODIFICATION`; CIDX uses
    type `config` in `target.resource.type`, which the guide calls deprecated
    in favour of `resource_type`.
  - A principal machine identifier for `STATUS_*` and `SETTING_*` events;
    CIDX system self-reports carry only `principal.application`.

  The canary does not prove SecOps accepts every shape. It sends only
  successful events from a system actor that has an IP: no `BLOCK` results,
  no user principals and no system events without an IP (such as an automatic
  `siem_quarantine_requeued`).
- **Items to confirm on your tenant.** The CIDX
  [real-tenant checklist](operations.md#real-tenant-enablement-checklist)
  lists them, including the exact rejection format, the duplicate response
  (Google's `events:import` reference documents no HTTP 409; CIDX treats one
  as a possible duplicate and halts for an admin decision) and the IAM
  permission name.
