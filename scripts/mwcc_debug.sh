#!/bin/bash
# Toggle MWCC debug output (pcdump.txt) on/off.
#
# Usage:
#   ./scripts/mwcc_debug.sh on           # enable debug DLL
#   ./scripts/mwcc_debug.sh off          # restore stock DLL
#   ./scripts/mwcc_debug.sh compile FILE # compile one .o with debug, capture pcdump
#   ./scripts/mwcc_debug.sh status       # show current state
#
# The "compile" command calls mwcceppc.exe directly (bypassing sjiswrap)
# because sjiswrap launches the compiler as a subprocess that doesn't
# inherit the debug DLL.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
COMPILER_DIR="$REPO_ROOT/build/compilers/GC/1.2.5n"
DEBUG_DLL="$REPO_ROOT/scripts/mwcc_debug/lmgr326b_debug.dll"
STOCK_DLL="$REPO_ROOT/scripts/mwcc_debug/lmgr326b_stock.dll"
ACTIVE_DLL="$COMPILER_DIR/lmgr326b.dll"

require_file() {
    local path="$1"
    if [ ! -f "$path" ]; then
        echo "ERROR: Missing file: $path" >&2
        exit 1
    fi
}

save_stock() {
    require_file "$ACTIVE_DLL"
    if [ ! -f "$STOCK_DLL" ]; then
        cp "$ACTIVE_DLL" "$STOCK_DLL"
    fi
}

enable_debug() {
    require_file "$DEBUG_DLL"
    save_stock
    cp "$DEBUG_DLL" "$ACTIVE_DLL"
}

disable_debug() {
    if [ -f "$STOCK_DLL" ]; then
        cp "$STOCK_DLL" "$ACTIVE_DLL"
    fi
}

case "${1:-}" in
    on)
        enable_debug
        echo "MWCC debug enabled."
        ;;
    off)
        disable_debug
        rm -f "$REPO_ROOT/pcdump.txt"
        echo "MWCC debug disabled."
        ;;
    compile)
        shift
        SOURCE_FILE="${1:?Usage: mwcc_debug.sh compile src/melee/path/to/file.c}"
        require_file "$SOURCE_FILE"
        OBJ_FILE="build/GALE01/${SOURCE_FILE%.c}.o"

        # Get the ninja compile command and strip sjiswrap to call compiler directly
        CMD=$(ninja -t commands "$OBJ_FILE" 2>/dev/null | awk '/mwcc/ { print; exit }')
        if [ -z "$CMD" ]; then
            echo "ERROR: Could not find compile command for $OBJ_FILE"
            exit 1
        fi

        # Extract the wrapped compiler invocation even if ninja prepends
        # setup commands like mkdir/touch before the actual compile step.
        COMPILE_CMD=$(printf '%s\n' "$CMD" | python3 -c '
import re
import sys

command = sys.stdin.read().strip()
match = re.search(
    r"(?:^|&&\s*)wine build/tools/sjiswrap\.exe (.*?)(?:\s*&&|$)",
    command,
)
if not match:
    raise SystemExit(1)
print("wine " + match.group(1).strip())
')
        if [ -z "$COMPILE_CMD" ]; then
            echo "ERROR: Could not isolate mwcc compile command for $OBJ_FILE"
            exit 1
        fi

        enable_debug
        trap 'disable_debug' EXIT
        rm -f "$REPO_ROOT/pcdump.txt"
        echo "Compiling: $SOURCE_FILE"
        eval "$COMPILE_CMD" 2>&1
        disable_debug
        trap - EXIT

        PCDUMP="$REPO_ROOT/pcdump.txt"
        if [ -f "$PCDUMP" ]; then
            BASENAME=$(basename "$SOURCE_FILE" .c)
            OUTPUT="$REPO_ROOT/scripts/mwcc_debug/pcdump_${BASENAME}.txt"
            cp "$PCDUMP" "$OUTPUT"
            LINES=$(wc -l < "$OUTPUT")
            echo "Captured: $OUTPUT ($LINES lines)"
        else
            echo "WARNING: pcdump.txt was not created"
        fi
        ;;
    status)
        if [ -f "$STOCK_DLL" ] && ! cmp -s "$ACTIVE_DLL" "$STOCK_DLL"; then
            echo "MWCC debug: ENABLED"
        else
            echo "MWCC debug: disabled"
        fi
        ;;
    *)
        echo "Usage: $0 {on|off|compile FILE|status}"
        exit 1
        ;;
esac
