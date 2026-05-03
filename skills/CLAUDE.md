# Skills Folder

Skill files for Claude.ai / Claude Code.

## File Representation

Each skill has two representations that MUST be kept in sync:

| File | Purpose |
|------|---------|
| `<skill-name>/SKILL.md` | Human-readable source — edit this |
| `<Display Name>.skill` | ZIP archive — upload to Claude.ai |

Never commit one without the other.

## Editing a Skill

1. Edit the `SKILL.md` source file directly.
2. Recompress into the `.skill` archive **before committing**:

```bash
# From the skills/ directory
cd skills/
zip "Lightspeed Neo Exploration.skill" lightspeed-neo-exploration/SKILL.md
cd ..
```

3. Commit both files together:

```bash
git add skills/
git commit -m "..."
```

## Adding a New Skill

1. Create the source folder and file:

```bash
mkdir -p skills/<skill-name>
# write skills/<skill-name>/SKILL.md
```

2. Compress into a `.skill` archive:

```bash
cd skills/
zip "<Display Name>.skill" <skill-name>/SKILL.md
cd ..
```

3. Commit both files.

## Current Skills

| Display Name | Source |
|---|---|
| Lightspeed Neo Exploration | `lightspeed-neo-exploration/SKILL.md` |
