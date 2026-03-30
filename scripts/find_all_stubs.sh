#!/bin/bash
# Find all C files with /// # stub markers and run find_stubs.py on them.
# Outputs combined JSON sorted by size.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Find all .c files containing stub markers
FILES=()
if command -v rg >/dev/null 2>&1; then
    while IFS= read -r -d '' file; do
        FILES+=("$file")
    done < <(rg -0 -l '^\s*/// #' src/melee/ -g '*.c' 2>/dev/null || true)
else
    while IFS= read -r file; do
        FILES+=("$file")
    done < <(grep -rl '/// #' src/melee/ --include='*.c' 2>/dev/null || true)
fi

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "[]"
    exit 0
fi

python3 scripts/find_stubs.py "${FILES[@]}"
