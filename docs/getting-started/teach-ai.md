# Teaching an AI assistant to use cidx (`cidx teach-ai`)

`cidx teach-ai` installs instructions that tell a local AI coding assistant to search with `cidx` instead of
grep. It runs entirely on your machine and needs no server. To give an assistant the server's tools instead,
see [MCP registration](mcp-registration.md).

Implementation: `teach_ai` in `src/code_indexer/cli.py` and `src/code_indexer/teach_ai_templates.py`. The content it
installs lives in `prompts/ai_instructions/`.

## What it installs

Every run (except `--show-only`) does two things:

1. **Skills**: copies `prompts/ai_instructions/skills/cidx/` to `~/.claude/skills/cidx/`. This is a clean
   overwrite: an existing `~/.claude/skills/cidx/` is deleted first, so local edits there are lost. The skills are
   installed to this Claude Code location whichever platform you choose. Files installed:
   `SKILL.md`, `reference/fts-search.md`, `reference/scip-intelligence.md`, `reference/semantic-search.md`,
   `reference/temporal-search.md`.
2. **Awareness section**: writes the content of `prompts/ai_instructions/awareness/awareness.md` (a section
   headed `## 1. SEMANTIC SEARCH - CIDX MANDATORY`) into the platform's instruction file (table below).

## Usage

```bash
cidx teach-ai --claude --project     # ./CLAUDE.md + skills
cidx teach-ai --claude --global      # ~/.claude/CLAUDE.md + skills
cidx teach-ai --claude --show-only   # print the awareness text and list the skill files; writes nothing
cidx teach-ai --claude --show-only --verbose   # also print every skill file
cidx teach-ai --skills-only          # only refresh ~/.claude/skills/cidx/
```

Exactly one platform flag is required, and exactly one of `--project` or `--global` unless `--show-only` is
given. `--skills-only` ignores the platform and scope flags.

## Target file per platform

| Flag | `--project` (current directory) | `--global` |
|------|--------------------------------|-----------|
| `--claude` | `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `--codex` | `CODEX.md` | `~/.codex/instructions.md` |
| `--gemini` | `.gemini/styleguide.md` | not supported (exits with an error) |
| `--opencode` | `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `--q` | `.amazonq/rules/cidx.md` | `~/.aws/amazonq/Q.md` |
| `--junie` | `.junie/guidelines.md` | not supported (exits with an error) |

Missing parent directories are created.

## Re-running (refresh) and existing files

- Target file does not exist: it is created with the awareness section only.
- Target file exists and contains a level-2 heading whose text starts with `SEMANTIC SEARCH`, case-insensitively,
  optionally after a number such as `1.` (for example `## 1. SEMANTIC SEARCH - CIDX MANDATORY`,
  `## Semantic search rules` or `## 3. Semantic Search`): that section, from its heading up to the next `## `
  heading or the end of the file, is replaced with the current awareness text. Everything else in the file is kept.
  The command reports `instructions updated`.
- Target file exists without such a heading: the awareness text is appended after a `---` separator, and the
  command reports `instructions added`. This includes headings where the words come later or at another level, such
  as `## CIDX SEMANTIC SEARCH` or `### SEMANTIC SEARCH`.

Re-running the same command after upgrading CIDX therefore refreshes the section in place instead of duplicating
it, and replaces the skills directory.

## Errors

| Message | Cause |
|---------|-------|
| `Platform required: --claude, --codex, --gemini, --opencode, --q, or --junie` | No platform flag (also with `--show-only`). |
| `Only one platform flag allowed at a time` | Two or more platform flags. |
| `Scope required: --project or --global` | Neither scope flag and no `--show-only`. |
| `Only one scope flag allowed at a time` | Both scope flags. |
| `Gemini platform only supports project-level instructions (--project)` | `--gemini --global`; the same applies to `--junie --global`. |
| `Failed to load awareness template: ...` | The templates are not available; see the limitation below. |

All of these exit with status 1.

## Known limitation: source checkout required

`teach_ai_templates.py` reads `prompts/ai_instructions/` relative to the source tree. The built package
(`pipx install`, `pip install git+...`) does not contain that directory, so from an installed package:

- `cidx teach-ai --<platform> --project|--global` fails with `Failed to load awareness template` (exit 1) before
  writing anything; an existing `~/.claude/skills/cidx/` is left as it was;
- `cidx teach-ai --skills-only` deletes an existing `~/.claude/skills/cidx/`, reports `Installed 0 files`, exits 0,
  and leaves the directory empty. Do not run it from an installed package.

Until this is fixed, run `teach-ai` from a source checkout, for example
`PYTHONPATH=<checkout>/src python3 -m code_indexer.cli teach-ai --claude --project`, or from an editable install
(`python3 -m pip install -e <checkout>`).

## Verifying

```bash
cidx teach-ai --claude --project
grep -n "SEMANTIC SEARCH" CLAUDE.md
ls ~/.claude/skills/cidx ~/.claude/skills/cidx/reference
```

Then ask the assistant to find something in the code; it should run `cidx query ...` rather than grep. The project
must be indexed first (`cidx init && cidx index`).

## Related

- [MCP registration](mcp-registration.md): connect an assistant to a CIDX server.
- [Query guide](../guides/query.md): the search options the skills describe.
