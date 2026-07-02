#!/usr/bin/env bash
# Usage: ./scripts/rename.sh <servicename> [target-parent-dir]
# Example: ./scripts/rename.sh datadog
#          ./scripts/rename.sh datadog /path/to/projects
#
# Creates <target-parent-dir>/<servicename>-mcp/ with all
# SOURCENAME/sourcename placeholders replaced with the provided name.
# Defaults to the parent directory of the template repo.
#
# Rules:
#   SOURCENAME  → UPPERCASE (e.g. DATADOG)
#   Sourcename  → PascalCase (e.g. Datadog)
#   sourcename  → lowercase (e.g. datadog)

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <servicename> [target-parent-dir]"
    echo "Example: $0 datadog"
    exit 1
fi

INPUT="$1"
LOWER="${INPUT,,}"
UPPER="${INPUT^^}"
PASCAL="${INPUT^}"

TEMPLATE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PARENT_DIR="${2:-$(dirname "$TEMPLATE_DIR")}"
TARGET_DIR="${PARENT_DIR}/${LOWER}-mcp"

if [[ -d "$TARGET_DIR" ]]; then
    echo "Error: $TARGET_DIR already exists. Remove it first."
    exit 1
fi

echo "Creating $TARGET_DIR from template..."
cp -r "$TEMPLATE_DIR" "$TARGET_DIR"

# Remove rename.sh (template-only) and git artifacts from the new project
rm -f "$TARGET_DIR/scripts/rename.sh"
rm -rf "$TARGET_DIR/.git"

# Rename files: SOURCENAME → lowercase service name in filenames
find "$TARGET_DIR" -depth -name "*SOURCENAME*" | while read -r f; do
    newname="${f//SOURCENAME/${LOWER}}"
    mv "$f" "$newname"
done
find "$TARGET_DIR" -depth -name "*sourcename*" | while read -r f; do
    newname="${f//sourcename/${LOWER}}"
    mv "$f" "$newname"
done

# Replace content placeholders in all relevant file types
find "$TARGET_DIR" -type f \( -name "*.ts" -o -name "*.json" -o -name "*.yml" -o -name "*.md" -o -name "*.js" -o -name "*.sh" -o -name "*.http" \) | while read -r f; do
    sed -i \
        -e "s/SOURCENAME/${UPPER}/g" \
        -e "s/Sourcename/${PASCAL}/g" \
        -e "s/sourcename/${LOWER}/g" \
        "$f"
done

echo ""
echo "Done. New project: $TARGET_DIR"
echo ""
echo "Next steps:"
echo "  1. cd \"$TARGET_DIR\" && npm install"
echo "  2. Implement ${PASCAL}ApiService in src/modules/${UPPER}/services/${LOWER}-api.service.ts"
echo "  3. Define tools in TOOL_DEFINITIONS inside ${LOWER}-mcp.service.ts"
echo "  4. Add SSM credentials to serverless.yml environment block"
echo "  5. Follow documentation/agent/MCP_SERVER_REGISTRATION.md (5 steps)"
