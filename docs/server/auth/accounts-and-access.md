# Accounts and Access

Who can do what on a CIDX Server: accounts and roles, groups and repository access, API keys, MCP credentials,
SSO-linked accounts and administrator impersonation.

Related: [Login and Elevation](login-and-elevation.md) (sign-in, MFA, throttling, step-up), [OIDC](oidc.md),
[Admin Guide](../admin-guide.md).

Two independent things decide what a user can do:

- the **role** of the account decides which operations it may perform;
- the **group** the account belongs to decides which repositories it can see.

## Roles

Each account has one role. Permissions are inherited upward (`src/code_indexer/server/auth/user_manager.py`,
`User.has_permission`):

| Role | Permissions |
|------|-------------|
| `normal_user` | `query_repos`, `repository:read`, `activate_repos` |
| `power_user` | everything of `normal_user`, plus `repository:write` (file edits, git write operations) |
| `admin` | everything of `power_user`, plus `manage_users`, `manage_golden_repos`, `repository:admin` |

Each MCP tool declares the permission it needs; the [MCP tool reference](../../reference/mcp-tools/README.md) lists
it per tool.

## Accounts

The first start creates an `admin` account; change its password before exposing the server
([Deployment](../deployment.md#first-start-and-the-administrator-account)).

| Task | Web UI | REST | MCP |
|------|--------|------|-----|
| List accounts | Users (`/admin/users`) | `GET /api/admin/users` | `list_users` |
| Create an account (username, password, role) | Users | `POST /api/admin/users` | `create_user` |
| Change a role | Users | `PUT /api/admin/users/{username}` (`{"role": ...}`) | - |
| Set another user's password | Users | `PUT /api/admin/users/{username}/change-password` | - |
| Change your own password | - | `PUT /api/users/change-password` | - |
| Delete an account | Users | `DELETE /api/admin/users/{username}` | - |

The CLI equivalents are under `cidx admin users` ([CLI reference](../../reference/cli/admin.md)).

- Every write above, except changing your own password, requires step-up elevation when enforcement is on.
- Password rules come from the runtime Password Security settings (Web UI, Configuration).
- An account created through REST or MCP joins the `admins` group if its role is `admin`, otherwise the `users`
  group.
- Changing a user's role or password through the REST API ends that user's elevation windows.
- The last account with the `admin` role cannot be deleted (HTTP 400).
- Deleting an account removes the account first, then everything keyed to its name: group membership, MFA
  enrolment and recovery codes, SSO links, OAuth and refresh tokens, API keys, MCP credentials and git
  credentials. Its activated repositories are removed too. An account later created with the same name starts with
  none of them.

### Self-registration

`POST /auth/register` lets an unauthenticated caller create a `normal_user` account. It is controlled by the runtime
setting `web_security_config.self_registration_enabled` (Web UI, Configuration, Web Security), default off; while
off the endpoint answers HTTP 403.

## Groups and repository access

Code: `src/code_indexer/server/services/group_access_manager.py`.

- Every account belongs to at most one group.
- Three default groups exist from the first start: `admins`, `powerusers`, `users`. They cannot be renamed, updated
  or deleted, and a group that still has members cannot be deleted.
- A group is granted access to repositories by name. Members see the granted repositories plus `cidx-meta`, which
  every group can always read.
- Members of the `admins` group see every repository.
- A newly registered golden repository is granted to `admins` and `powerusers` automatically. `users` never
  receives an automatic grant: grant repositories to it explicitly.

| Task | Web UI | REST | MCP |
|------|--------|------|-----|
| List, create, rename, delete groups | Groups (`/admin/groups`) | `GET/POST /api/v1/groups`, `PUT/DELETE /api/v1/groups/{group_id}` | `list_groups`, `create_group`, `update_group`, `delete_group` |
| Move a user to a group | Groups | `POST /api/v1/groups/{group_id}/members`, `PUT /api/v1/users/{user_id}/group` | `manage_group_members` |
| Grant or revoke repositories | Groups | `POST /api/v1/groups/{group_id}/repos`, `DELETE /api/v1/groups/{group_id}/repos/{repo_name}` | `manage_group_repos` |

All group writes require step-up elevation when enforcement is on. The CLI equivalents are under
`cidx admin groups`.

SSO sign-in assigns a group only to an account that has none yet ([OIDC group mapping](oidc.md#group-mapping)).

Groups also store per-tool MCP grants (`/api/v1/groups/tool-access`). Per-group MCP tool grants are not enforced;
MCP tool access follows the role permissions above.

## API keys

A personal API key authenticates as its owner on REST (`Authorization: Bearer cidx_sk_...`) and through the MCP
`authenticate` tool.

| Task | Web UI | REST | MCP |
|------|--------|------|-----|
| Create (optional `name`, up to 100 characters) | API Keys (`/user/api-keys`, `/admin/api-keys`) | `POST /api/keys` | `create_api_key` |
| List | API Keys | `GET /api/keys` | `list_api_keys` |
| Delete | API Keys | `DELETE /api/keys/{key_id}` | `delete_api_key` |

- The key is returned once, at creation (`api_key` field); the server stores only a hash.
- Creating and deleting your own keys requires step-up elevation when enforcement is on, for every role. While
  enforcement is on, the API Keys entry of the user menu is hidden until the user has enrolled TOTP.

## MCP credentials

An MCP credential is a `client_id` (prefix `mcp_`) and a `client_secret` (prefix `mcp_sec_`) owned by one account,
used by MCP clients on `/mcp`. Create them in the Web UI (MCP Credentials page) or with
`POST /api/mcp-credentials`; the secret is shown once. Creation and usage are described in
[MCP registration](../../getting-started/mcp-registration.md#creating-mcp-credentials).

Administrators manage other accounts' credentials with:

| REST | Purpose |
|------|---------|
| `GET /api/admin/mcp-credentials` | All credentials of all accounts |
| `GET /api/admin/users/{username}/mcp-credentials` | One account's credentials |
| `POST /api/admin/users/{username}/mcp-credentials` | Create a credential for an account |
| `DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}` | Revoke one |

These require step-up elevation when enforcement is on; so do a user's own create and revoke
(`POST /api/mcp-credentials`, `DELETE /api/mcp-credentials/{credential_id}`). A request authenticated with an MCP
credential counts as elevated ([Login and Elevation](login-and-elevation.md#opening-a-window)).

## SSO-linked accounts

An account is linked to an identity-provider subject when SSO matches it by email or creates it on first sign-in
([OIDC account matching](oidc.md#account-matching)). Linking keeps the account's role, group and password. An account
created by SSO has no password known to the user and signs in through SSO, an API key or MCP credentials.

### Restricting accounts without SSO to the Web UI

Runtime setting `web_security_config.restrict_non_sso_to_web_ui`, default `false`. When enabled, accounts without an
SSO identity are refused on REST and MCP with HTTP 403 `Non-SSO accounts are restricted to Web UI access only` and
can use only the Web UI; SSO accounts are unaffected. The Web UI Configuration page does not show this setting.

## Impersonation

An administrator can make an MCP session act as another user, for example to check what that user can see.

- Tool: `set_session_impersonation` with `username` to start, or `null` to stop.
- Only accounts with the `admin` role can impersonate; the tool requires step-up elevation when enforcement is on.
- It needs a stateful MCP session and lasts for that session only.
- While impersonating, every tool call is checked against the impersonated user's role and repository access, so it
  can only narrow what the administrator can do. `set_session_impersonation` itself always runs as the
  administrator, so impersonation can always be cleared.
- Audit: starting, clearing and a refused attempt write the audit events `impersonation_set`,
  `impersonation_cleared` and `impersonation_denied`. Audit rows written while impersonating name the
  administrator as actor and the impersonated user in `impersonated_user`. Query them in the Web UI (Audit Logs)
  or with the MCP tool `query_audit_logs`.
