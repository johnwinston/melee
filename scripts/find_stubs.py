#!/usr/bin/env python3
"""Find undecompiled function stubs in a C source file.

Scans for `/// #function_name` markers whose function is still missing
an implementation anywhere in the file.

Outputs JSON array of {name, size, file, line} sorted by size ascending.
"""

import json
import re
import sys
from pathlib import Path

SYMBOLS_FILE = Path("config/GALE01/symbols.txt")
STUB_MARKER_RE = re.compile(r"^/// #(\w+)\s*$")
FUNC_DEF_RE = re.compile(r"^\s*[\w][\w\s\*]*\b([A-Za-z_]\w*)\s*\(")
CONTROL_KEYWORDS = frozenset({"if", "for", "while", "switch"})


def get_symbol_sizes():
    """Parse symbols.txt to get function sizes."""
    sizes = {}
    try:
        lines = SYMBOLS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return sizes

    for line in lines:
        m = re.match(
            r"(\w+)\s*=\s*\.text:0x[0-9A-Fa-f]+;\s*//\s*type:function\s+size:0x([0-9A-Fa-f]+)",
            line,
        )
        if m:
            sizes[m.group(1)] = int(m.group(2), 16)
    return sizes


def find_defined_functions(lines):
    """Return the set of implemented function names in a file."""
    defined = set()
    total = len(lines)

    for i, line in enumerate(lines):
        m = FUNC_DEF_RE.match(line)
        if not m:
            continue

        func_name = m.group(1)
        if func_name in CONTROL_KEYWORDS:
            continue

        snippet = "\n".join(lines[i:min(total, i + 4)])
        if "{" in snippet:
            defined.add(func_name)

    return defined


def find_stubs(c_file):
    """Find stub markers whose function is not implemented in the file."""
    text = Path(c_file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    defined = find_defined_functions(lines)
    stubs = []
    seen = set()
    for i, line in enumerate(lines):
        m = STUB_MARKER_RE.match(line)
        if not m:
            continue

        func_name = m.group(1)
        if func_name in seen:
            continue
        seen.add(func_name)

        if func_name not in defined:
            stubs.append({"name": func_name, "line": i + 1, "file": str(c_file)})
    return stubs


def main():
    if len(sys.argv) < 2:
        print("Usage: find_stubs.py <file.c> [<file2.c> ...]", file=sys.stderr)
        sys.exit(1)

    sizes = get_symbol_sizes()
    all_stubs = []
    for f in sys.argv[1:]:
        for stub in find_stubs(f):
            stub["size"] = sizes.get(stub["name"], 0)
            all_stubs.append(stub)

    all_stubs.sort(key=lambda s: s["size"])
    print(json.dumps(all_stubs, indent=2))


if __name__ == "__main__":
    main()
