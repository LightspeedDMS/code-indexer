# OIDC Single Sign-On

How to connect a CIDX Server to an OpenID Connect (OIDC) identity provider so users sign in with SSO, and how SSO
sign-ins map to CIDX accounts and groups.

Code: `src/code_indexer/server/auth/oidc/` (provider, manager, callback route), `/login/sso` in
`src/code_indexer/server/web/routes.py`, group assignment in
`src/code_indexer/server/services/sso_provisioning_hook.py`.

## Prerequisites

- A running CIDX Server reachable by users at a stable URL, normally HTTPS behind a reverse proxy
  ([Deployment](../deployment.md#ports-and-network)).
- An administrator account on the server and permission to register a client application at the identity provider.
- The provider publishes a discovery document at `<issuer URL>/.well-known/openid-configuration` containing
  `issuer`, `authorization_endpoint` and `token_endpoint`.

## Register CIDX at the identity provider

Create a confidential web client with:

| Item | Value |
|------|-------|
| Grant type | Authorization code |
| Redirect URI | `<public server URL>/auth/sso/callback` |
| PKCE | S256. The server always sends a PKCE challenge |
| Client authentication | Client secret, sent in the token request body (`client_id`, `client_secret` form fields) |
| Scopes | At least `openid`, plus whatever makes the claims below appear (commonly `profile`, `email`) |

The server reads user information from the **ID token** returned by the token endpoint (it does not call the
userinfo endpoint), so the provider must put these claims in the ID token:

| Claim | Used for |
|-------|----------|
| `sub` | Stable identity of the user (required) |
| the email claim (default `email`) and `email_verified` | Linking to an existing CIDX account and recording the email of a new one |
| the username claim (default `preferred_username`) | The username of an account created on first sign-in |
| the groups claim (default `groups`), a list | Initial group assignment through group mappings |

### The redirect URI the server uses

The server builds the redirect URI from the `CIDX_ISSUER_URL` environment variable of the `cidx-server` systemd unit
when it is set (`<CIDX_ISSUER_URL>/auth/sso/callback`), otherwise from the URL of the incoming request. Behind a
reverse proxy, set `CIDX_ISSUER_URL` to the public URL (see [Deployment](../deployment.md#systemd-units)); the
value registered at the provider must match it exactly, including scheme and any trailing slash.

## Configure the server

OIDC settings are runtime settings stored in the server database, not in `config.json`. Edit them in the Web UI:
Configuration, Authentication and Security, **SSO Authentication** (section `oidc`, saved to
`/admin/config/oidc`).

| Setting | Default | Meaning |
|---------|---------|---------|
| `enabled` | `false` | Master switch |
| `issuer_url` | empty | Issuer URL; the discovery document is fetched from `<issuer_url>/.well-known/openid-configuration` |
| `client_id` | empty | Client identifier from the provider |
| `client_secret` | empty | Client secret. The form never displays it; leave the field empty to keep the stored secret |
| `scopes` | `openid profile email` | Space-separated scopes requested at sign-in |
| `email_claim` | `email` | ID-token claim holding the email address |
| `username_claim` | `preferred_username` | ID-token claim used as the username of an account created on first sign-in |
| `groups_claim` | `groups` | ID-token claim holding the user's groups |
| `group_mappings` | `[]` | External group to CIDX group mappings (see [Group mapping](#group-mapping)) |
| `require_email_verification` | `true` | Link or create accounts only when `email_verified` is true |
| `enable_jit_provisioning` | `true` | Create an account on first sign-in when none matches |
| `default_role` | `normal_user` | Role of an account created on first sign-in (`normal_user` or `admin`) |
| `use_pkce` | `true` | Stored and shown in the form; the server sends a PKCE (S256) challenge regardless |

Saving the section applies it as one audited change. The server first builds the SSO components for the new values;
if that fails the page shows `Invalid OIDC configuration: ... Changes not saved.` (HTTP 400) and nothing changes.
The new values take effect at once in the server process that handled the save. Restart the server (every node in a
cluster) so that all worker processes use them.

The provider's discovery document is fetched at the first SSO sign-in after a start or a save, not when saving. A
wrong or unreachable issuer therefore shows up at sign-in (see [Troubleshooting](#troubleshooting)).

## The sign-in flow

1. The user selects SSO on the login page, which calls `GET /login/sso` (optionally with `redirect_to`, a local
   path to return to).
2. The server redirects to the provider's authorization endpoint with a state token and a PKCE challenge.
3. The provider redirects back to `/auth/sso/callback`. The server validates the state token, exchanges the code
   for tokens and reads the claims from the ID token.
4. The server matches or creates the CIDX account (next section).
5. If that account has TOTP MFA enabled, the user gets the MFA challenge page before any session is issued (see
   [Login and Elevation](login-and-elevation.md#sso-plus-mfa)). Codes answered at a challenge started by SSO are
   throttled under their own key, separate from the account's password attempts.
6. Otherwise a Web UI session starts and the browser goes to `redirect_to` if given, else `/admin/` for
   administrators and `/user/api-keys` for other roles.

When the sign-in started from the OAuth authorization flow (`/oauth/authorize`, used by MCP clients that connect with
OAuth), step 6 issues an OAuth authorization code to the client instead of a Web UI session; the MFA challenge, if
any, completes at `/oauth/mfa/verify`.

## Account matching

On each SSO sign-in the server looks for the account in this order:

1. **Existing SSO link.** If the provider subject (`sub`) is already linked to an account, that account signs in.
   A link whose account no longer exists is removed and matching continues.
2. **Email link.** If the ID token has an email, and either `email_verified` is true or
   `require_email_verification` is `false`, an existing account with the same email (case-insensitive) is linked to
   the subject and signs in. Its role and password are unchanged.
3. **Creation on first sign-in (JIT).** If `enable_jit_provisioning` is `true`, a new account is created when:
   - `email_verified` is true, or `require_email_verification` is `false`;
   - the username claim is present;
   - no account with that username exists already;
   - the username passes the server's username validation.

   The account gets `default_role`, the email from the token, the SSO link, and no password known to the user.

If none of these applies, the sign-in is refused with HTTP 403 `User not authorized. Please contact
administrator.` A username collision is logged (`AUTH-OIDC-001`) and is never resolved by renaming: link the
existing account instead, for example by giving it the user's email so step 2 matches.

Keep `require_email_verification` on unless the provider only issues addresses it has verified: with it off, the
server links an existing account by email without the provider's confirmation that the address is verified.

SSO links are stored in the server's OAuth store (`oauth.db` on a standalone server, PostgreSQL in a cluster).
Deleting an account removes its SSO link. See [Accounts and Access](accounts-and-access.md) for roles and groups.

## Group mapping

Every SSO sign-in checks the account's group membership. If the account already belongs to a group, SSO never
changes it. If it belongs to none (normally the first sign-in), the server assigns one:

- the CIDX group mapped to the first value of the groups claim that equals some mapping's `external_group_id`
  (the order of the claim's values decides, not the order of the mappings);
- `users` when no mapping matches, the claim is empty, or the mapped CIDX group does not exist.

A failure while assigning the group is logged and does not block the sign-in.

`group_mappings` is a JSON list:

```json
[
  {"external_group_id": "00000000-0000-0000-0000-000000000001", "external_group_name": "Platform team", "cidx_group": "powerusers"},
  {"external_group_id": "engineering", "cidx_group": "users"}
]
```

`external_group_name` is optional and informational. An older object form (`{"<external id>": "<cidx group>"}`)
is accepted and converted to the list form. Later changes to a user's group are made by an administrator in the
Groups page (see [Accounts and Access](accounts-and-access.md#groups-and-repository-access)).

Group mapping assigns a group, not a role. The role of an account created on first sign-in is `default_role`.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `/login/sso` answers HTTP 400 `SSO is not enabled on this server` | `enabled` is off, or the process serving the request has not loaded the new settings; restart the server |
| HTTP 503 `SSO provider is currently unavailable` | The discovery document could not be fetched. Check `issuer_url` and that the server can reach `<issuer_url>/.well-known/openid-configuration`; the error is logged as `AUTH-GENERAL-004` |
| Provider shows a redirect URI mismatch | The registered redirect URI differs from the one the server sends. Set `CIDX_ISSUER_URL` to the public URL and register `<CIDX_ISSUER_URL>/auth/sso/callback` |
| HTTP 400 `Invalid state` on the callback | The state token expired or the callback did not come from a sign-in this server started; start again from the login page |
| HTTP 500 `ID token not returned by provider` | Include the `openid` scope |
| HTTP 403 `User not authorized. Please contact administrator.` | No account matched and none could be created: email not verified, username claim missing, username already taken (`AUTH-OIDC-001`), username rejected (`AUTH-OIDC-002`), or JIT provisioning off |
| New user lands in `users` instead of a mapped group | The groups claim is missing from the ID token, its values do not equal any `external_group_id`, or the account already had a group |

Server logs: Web UI `/admin/logs` (filter on `oidc` or `SSO`), or on a standalone node:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, message FROM logs WHERE message LIKE '%SSO%' OR message LIKE '%OIDC%' ORDER BY id DESC LIMIT 50"
```

To inspect the provider's discovery document from the server host:

```bash
curl -s https://idp.example.com/.well-known/openid-configuration | jq '.authorization_endpoint, .token_endpoint'
```
