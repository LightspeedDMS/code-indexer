# cidx Event Catalog for Google SecOps

This catalog lists every cidx audit event that SIEM delivery sends to Google
Security Operations (SecOps), with the UDM fields each one fills and an
example. It is the reference part of the
[Google SecOps Guide for cidx Audit Events](secops-guide.md); read that
guide first for the terms used here (UDM, principal, target), for
configuration, and for searching and alerting.

Every statement about cidx here was checked against the cidx source code.

How to read field names in this catalog:

- The JSON examples show what cidx sends, in camelCase (`productLogId`,
  `securityResult`). SecOps Search and rules use snake_case for the same
  fields (`metadata.product_log_id`, `security_result.action`).
- `additional.X` (for example `additional.cidx_instance`) is shorthand for
  the key `X` inside the `additional` container. In Search, type
  `additional.fields["cidx_instance"] = "cidx-example-1"`. In a rule, follow
  Google's documented form
  `$e.udm.additional.fields["cidx_instance"] = "cidx-example-1"`. Only keys
  with string values work this way (see section 2).

## Contents

- [1. Summary](#1-summary)
- [2. Fields every event carries](#2-fields-every-event-carries)
- [3. Logins](#3-logins): [authentication_success](#authentication_success),
  [authentication_failure](#authentication_failure),
  [elevation_granted](#elevation_granted),
  [elevation_failed](#elevation_failed)
- [4. MFA changes](#4-mfa-changes): [mfa_activated](#mfa_activated),
  [mfa_disabled](#mfa_disabled),
  [mfa_recovery_codes_regenerated](#mfa_recovery_codes_regenerated),
  [mfa_secret_regenerated_cross_user](#mfa_secret_regenerated_cross_user)
- [5. Group and permission changes](#5-group-and-permission-changes):
  [user_role_changed](#user_role_changed),
  [user_group_assign](#user_group_assign),
  [user_group_change](#user_group_change),
  [repo_access_grant](#repo_access_grant),
  [repo_access_revoke](#repo_access_revoke)
- [6. Admin actions](#6-admin-actions): [user_created](#user_created),
  [user_deleted](#user_deleted),
  [user_password_reset_by_admin](#user_password_reset_by_admin),
  [mcp_credential_created](#mcp_credential_created),
  [mcp_credential_revoked](#mcp_credential_revoked),
  [api_key_created](#api_key_created),
  [ssh_key_host_assigned](#ssh_key_host_assigned),
  [impersonation_set](#impersonation_set),
  [impersonation_cleared](#impersonation_cleared),
  [impersonation_denied](#impersonation_denied)
- [7. SIEM delivery self-reports](#7-siem-delivery-self-reports):
  [config_changed](#config_changed),
  [siem_credential_changed](#siem_credential_changed),
  [siem_canary_sent](#siem_canary_sent),
  [siem_canary_visibility_confirmed](#siem_canary_visibility_confirmed),
  [siem_quarantine_requeued](#siem_quarantine_requeued),
  [siem_delivery_resumed](#siem_delivery_resumed),
  [siem_batch_acknowledged](#siem_batch_acknowledged),
  [siem_batch_rebatched](#siem_batch_rebatched),
  [siem_destination_retargeted](#siem_destination_retargeted),
  [siem_destination_abandoned](#siem_destination_abandoned)
- [8. Unmapped events (fallback)](#8-unmapped-events-fallback)

## 1. Summary

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
`_denied`.

Failed attempts: the MFA and admin actions marked "BLOCK on a failed
attempt" also write a row when the action fails (`outcome` `failure`,
action `BLOCK`). cidx records only what it verified, so a failure row
usually has no `cidx_details`, and for user, credential and key actions
usually no `target`. (MFA failure rows keep the account as target.) A
failed admin action is worth an alert. Example, a failed `user_created`:

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_CREATION", "productEventType": "user_created", "productLogId": "00000000-0000-4000-8000-000000000037", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "securityResult": [{"action": ["BLOCK"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "failure", "schema_version": 1, "source": "web"}
}
```

`STATUS_UPDATE` is described in Google's UDM list as "A software or
fingerprint update". cidx uses it for its SIEM self-reports; treat it as
"SIEM pipeline status" for cidx events.

## 2. Fields every event carries

| UDM field | Value |
|-----------|-------|
| `metadata.event_timestamp` | When the action happened in cidx, UTC, RFC 3339. |
| `metadata.event_type` | From the table in section 1. |
| `metadata.vendor_name` | `CIDX` |
| `metadata.product_name` | `cidx-server` |
| `metadata.product_event_type` | The cidx action type, for example `user_role_changed`. |
| `metadata.product_log_id` | The cidx audit event UUID. Unique per audit event; use it to remove duplicates. |
| `principal.user.userid` | The cidx username that acted. Absent for system actions and for failed logins with an unknown account name. |
| `principal.application` | `cidx-server` for system actions (and when nothing else identifies the actor). |
| `principal.ip` | Peer address of the HTTP request. **Currently the reverse proxy address, not the client** (see [Known limitations](secops-guide.md#8-known-limitations)). Absent for system actions. |
| `target` | Depends on the event: `target.user.userid` for user and login targets; `target.resource.name` plus `target.resource.type` for repositories, keys, credentials and settings. |
| `security_result.action` | `ALLOW` or `BLOCK`, see section 1. |
| `extensions.auth.type` | Only on `authentication_success` and `authentication_failure`: `AUTHTYPE_UNSPECIFIED` (password), `SSO` (sso), `MACHINE` (api_key or mcp_credential). |
| `additional.cidx_instance` | Your `source_instance_label`. |
| `additional.schema_version` | Version of the cidx event payload (currently 1). |
| `additional.outcome` | cidx outcome: `success`, `failure`, `denied` or `attempted`, when recorded. |
| `additional.source` | Front door: `rest`, `mcp`, `web` or `system`. |
| `additional.auth_method` | How the caller was authenticated: `jwt`, `oauth_token`, `mcp_credential`, `web_session`, `none` (before login) or `system`. |
| `additional.actor_is_system` | true for cidx's own components. |
| `additional.correlation_id` | cidx request correlation id. Events from one request share it. |
| `additional.node_id` | cidx cluster node id (absent on a single-node server). |
| `additional.cidx_details` | The allowlisted details of the action (listed per event below). |
| `additional.principal_unknown` | true when cidx had no user and no IP for the actor. |
| `additional.cidx_canary` | true on canary events only. |

`additional` is a Google `Struct`. Google's
[search syntax reference](https://docs.cloud.google.com/chronicle/docs/investigation/search-syntax-reference)
says `additional.fields["key"] = "value"` works for string values only, and
its [YARA-L map syntax](https://docs.cloud.google.com/chronicle/docs/yara-l/expressions)
page says the same for rules. So `cidx_instance`, `outcome`, `source`,
`auth_method`, `correlation_id` and `node_id` can be matched by key. For
booleans and numbers (`actor_is_system`, `cidx_canary`, `principal_unknown`,
`schema_version`) the search syntax reference documents
`additional.fields.value.bool_value = true` and
`additional.fields.value.number_value > 500`; these match the value under
any key, not a named one. Searching inside the nested `cidx_details` object
is not documented; read it in the event viewer.

The examples below were produced by running the cidx mapping code on sample
events. They use the camelCase JSON form that cidx sends. Every value is a
neutral sample.

## 3. Logins

### authentication_success

A login succeeded on the REST, MCP or Web door. `cidx_details`: `method`
(`password`, `api_key`, `sso`, `mcp_credential`), `mfa` (`not_enrolled`,
`totp`, `recovery_code`, `not_applicable`, `not_checked_password_expired`,
`not_checked_sso_oauth`), `flow` (`rest_token`, `web_session`, `oauth_code`,
`mcp_jwt`). The principal and the target are the same user.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_LOGIN", "productEventType": "authentication_success", "productLogId": "00000000-0000-4000-8000-000000000001", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "extensions": {"auth": {"type": "AUTHTYPE_UNSPECIFIED"}},
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"flow": "rest_token", "method": "password", "mfa": "totp"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### authentication_failure

A login was refused. `cidx_details`: `method`, `stage` (`credentials`,
`mfa_code`, `challenge`, `issuance`), `reason` (`bad_credentials`,
`mfa_code_invalid`, `challenge_invalid_or_expired`, `account_locked`,
`rate_limited`, `password_expired`, `server_error`). `account_locked` is
legacy and no longer emitted (logins use a progressive throttle with no
lockout); `rate_limited` marks the failed attempt that
starts the throttle, and attempts refused while it runs (HTTP 429) write no
event. The typed username is
recorded only when it names an existing account. Otherwise cidx records
`(unknown)` and sends no `principal.user` and no `target` (so a password typed
into the username box never reaches SecOps).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_LOGIN", "productEventType": "authentication_failure", "productLogId": "00000000-0000-4000-8000-000000000002", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["BLOCK"]}],
  "extensions": {"auth": {"type": "AUTHTYPE_UNSPECIFIED"}},
  "additional": {"actor_is_system": false, "auth_method": "none", "cidx_details": {"method": "password", "reason": "bad_credentials", "stage": "credentials"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "failure", "schema_version": 1, "source": "web"}
}
```

Unknown account name (note: no user, no target):

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_LOGIN", "productEventType": "authentication_failure", "productLogId": "00000000-0000-4000-8000-000000000003", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"]},
  "securityResult": [{"action": ["BLOCK"]}],
  "extensions": {"auth": {"type": "AUTHTYPE_UNSPECIFIED"}},
  "additional": {"actor_is_system": false, "auth_method": "none", "cidx_details": {"method": "password", "reason": "bad_credentials", "stage": "credentials"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "failure", "schema_version": 1, "source": "mcp"}
}
```

### elevation_granted

A user passed TOTP step-up (elevation) for sensitive admin actions.
`cidx_details`: `scope` (`full`, `totp_repair`), `used_recovery_code`. No
`extensions.auth`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_LOGIN", "productEventType": "elevation_granted", "productLogId": "00000000-0000-4000-8000-000000000004", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-admin"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"scope": "full", "used_recovery_code": false}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### elevation_failed

A step-up attempt failed. Same fields as `elevation_granted`; action `BLOCK`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_LOGIN", "productEventType": "elevation_failed", "productLogId": "00000000-0000-4000-8000-000000000005", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-admin"}},
  "securityResult": [{"action": ["BLOCK"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"scope": "full", "used_recovery_code": false}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "failure", "schema_version": 1, "source": "web"}
}
```

## 4. MFA changes

All four use `USER_UNCATEGORIZED`, `target.user.userid` = the account whose
MFA changed, `principal.user.userid` = who did it.

### mfa_activated

A user turned on TOTP MFA. `cidx_details`: `recovery_codes_issued`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mfa_activated", "productLogId": "00000000-0000-4000-8000-000000000006", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"recovery_codes_issued": true}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### mfa_disabled

MFA was turned off. `cidx_details`: `method` (`totp` or `recovery_code`).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mfa_disabled", "productLogId": "00000000-0000-4000-8000-000000000007", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"method": "totp"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### mfa_recovery_codes_regenerated

New recovery codes were issued. `cidx_details`: `count`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mfa_recovery_codes_regenerated", "productLogId": "00000000-0000-4000-8000-000000000008", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"count": 10}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### mfa_secret_regenerated_cross_user

One user (an admin) regenerated another user's MFA secret. No
`cidx_details`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mfa_secret_regenerated_cross_user", "productLogId": "00000000-0000-4000-8000-000000000009", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

## 5. Group and permission changes

### user_role_changed

A user's cidx role changed. `cidx_details`: `old_role`, `new_role` (`admin`,
`power_user`, `normal_user`).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_CHANGE_PERMISSIONS", "productEventType": "user_role_changed", "productLogId": "00000000-0000-4000-8000-000000000010", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"new_role": "admin", "old_role": "normal_user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### user_group_assign

A user was put in a group (for example automatically when the account is
created). `cidx_details` may hold `user_id`, `group`, `from_group`,
`to_group`, `old_group`, `new_group`, `removed_from_group`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "GROUP_MODIFICATION", "productEventType": "user_group_assign", "productLogId": "00000000-0000-4000-8000-000000000011", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"group": "users"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "web"}
}
```

### user_group_change

A user moved between groups. Same `cidx_details` keys as `user_group_assign`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "GROUP_MODIFICATION", "productEventType": "user_group_change", "productLogId": "00000000-0000-4000-8000-000000000012", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"from_group": "users", "to_group": "admins", "user_id": "example-user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "mcp"}
}
```

### repo_access_grant

A group got access to a repository. `target.resource` = the repository
alias, type `repo`. `cidx_details`: `repo`, `group`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_RESOURCE_UPDATE_PERMISSIONS", "productEventType": "repo_access_grant", "productLogId": "00000000-0000-4000-8000-000000000013", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "example-repo-global", "type": "repo"}},
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"group": "users", "repo": "example-repo-global"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "web"}
}
```

### repo_access_revoke

A group lost access to a repository. `cidx_details`: `repo`, `group`, and on
a bulk revoke summary `not_in_group_count`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_RESOURCE_UPDATE_PERMISSIONS", "productEventType": "repo_access_revoke", "productLogId": "00000000-0000-4000-8000-000000000014", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "example-repo-global", "type": "repo"}},
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"group": "users", "repo": "example-repo-global"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "web"}
}
```

## 6. Admin actions

### user_created

An account was created. `cidx_details`: `role`, `provisioning` (`admin`,
`self_registration`, `sso`).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_CREATION", "productEventType": "user_created", "productLogId": "00000000-0000-4000-8000-000000000015", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"provisioning": "admin", "role": "normal_user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

Accounts created by self-registration or SSO provisioning are system actions:
`principal.application` = `cidx-server` instead of a user.

### user_deleted

An account was deleted. `cidx_details`: `deleted_role`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_DELETION", "productEventType": "user_deleted", "productLogId": "00000000-0000-4000-8000-000000000016", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"deleted_role": "normal_user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### user_password_reset_by_admin

An admin reset another user's password. No `cidx_details`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_CHANGE_PASSWORD", "productEventType": "user_password_reset_by_admin", "productLogId": "00000000-0000-4000-8000-000000000017", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### mcp_credential_created

An MCP client credential was created. `target.resource` type
`mcp_credential`. `cidx_details`: `credential_id`, `for_self` (false means
an admin created it for someone else).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mcp_credential_created", "productLogId": "00000000-0000-4000-8000-000000000018", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "example-credential-id", "type": "mcp_credential"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"credential_id": "example-credential-id", "for_self": true}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### mcp_credential_revoked

An MCP client credential was revoked. Same fields as above.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "mcp_credential_revoked", "productLogId": "00000000-0000-4000-8000-000000000019", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "example-credential-id", "type": "mcp_credential"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"credential_id": "example-credential-id", "for_self": false}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### api_key_created

A cidx API key was created. `target.resource` type `api_key`.
`cidx_details`: `key_id` (an id, never the key).

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "api_key_created", "productLogId": "00000000-0000-4000-8000-000000000020", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "example-key-id", "type": "api_key"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"key_id": "example-key-id"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### ssh_key_host_assigned

A server SSH key was assigned to a git host. `target.resource` type
`ssh_key`; its name is the key's SHA-256 fingerprint as `sha256:<hex>`,
never the key's own name. `cidx_details`: `host`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "ssh_key_host_assigned", "productLogId": "00000000-0000-4000-8000-000000000021", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "type": "ssh_key"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"host": "git.example.com"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### impersonation_set

An admin started acting as another user. `target.user.userid` = the
impersonated user. `cidx_details`: `actor_username`, `target_username`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "impersonation_set", "productLogId": "00000000-0000-4000-8000-000000000022", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"actor_username": "example-admin", "target_username": "example-user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "mcp"}
}
```

### impersonation_cleared

Impersonation ended. `cidx_details`: `actor_username`, `previous_target`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "impersonation_cleared", "productLogId": "00000000-0000-4000-8000-000000000023", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"user": {"userid": "example-user"}},
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"actor_username": "example-admin", "previous_target": "example-user"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "schema_version": 1, "source": "mcp"}
}
```

### impersonation_denied

A user tried to impersonate someone and was refused. Action `BLOCK`.
`target.user.userid` = the user they tried to impersonate.
`cidx_details`: `actor_username`, `target_username`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "USER_UNCATEGORIZED", "productEventType": "impersonation_denied", "productLogId": "00000000-0000-4000-8000-000000000024", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-user"}},
  "target": {"user": {"userid": "example-admin"}},
  "securityResult": [{"action": ["BLOCK"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"actor_username": "example-user", "target_username": "example-admin"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "denied", "schema_version": 1, "source": "mcp"}
}
```

## 7. SIEM delivery self-reports

All of these have `target.resource.name` = `siem_delivery`, type `config`.

### config_changed

The SIEM delivery settings were saved, or a save of them failed. Only
changes that touch the `siem_delivery` section are sent. `cidx_details`:
`change_kind` (`update`, `reset_to_defaults`), `changed_keys` (on success) or
`attempted_keys` (on failure), and `values` (before/after pairs for
`enabled`, `api_version`, `max_batch_events`, `region`,
`trusted_ca_fingerprint` only). A failed save has action `BLOCK`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "SETTING_MODIFICATION", "productEventType": "config_changed", "productLogId": "00000000-0000-4000-8000-000000000025", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"change_kind": "update", "changed_keys": ["siem_delivery_config.enabled"], "values": {"siem_delivery_config.enabled": [false, true]}}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

Watch for `siem_delivery_config.enabled` going from true to false: someone
turned audit delivery off.

The full-send form: a reset of all settings to defaults, or one save that
updates several settings sections, is recorded with target `*`. If it
touches any `siem_delivery_config.*` key, the whole row is sent:
`target.resource.name` is `*`, `changed_keys` lists keys from every section
(up to 200), and `values` also holds the before/after pairs of allowlisted
non-SIEM settings such as `workers` and `log_level`. A reset that changes no
SIEM key is not sent.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "SETTING_MODIFICATION", "productEventType": "config_changed", "productLogId": "00000000-0000-4000-8000-000000000036", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "*", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"change_kind": "reset_to_defaults", "changed_keys": ["log_level", "siem_delivery_config.enabled", "siem_delivery_config.region", "workers"], "values": {"log_level": ["DEBUG", "INFO"], "siem_delivery_config.enabled": [true, false], "siem_delivery_config.region": ["us", ""], "workers": [4, 1]}}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### siem_credential_changed

The service-account key was set, replaced or removed. `cidx_details`:
`change`, `client_email`, `private_key_id`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "SETTING_MODIFICATION", "productEventType": "siem_credential_changed", "productLogId": "00000000-0000-4000-8000-000000000026", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "web_session", "cidx_details": {"change": "replaced", "client_email": "secops-sa@example.com", "private_key_id": "0123456789abcdef0123456789abcdef01234567"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "web"}
}
```

### siem_canary_sent

An admin sent the canary. `cidx_details`: `canary_run_id`, `event_count`,
`result` (`accepted`, `rejected`), `mapping_version`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_canary_sent", "productLogId": "00000000-0000-4000-8000-000000000027", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"canary_run_id": "6c1d2e3f-0000-4000-8000-000000000001", "event_count": 34, "mapping_version": 2, "result": "accepted"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_canary_visibility_confirmed

An admin submitted the canary visibility list. `cidx_details`:
`canary_run_id`, `expected_count`, `confirmed_count`, `missing_action_types`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_canary_visibility_confirmed", "productLogId": "00000000-0000-4000-8000-000000000028", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"canary_run_id": "6c1d2e3f-0000-4000-8000-000000000001", "confirmed_count": 34, "expected_count": 34, "missing_action_types": []}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_quarantine_requeued

Quarantined events were put back in the queue, by an admin or automatically
after a cidx upgrade with a new mapping. `cidx_details`: `count`,
`mapping_version`, `trigger` (`admin`, `mapping_version_change`). The
automatic form is a system action:

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_quarantine_requeued", "productLogId": "00000000-0000-4000-8000-000000000029", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"application": "cidx-server"},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": true, "auth_method": "system", "cidx_details": {"count": 2, "mapping_version": 2, "trigger": "mapping_version_change"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "system"}
}
```

### siem_delivery_resumed

An admin cleared a delivery halt. `cidx_details`: `halted_class`
(`local_validation_burst`, `row_rejection_burst`, `request_rejection`,
`credential`, `duplicate_response`, `unclassified`), `signature_reset`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_delivery_resumed", "productLogId": "00000000-0000-4000-8000-000000000030", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"halted_class": "credential", "signature_reset": true}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_batch_acknowledged

After a duplicate halt, an admin declared a batch's events present in SecOps
and marked them delivered. `cidx_details`: `batch_id`, `event_count`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_batch_acknowledged", "productLogId": "00000000-0000-4000-8000-000000000031", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"batch_id": "b1a2c3d4-0000-4000-8000-000000000001", "event_count": 120}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_batch_rebatched

After a duplicate halt, an admin chose to send a batch's events again
(duplicates possible). Same fields as above.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_batch_rebatched", "productLogId": "00000000-0000-4000-8000-000000000032", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"batch_id": "b1a2c3d4-0000-4000-8000-000000000001", "event_count": 120}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_destination_retargeted

Events queued for an old destination were moved to the current one.
`cidx_details`: `from_destination_key`, `count`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_destination_retargeted", "productLogId": "00000000-0000-4000-8000-000000000033", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"count": 42, "from_destination_key": "gsecops:0123456789abcdef"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

### siem_destination_abandoned

Undelivered events of an old destination were dropped on purpose. They will
never reach SecOps. `cidx_details`: `destination_key`, `count`.

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "STATUS_UPDATE", "productEventType": "siem_destination_abandoned", "productLogId": "00000000-0000-4000-8000-000000000034", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"ip": ["198.51.100.7"], "user": {"userid": "example-admin"}},
  "target": {"resource": {"name": "siem_delivery", "type": "config"}},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": false, "auth_method": "jwt", "cidx_details": {"count": 42, "destination_key": "gsecops:0123456789abcdef"}, "cidx_instance": "cidx-example-1", "correlation_id": "5f0c2a9e-0000-4000-8000-00000000c0de", "node_id": "example-node-1", "outcome": "success", "schema_version": 1, "source": "rest"}
}
```

## 8. Unmapped events (fallback)

If cidx ever delivers an action type that has no UDM mapping, it does not
drop it. It sends it as `metadata.event_type` = `GENERIC_EVENT`, with the
cidx type in `metadata.product_event_type`, and without `target` and without
`cidx_details`. cidx also logs one WARNING per type and counts it.

How to recognise one: `metadata.vendor_name = "CIDX"` and
`metadata.event_type = "GENERIC_EVENT"`. In normal operation the only one is
the canary's `siem_canary_unmapped`. This is what the canary code produces
(sample address `192.0.2.10`, correlation id `canary-<canary_run_id>`,
`cidx_canary` true, no `node_id`):

```json
{
  "metadata": {"eventTimestamp": "2026-10-01T14:03:22.512345Z", "eventType": "GENERIC_EVENT", "productEventType": "siem_canary_unmapped", "productLogId": "00000000-0000-4000-8000-000000000035", "productName": "cidx-server", "vendorName": "CIDX"},
  "principal": {"application": "cidx-server", "ip": ["192.0.2.10"]},
  "securityResult": [{"action": ["ALLOW"]}],
  "additional": {"actor_is_system": true, "auth_method": "system", "cidx_canary": true, "cidx_instance": "cidx-example-1", "correlation_id": "canary-6c1d2e3f-0000-4000-8000-000000000001", "outcome": "success", "schema_version": 1, "source": "system"}
}
```

Any other `GENERIC_EVENT` from cidx means a mapping gap; tell the cidx
operator.
