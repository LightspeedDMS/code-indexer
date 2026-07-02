# Guardrails Repository Convention

Story #457: Safety guardrails for open delegation jobs.

When CIDX executes an open delegation job via `execute_open_delegation`, it prepends
a safety guardrails prompt to every user objective before sending it to Claude Server.
This document describes how to supply a custom guardrails prompt via a dedicated
golden repository.

## Why Guardrails

Open delegation gives Claude Server broad authority to act on your codebase.
Guardrails are a mandatory safety layer that constrains the delegated agent to
acceptable behaviour: restricting filesystem access, forbidding secret leakage,
controlling package installation, and more.

When no custom guardrails repo is configured, CIDX falls back to a built-in default
template that covers all six safety categories.

## Configuration

Two settings in the Claude Delegation configuration control guardrails:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `guardrails_enabled` | bool | `true` | When `false`, no guardrails are prepended and no guardrails repo is added to the job. |
| `delegation_guardrails_repo` | string | `""` | Golden repo alias that contains `guardrails/system-prompt.md`. Empty means use the built-in default template. |

Both settings are available in the Claude Delegation configuration screen in the
CIDX Server Web UI.

## Repository Layout

A guardrails repository must follow this directory convention:

```
guardrails/
    system-prompt.md          # Required: the guardrails system prompt
packages/
    python/
        approved.txt          # Optional: one package name per line
    nodejs/
        approved.txt
    java/
        approved.txt
    go/
        approved.txt
    ruby/
        approved.txt
    rust/
        approved.txt
    dotnet/
        approved.txt
    system/
        approved.txt
```

### system-prompt.md

This file is the full guardrails system prompt sent to the delegated agent.
It may contain the `{packages_context}` placeholder, which CIDX replaces with
the formatted list of approved packages at job submission time.

Example `guardrails/system-prompt.md`:

```markdown
SAFETY GUARDRAILS - MANDATORY RULES

1. FILESYSTEM SAFETY
   Only modify files within the repository workspace.

5. PACKAGE SAFETY
   {packages_context}

USER OBJECTIVE
```

The text after `USER OBJECTIVE` is where CIDX appends the actual user prompt.

### packages/

Each subdirectory must match one of the supported language names exactly
(case-sensitive). Directories with other names are silently ignored.

Supported language names:

- `python`
- `nodejs`
- `java`
- `go`
- `ruby`
- `rust`
- `dotnet`
- `system`

Each language directory must contain an `approved.txt` file with one package
name per line. Empty lines are ignored.

Example `packages/python/approved.txt`:

```
requests
numpy
pandas
```

When no `packages/` directory exists, or when all language directories are empty,
CIDX substitutes `{packages_context}` with:

```
No pre-approved packages configured for this workspace.
```

## Resolution Rules

At job submission time, CIDX applies the following resolution order:

1. `guardrails_enabled = false` — guardrails are skipped entirely. The user
   prompt is sent as-is and no guardrails repo is added to the repositories list.

2. `delegation_guardrails_repo` is set and the golden repo exists and
   `guardrails/system-prompt.md` is present — the file is read, `{packages_context}`
   is interpolated, and the result is prepended to the prompt. The repo alias is
   added to the repositories list so Claude Server can access it.

3. Any other case (repo not set, repo missing, file not found) — the built-in
   `DEFAULT_GUARDRAILS_TEMPLATE` is used with a "no pre-approved packages" message.
   If a repo was configured but the file was not found, a warning is logged.

## Built-in Default Template

When no custom guardrails repo is configured, CIDX uses a built-in template
that covers six safety categories:

1. FILESYSTEM SAFETY
2. PROCESS SAFETY
3. GIT SAFETY
4. SYSTEM SAFETY
5. PACKAGE SAFETY (with `{packages_context}` resolved at runtime)
6. SECRETS SAFETY

The full template is defined in
`src/code_indexer/server/config/delegation_config.py` as
`DEFAULT_GUARDRAILS_TEMPLATE`.

## Registering the Guardrails Repository

Register your guardrails repository as a standard golden repo:

```bash
# Via MCP tool
add_golden_repo alias=my-guardrails url=git@github.com:org/guardrails-repo.git

# Then set the alias in delegation config
delegation_guardrails_repo = my-guardrails
```

The repo is registered and indexed like any other golden repo. CIDX accesses
`guardrails/system-prompt.md` from the versioned snapshot path, not the live
git working tree.
