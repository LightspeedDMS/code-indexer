# Claude.ai Skills

This directory contains source files for Claude.ai skills that get uploaded to Claude.ai as `.skill` zip bundles.

## Source vs bundle

For each skill:
- Source folder: `skills/<skill-slug>/SKILL.md` (the editable source)
- Compressed bundle: `skills/<Friendly Name>.skill` (zip uploaded to Claude.ai)

## Editing workflow

1. Edit `skills/<slug>/SKILL.md`
2. Rebuild the bundle: `./skills/build.sh` (or `./skills/build.sh <slug>` for one)
3. Re-upload `*.skill` to Claude.ai (Settings -> Skills)
4. Commit both the SKILL.md change AND the rebuilt .skill zip

## Sync enforcement

A pre-commit hook checks that SKILL.md is not newer than the .skill bundle. If you forget to rebuild, the commit will be blocked with a hint to run `./skills/build.sh`.

The hook is wired in `.pre-commit-config.yaml` as the `skill-bundle-sync` entry and runs `skills/build.sh --check`.

## build.sh reference

```
./skills/build.sh                    # rebuild all .skill zips
./skills/build.sh <slug>             # rebuild one zip (e.g. lightspeed-neo-exploration)
./skills/build.sh --check            # exit 1 if any SKILL.md is newer than its zip
./skills/build.sh --check <slug>     # check one zip only
```

## Adding a new skill

1. Create the source folder and file:

```bash
mkdir -p skills/<skill-name>
# write skills/<skill-name>/SKILL.md
```

2. Build the .skill archive:

```bash
./skills/build.sh <skill-name>
```

3. Commit both files.

## Available skills

| Display Name | Source |
|---|---|
| Lightspeed Neo Exploration | `lightspeed-neo-exploration/SKILL.md` |
