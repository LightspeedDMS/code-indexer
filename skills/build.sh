#!/usr/bin/env bash
# Rebuild all .skill zip bundles from their corresponding source folders.
# Usage: ./skills/build.sh [--check] [skill-folder]
#   --check       : exit 1 if any SKILL.md is newer than its .skill zip (no rebuild)
#   skill-folder  : rebuild only that folder's zip (omit to rebuild all)
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SKILLS_DIR"

CHECK_MODE=0
TARGET_FOLDER=""

for arg in "$@"; do
    case "$arg" in
        --check) CHECK_MODE=1 ;;
        *) TARGET_FOLDER="$arg" ;;
    esac
done

# Given a folder name, find the matching .skill zip by inspecting zip contents.
# The zip stores files as <folder>/SKILL.md — extract the folder prefix.
find_zip_for_folder() {
    local folder="$1"
    local result=""
    while IFS= read -r f; do
        # Extract the first filename entry from the zip listing
        local inner
        inner=$(unzip -l "$f" 2>/dev/null | awk 'NR==4{print $4}' | sed 's:/[^/]*$::')
        if [ "$inner" = "$folder" ]; then
            result="$f"
            break
        fi
    done < <(find . -maxdepth 1 -name "*.skill" -type f)
    echo "$result"
}

# Derive a friendly zip name from a folder slug.
# lightspeed-neo-exploration -> Lightspeed Neo Exploration.skill
derive_zip_name() {
    local folder="$1"
    echo "$folder" | awk -F- '{for(i=1;i<=NF;i++){$i=toupper(substr($i,1,1))tolower(substr($i,2))}; print}' OFS=' '
    # caller appends .skill
}

check_one() {
    local folder="$1"
    local skill_md="$folder/SKILL.md"
    if [ ! -f "$skill_md" ]; then
        return 0
    fi

    local zip_name
    zip_name=$(find_zip_for_folder "$folder")
    if [ -z "$zip_name" ]; then
        return 0  # no zip to compare against — nothing to check
    fi

    if [ "$skill_md" -nt "$zip_name" ]; then
        echo "ERROR: $skill_md is newer than $zip_name"
        echo "  Rebuild before committing: ./skills/build.sh $folder"
        return 1
    fi
    return 0
}

build_one() {
    local folder="$1"
    local skill_md="$folder/SKILL.md"
    if [ ! -f "$skill_md" ]; then
        echo "ERROR: $skill_md not found"
        return 1
    fi

    local zip_name
    zip_name=$(find_zip_for_folder "$folder")

    if [ -z "$zip_name" ]; then
        # No existing zip — derive friendly name from folder slug
        zip_name="$(derive_zip_name "$folder").skill"
    fi

    echo "Rebuilding $zip_name from $folder/..."
    rm -f "$zip_name"
    zip -r "$zip_name" "$folder" >/dev/null
    echo "  -> $zip_name ($(wc -c < "$zip_name") bytes)"
}

run_on_all_folders() {
    local action="$1"  # "check_one" or "build_one"
    local stale=0
    while IFS= read -r d; do
        d="${d%/}"
        if [ -f "$d/SKILL.md" ]; then
            if ! "$action" "$d"; then
                stale=1
            fi
        fi
    done < <(find . -maxdepth 1 -mindepth 1 -type d | sed 's:^\./::')
    return "$stale"
}

if [ "$CHECK_MODE" = "1" ]; then
    stale=0
    if [ -n "$TARGET_FOLDER" ]; then
        check_one "$TARGET_FOLDER" || stale=1
    else
        run_on_all_folders check_one || stale=1
    fi

    if [ "$stale" = "1" ]; then
        echo ""
        echo "One or more SKILL.md files are out of sync with their .skill zip bundles."
        echo "Rebuild: ./skills/build.sh"
        exit 1
    fi
    echo "All .skill bundles are up to date."
    exit 0
fi

# Build mode
if [ -n "$TARGET_FOLDER" ]; then
    build_one "$TARGET_FOLDER"
else
    run_on_all_folders build_one
fi

echo "Done."
