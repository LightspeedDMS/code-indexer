# Security Policy

## Supported Versions

CIDX follows a rolling release model. Security fixes are applied to the latest
release on the `master` branch. Please make sure you are running the most recent
release before reporting a vulnerability.

| Version                     | Supported          |
|-----------------------------|--------------------|
| Latest release (`master`)   | Yes                |
| Older releases              | No                 |

## Reporting a Vulnerability

Please report security vulnerabilities **privately**. Do not open a public issue,
pull request, or discussion for a suspected vulnerability.

1. Go to the [Security Advisories](https://github.com/LightspeedDMS/code-indexer/security/advisories) page.
2. Click **Report a vulnerability** to open a private advisory.
3. Include reproduction steps, affected version(s), and an impact assessment.

We will acknowledge your report, work with you to understand and validate the
issue, and coordinate a fix and disclosure timeline.

## Scope

CIDX has a meaningful security surface, particularly in Server and Cluster mode.
Reports concerning the following areas are especially valued:

- Authentication and authorization (OAuth 2.0 / OIDC, role-based permissions).
- TOTP step-up elevation for administrative operations.
- The X-Ray evaluator sandbox (AST whitelist, restricted builtins, process isolation).
- Multi-user and multi-tenant data isolation across golden and activated repositories.

Relevant architecture is documented under [docs/security/](docs/security/),
[docs/totp-elevation.md](docs/totp-elevation.md), and
[docs/oidc-setup-and-configuration.md](docs/oidc-setup-and-configuration.md).

## Coordinated Disclosure

We support coordinated disclosure. Please give us a reasonable opportunity to
release a fix before disclosing a vulnerability publicly.
