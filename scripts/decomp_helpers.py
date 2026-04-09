#!/usr/bin/env python3
"""Helper subcommands for decomp_overnight.sh.

Extracts inline python into testable, debuggable functions.

Usage:
    python3 scripts/decomp_helpers.py <subcommand> [args...]

Subcommands:
    ninja-progress              Read ninja stdout, show progress bar
    stream-monitor <log> <pid>  Poll stream JSON, print live summaries
    parse-result <log>          Parse stream JSON result event
    token-usage <log>           Sum token usage from stream JSON
    extract-log <stream> <out>  Extract readable log from stream JSON
    filter-stubs                Filter stubs and pick next target (reads JSON from stdin)
    extract-asm <file> <func>   Extract function assembly from .s file
    cutoff-epoch <hour>         Print cutoff epoch timestamp
    progress-save <file> <func> <status>  Append to progress JSON
    progress-check <file> <func>          Check if function already tried
    parse-rate-limit <log> <backoff>      Parse rate limit reset time
    resolve-sda-constants <asm> <func...> Resolve SDA float constants from asm
    extract-struct <header> <name>        Extract struct definition from header
    draft-pr-body <status_file>          Generate draft PR body markdown
    draft-pr-set-status <file> ...       Update function status in draft PR
    draft-pr-clear-pending <file>        Clear stale pending entries
    github-exclusions <repo>             List function names from open PRs/issues
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def load_json_file(path, default):
    """Load JSON from a file, returning default on missing/corrupt input."""
    try:
        with open(path) as f:
            value = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return value


def normalize_name(value):
    """Normalize function names for case-insensitive comparisons."""
    return str(value).strip().lower()


def parse_int(value, default=0):
    """Parse an int-like value, returning default on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def cmd_ninja_progress():
    """Read ninja output from stdin, display progress bar."""
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for line in sys.stdin:
        m = re.match(r"\[(\d+)/(\d+)\]", line)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if total <= 0:
                continue
            pct = cur * 100 // total
            filled = pct * 30 // 100
            bar = "\u2588" * filled + "\u2591" * (30 - filled)
            print(f"\r  [{bar}] {cur}/{total} ({pct}%)", end="", flush=True)
    print()


def cmd_stream_monitor(log_file, pid_str, done_flag=None):
    """Poll stream JSON log, print live summaries, exit when PID dies.

    If done_flag path is given, writes to it when a result event is seen.

    Supports --include-partial-messages: when stream_event events arrive,
    text/thinking deltas are printed inline as they stream so the user sees
    live progress during long first responses. Falls back to per-block
    assistant events when partials are absent.
    """
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        pid = int(pid_str)
    except ValueError:
        print("invalid pid", file=sys.stderr)
        sys.exit(1)

    pos = 0
    start_time = time.time()
    last_event_time = start_time
    last_heartbeat = start_time
    HEARTBEAT_INTERVAL = 15  # seconds between idle heartbeats
    saw_partials = False
    # Per-block streaming state
    current_block_type = None  # "thinking" | "text" | "tool_use" | None
    block_has_content = False  # did this block already print anything?
    thinking_chars_printed = 0
    THINKING_PREVIEW_MAX = 200  # cap thinking echo per block to keep output tidy

    def end_current_block():
        nonlocal current_block_type, block_has_content, thinking_chars_printed
        if current_block_type in ("text", "thinking") and block_has_content:
            print("", flush=True)
        current_block_type = None
        block_has_content = False
        thinking_chars_printed = 0

    while True:
        try:
            if os.path.exists(log_file):
                file_size = os.path.getsize(log_file)
                if file_size < pos:
                    pos = 0

                with open(log_file, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    for line in f:
                        try:
                            d = json.loads(line)
                        except (json.JSONDecodeError, KeyError):
                            continue

                        last_event_time = time.time()
                        t = d.get("type", "")

                        if t == "stream_event":
                            saw_partials = True
                            ev = d.get("event", {})
                            et = ev.get("type", "")

                            if et == "content_block_start":
                                end_current_block()
                                cb = ev.get("content_block", {})
                                cb_type = cb.get("type", "")
                                if cb_type == "thinking":
                                    current_block_type = "thinking"
                                    print("  [thinking] ", end="", flush=True)
                                elif cb_type == "text":
                                    current_block_type = "text"
                                    print("  [claude] ", end="", flush=True)
                                elif cb_type == "tool_use":
                                    current_block_type = "tool_use"
                                    name = cb.get("name", "tool_use")
                                    print(f"  [{name}]", flush=True)

                            elif et == "content_block_delta":
                                delta = ev.get("delta", {})
                                dt = delta.get("type", "")
                                if dt == "text_delta" and current_block_type == "text":
                                    txt = delta.get("text", "")
                                    if txt:
                                        print(txt, end="", flush=True)
                                        block_has_content = True
                                elif dt == "thinking_delta" and current_block_type == "thinking":
                                    if thinking_chars_printed < THINKING_PREVIEW_MAX:
                                        txt = delta.get("thinking", "")
                                        remaining = THINKING_PREVIEW_MAX - thinking_chars_printed
                                        chunk = txt[:remaining]
                                        if chunk:
                                            print(chunk, end="", flush=True)
                                            block_has_content = True
                                            thinking_chars_printed += len(chunk)
                                            if thinking_chars_printed >= THINKING_PREVIEW_MAX:
                                                print("...", end="", flush=True)
                                # ignore input_json_delta, signature_delta

                            elif et == "content_block_stop":
                                end_current_block()

                            # ignore message_start, message_delta, message_stop

                        elif t == "assistant" and not saw_partials:
                            # Fallback when partials are disabled: print per-block
                            # summaries from the full assistant event.
                            for c in d.get("message", {}).get("content", []):
                                if (
                                    c.get("type") == "text"
                                    and c.get("text", "").strip()
                                ):
                                    print(
                                        f"  [claude] {c['text'][:120]}",
                                        flush=True,
                                    )
                                elif c.get("type") == "tool_use":
                                    inp = c.get("input", {})
                                    if not isinstance(inp, dict):
                                        inp = {}
                                    name = c.get("name", "tool_use")
                                    desc = (
                                        inp.get("description", "")
                                        or inp.get("pattern", "")
                                        or inp.get("file_path", "")
                                        or ""
                                    )
                                    print(
                                        f"  [{name}] {desc[:100]}", flush=True
                                    )

                        elif t == "result":
                            end_current_block()
                            s = d.get("subtype", "")
                            print(f"  [result] {s}", flush=True)
                            if done_flag:
                                try:
                                    Path(done_flag).write_text(s)
                                except OSError:
                                    pass
                    pos = f.tell()

            # Heartbeat: reassure the user that Claude is still alive when
            # no stream events have landed recently. Especially important
            # during the long first-response window for huge decomp prompts.
            now = time.time()
            idle = now - last_event_time
            if idle >= HEARTBEAT_INTERVAL and (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                elapsed_total = int(now - start_time)
                print(
                    f"  [waiting] idle {int(idle)}s (total {elapsed_total}s, pid {pid} alive)",
                    flush=True,
                )
                last_heartbeat = now

            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            break
        except OSError:
            break


def cmd_parse_result(log_file):
    """Parse stream JSON for the result event. Prints structured output.

    Output format:
        status=success
        status=failure best=XX.X
        status=error

    Uses the `type: "result"` event's `subtype` and `result` fields,
    NOT grep-based text matching on conversational output.
    """
    result_event = None
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "result":
                        result_event = d
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        print("status=error")
        sys.exit(1)

    if result_event is None:
        print("status=error")
        sys.exit(1)

    subtype = result_event.get("subtype", "")
    result_text = result_event.get("result", "")
    if not isinstance(result_text, str):
        result_text = str(result_text or "")
    is_error = result_event.get("is_error", False)

    if is_error or subtype == "error":
        print("status=error")
        sys.exit(0)

    # Extract game context summary if present
    context_match = re.search(r"CONTEXT:\s*(.+?)(?:\n|$)", result_text)
    if context_match:
        print(f"context={context_match.group(1).strip()}")

    results_match = re.search(r"RESULTS:\s*(.+?)(?:\n|$)", result_text)
    if results_match:
        parts = []
        for part in results_match.group(1).split():
            m = re.match(r"([A-Za-z_0-9]+)=(SUCCESS|FAILURE)", part)
            if m:
                parts.append((m.group(1), m.group(2)))
        if any(status == "SUCCESS" for _, status in parts):
            print("status=success")
        else:
            best = "?"
            m = re.search(r"best=([0-9.]+)", result_text)
            if m:
                best = m.group(1)
            print(f"status=failure best={best}")
        for fname, fstatus in parts:
            print(
                f"func_{fname}="
                f"{'success' if fstatus == 'SUCCESS' else 'failure'}"
            )
        return

    # Check the result text for SUCCESS/FAILURE markers.
    # The result field contains the last assistant message text.
    if re.search(r"\bSUCCESS\b", result_text):
        print("status=success")
    elif re.search(r"\bFAILURE\b", result_text):
        best = "?"
        m = re.search(r"best=([0-9.]+)", result_text)
        if m:
            best = m.group(1)
        print(f"status=failure best={best}")
    else:
        # Subtype from the result event itself
        if subtype == "success":
            print("status=success")
        else:
            print(f"status=failure best=?")

def cmd_token_usage(log_file):
    """Sum token usage across all assistant messages in stream JSON."""
    input_t = output_t = cache_create = cache_read = 0
    try:
        with open(log_file) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "assistant":
                        u = d.get("message", {}).get("usage", {})
                        input_t += u.get("input_tokens", 0)
                        output_t += u.get("output_tokens", 0)
                        cache_create += u.get("cache_creation_input_tokens", 0)
                        cache_read += u.get("cache_read_input_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        print("tokens=unknown")
        return

    total = input_t + output_t + cache_create + cache_read

    def fmt(n):
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    print(
        f"total={fmt(total)} in={fmt(input_t)} out={fmt(output_t)} "
        f"cache_create={fmt(cache_create)} cache_read={fmt(cache_read)}"
    )


def cmd_extract_log(stream_log, output_log):
    """Extract readable log from stream JSON."""
    try:
        Path(output_log).parent.mkdir(parents=True, exist_ok=True)
        with open(stream_log, encoding="utf-8", errors="replace") as inp, open(
            output_log,
            "w",
        ) as out:
            for line in inp:
                try:
                    d = json.loads(line)
                    if d.get("type") == "assistant":
                        for c in d.get("message", {}).get("content", []):
                            if c.get("type") == "text":
                                out.write(c.get("text", "") + "\n")
                            elif c.get("type") == "tool_use":
                                inp_obj = c.get("input", {})
                                if not isinstance(inp_obj, dict):
                                    inp_obj = {}
                                v = list(inp_obj.values())
                                out.write(
                                    f">> {c.get('name', 'tool_use')}"
                                    f"({str(v[0])[:80] if v else ''})\n"
                                )
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        pass


def cmd_filter_stubs(excluded_json, branch_funcs_str, progress_file, batch_size="1",
                     draft_status_file=""):
    """Filter stubs from stdin JSON, remove excluded/tried/branched.

    Reads stubs JSON array from stdin.
    If batch_size > 1, picks up to that many small functions from the same file.
    Prints JSON array of targets.
    """
    try:
        batch_size = max(1, int(batch_size))
    except ValueError:
        batch_size = 1

    try:
        stubs = json.load(sys.stdin)
    except json.JSONDecodeError:
        stubs = []
    if not isinstance(stubs, list):
        stubs = []

    try:
        excluded_values = json.loads(excluded_json)
    except json.JSONDecodeError:
        excluded_values = []
    excluded = {normalize_name(x) for x in excluded_values}

    progress = load_json_file(progress_file, [])
    if not isinstance(progress, list):
        progress = []
    already_tried = {
        normalize_name(e.get("name"))
        for e in progress
        if isinstance(e, dict) and e.get("name")
    }

    # Also exclude functions already matched or persistently failed in the draft status file
    if draft_status_file and os.path.exists(draft_status_file):
        draft_entries = load_json_file(draft_status_file, [])
        if isinstance(draft_entries, list):
            for e in draft_entries:
                if (
                    isinstance(e, dict)
                    and e.get("status") in ("matched", "failed")
                    and e.get("name")
                ):
                    already_tried.add(normalize_name(e["name"]))

    branch_funcs = {
        normalize_name(line)
        for line in branch_funcs_str.strip().splitlines()
        if line.strip()
    }

    targets = [
        s
        for s in stubs
        if isinstance(s, dict)
        and normalize_name(s.get("name")) not in excluded
        and normalize_name(s.get("name")) not in already_tried
        and normalize_name(s.get("name")) not in branch_funcs
        and parse_int(s.get("size", 0), 0) > 0
    ]
    targets.sort(key=lambda s: parse_int(s.get("size", 0), 0))

    if batch_size <= 1 or not targets:
        print(json.dumps(targets[:1]))
        return

    # Batch: pick up to batch_size small functions from the same file as the smallest
    first = targets[0]
    first_file = first.get("file")
    same_file = [s for s in targets if s.get("file") == first_file]
    # Only batch functions that are small (< 200 bytes)
    batch = [
        s for s in same_file if parse_int(s.get("size", 0), 0) < 200
    ][:batch_size]
    if not batch:
        batch = [first]
    print(json.dumps(batch))


def cmd_trim_context(c_file, *func_names):
    """Print trimmed source context: stubs + nearby implemented functions for reference.

    Shows each stub with surrounding context, plus up to 3 nearby implemented
    functions as style examples. Much smaller than dumping the entire file.
    """
    try:
        lines = Path(c_file).read_text().splitlines()
    except FileNotFoundError:
        print("(file not found)")
        return

    total = len(lines)
    func_set = set(func_names)
    include = set()

    # Find stub locations and include ±5 lines around each
    for i, line in enumerate(lines):
        m = re.match(r"^/// #(\w+)\s*$", line)
        if m and m.group(1) in func_set:
            for j in range(max(0, i - 5), min(total, i + 6)):
                include.add(j)

    # Find implemented functions near the stubs as style examples
    # Look for function definitions (type + name + open paren at start of line)
    func_defs = []
    for i, line in enumerate(lines):
        if re.match(r"^\w[\w\s\*]*\w+\s*\(", line) and "{" in "\n".join(lines[i:i+3]):
            # Find end of function (matching closing brace)
            depth = 0
            end = i
            for j in range(i, min(total, i + 200)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0 and j > i:
                    end = j
                    break
            func_defs.append((i, end))

    # Pick up to 3 implemented functions closest to any stub
    stub_lines = []
    for i, line in enumerate(lines):
        m = re.match(r"^/// #(\w+)\s*$", line)
        if m and m.group(1) in func_set:
            stub_lines.append(i)

    if stub_lines and func_defs:
        def dist_to_stubs(fd):
            return min(abs(fd[0] - s) for s in stub_lines)
        nearby = sorted(func_defs, key=dist_to_stubs)[:3]
        for start, end in nearby:
            for j in range(start, min(total, end + 1)):
                include.add(j)

    # Always include file header (includes, typedefs) — first 40 lines
    for j in range(min(40, total)):
        include.add(j)

    # Print with ellipsis for gaps
    sorted_lines = sorted(include)
    prev = -2
    for i in sorted_lines:
        if i > prev + 1:
            print(f"... (lines {prev + 2}-{i} omitted)")
        print(f"{i + 1:4d}: {lines[i]}")
        prev = i
    if prev < total - 1:
        print(f"... (lines {prev + 2}-{total} omitted)")


def cmd_extract_asm(asm_file, func_name):
    """Extract a function's assembly from a .s file."""
    try:
        text = Path(asm_file).read_text()
    except FileNotFoundError:
        print("(asm file not found)")
        return

    # Try .fn/.endfn format first
    pattern = rf"(\.fn {re.escape(func_name)}.*?\.endfn {re.escape(func_name)})"
    m = re.search(pattern, text, re.DOTALL)
    if m:
        print(m.group(1))
        return

    # Fallback: glabel to next glabel
    pattern = rf"(glabel {re.escape(func_name)}\b.*?)(?=\nglabel |\Z)"
    m = re.search(pattern, text, re.DOTALL)
    if m:
        print(m.group(1))
        return

    print("(function not found in asm)")


def cmd_cutoff_epoch(hour_str):
    """Print cutoff epoch timestamp for given hour (handles midnight wrap)."""
    try:
        hour = int(hour_str)
    except ValueError:
        print("invalid hour", file=sys.stderr)
        sys.exit(1)
    if hour < 0 or hour > 23:
        print("hour must be between 0 and 23", file=sys.stderr)
        sys.exit(1)
    now = datetime.now()
    cutoff = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if cutoff <= now:
        cutoff += timedelta(days=1)
    print(int(cutoff.timestamp()))


def cmd_progress_save(progress_file, func_name, status):
    """Append an entry to the progress JSON file."""
    data = load_json_file(progress_file, [])
    if not isinstance(data, list):
        data = []
    data.append(
        {"name": func_name, "status": status, "time": time.strftime("%H:%M:%S")}
    )
    Path(progress_file).parent.mkdir(parents=True, exist_ok=True)
    with open(progress_file, "w") as f:
        json.dump(data, f, indent=2)


def cmd_progress_check(progress_file, func_name):
    """Exit 0 if function was already tried (success or failure), else exit 1."""
    if not os.path.exists(progress_file):
        sys.exit(1)
    entries = load_json_file(progress_file, [])
    if not isinstance(entries, list):
        sys.exit(1)
    if any(
        isinstance(e, dict)
        and e.get("name") == func_name
        and e.get("status") in ("success", "failure")
        for e in entries
    ):
        sys.exit(0)
    sys.exit(1)


def cmd_parse_rate_limit(log_file, backoff_str):
    """Parse rate limit reset time from log. Prints seconds to wait."""
    backoff = int(backoff_str)
    try:
        text = Path(log_file).read_text()
    except FileNotFoundError:
        print(backoff)
        return

    m = re.search(r"resets (\d{1,2})(?::(\d{2}))?(am|pm)", text, re.I)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if hour < 1 or hour > 12:
            print(backoff)
            return
        if minute < 0 or minute > 59:
            print(backoff)
            return
        meridiem = m.group(3).lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        now = datetime.now()
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=1)
        wait = int((reset - now).total_seconds())
        print(max(60, min(wait, 7200)))
    else:
        print(backoff)


def cmd_resolve_sda_constants(asm_file, *func_names):
    """Resolve SDA float/double constants from the asm file's .sdata2/.sdata.

    Parses target functions' assembly for @sda21 references, then looks up
    their values in the .sdata2/.sdata sections of the same file.
    """
    try:
        text = Path(asm_file).read_text()
    except FileNotFoundError:
        print("(asm file not found)")
        return

    # Extract @sda21 references from target functions
    sda_refs = set()
    for func_name in func_names:
        # Extract function asm
        pattern = (
            rf"\.fn {re.escape(func_name)}.*?"
            rf"\.endfn {re.escape(func_name)}"
        )
        m = re.search(pattern, text, re.DOTALL)
        if not m:
            pattern = (
                rf"glabel {re.escape(func_name)}\b.*?"
                rf"(?=\nglabel |\Z)"
            )
            m = re.search(pattern, text, re.DOTALL)
        if m:
            for ref in re.findall(r'(\w+)@sda21', m.group(0)):
                sda_refs.add(ref)

    if not sda_refs:
        print("(no SDA references found)")
        return

    # Parse .sdata and .sdata2 sections for .obj/.endobj blocks
    sda_values = {}
    for m in re.finditer(
        r'\.obj (\w+),.*?\n(.*?)\.endobj \1', text, re.DOTALL
    ):
        sym = m.group(1)
        if sym not in sda_refs:
            continue
        body = m.group(2)

        # Try .float
        fm = re.search(r'\.float\s+(.+)', body)
        if fm:
            sda_values[sym] = f"{fm.group(1).strip()}f"
            continue

        # Try .double
        dm = re.search(r'\.double\s+(.+)', body)
        if dm:
            sda_values[sym] = f"{dm.group(1).strip()} (double)"
            continue

        # Try .string
        sm = re.search(r'\.string\s+"(.*?)"', body)
        if sm:
            sda_values[sym] = f'"{sm.group(1)}" (string)'
            continue

        # Try .4byte (integer constant)
        bm = re.search(r'\.4byte\s+(0x[0-9A-Fa-f]+|\d+)', body)
        if bm:
            sda_values[sym] = bm.group(1)
            continue

        sda_values[sym] = "(unknown format)"

    # Print in order of first appearance
    for ref in sorted(sda_refs, key=lambda r: text.index(r)):
        val = sda_values.get(ref, "(not found in .sdata/.sdata2)")
        print(f"{ref} = {val}")


def cmd_extract_struct(types_header, struct_name):
    """Extract a struct definition from a types header file.

    Finds `struct <name> {` and extracts through the closing `};`.
    """
    try:
        lines = Path(types_header).read_text().splitlines()
    except FileNotFoundError:
        print(f"(header not found: {types_header})")
        return

    start = None
    for i, line in enumerate(lines):
        if re.search(rf"^\s*struct\s+{re.escape(struct_name)}\b.*\{{\s*$", line):
            start = i
            break

    if start is None:
        print(f"(struct {struct_name} not found in {types_header})")
        return

    # Find matching closing brace
    depth = 0
    end = start
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            end = i
            break

    for i in range(start, end + 1):
        print(lines[i])


def cmd_draft_pr_clear_pending(status_file):
    """Clear stale 'pending' entries from the draft PR status file."""
    try:
        with open(status_file) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    entries = [e for e in entries if e.get("status") != "pending"]
    with open(status_file, "w") as f:
        json.dump(entries, f, indent=2)


def cmd_github_exclusions(repo):
    """Print JSON list of function names mentioned in open PRs/issues."""
    names = set()
    try:
        prs = json.loads(
            subprocess.run(
                [
                    "gh", "pr", "list", "--repo", repo, "--state", "open",
                    "--limit", "100", "--json", "title,body,number",
                ],
                capture_output=True, text=True,
            ).stdout or "[]"
        )
        issues = json.loads(
            subprocess.run(
                [
                    "gh", "issue", "list", "--repo", repo, "--state", "open",
                    "--limit", "100", "--json", "title,body",
                ],
                capture_output=True, text=True,
            ).stdout or "[]"
        )
    except Exception:
        prs, issues = [], []
    # Skip the draft progress-tracking PR (wip/pending-matches) — it lists
    # functions we've already tried, not someone else's in-progress work
    prs = [p for p in prs if p.get("title", "").lower() not in
           ("work in progress — pending matches", "match progress")]
    for item in prs + issues:
        text = (item.get("title", "") + " " + item.get("body", "")).lower()
        for m in re.finditer(
            r"\b(fn_[0-9a-f]{6,}|ft[A-Z]\w*_\w+|gr\w+_\w+|it_\w+|gm\w+_\w+)\b",
            text, re.I,
        ):
            names.add(m.group(1).lower())
    # Scan PR diffs for stub removals (lines like '- /// #funcName')
    for pr in prs:
        num = pr.get("number")
        if not num:
            continue
        try:
            diff = subprocess.run(
                [
                    "gh", "api", f"repos/{repo}/pulls/{num}/files",
                    "--jq", ".[].patch // empty",
                ],
                capture_output=True, text=True, timeout=10,
            ).stdout or ""
            for m in re.finditer(r"^-.*/// #(\w+)", diff, re.MULTILINE):
                names.add(m.group(1).lower())
        except Exception:
            pass
    print(json.dumps(sorted(names)))


def cmd_draft_pr_set_status(status_file, *args):
    """Update function status in the draft PR status file.

    Usage: draft-pr-set-status <file> <name> <src_file> <status> [detail] [context]
    """
    name = args[0]
    src_file = args[1]
    status = args[2]
    detail = args[3] if len(args) > 3 else ""
    context = args[4] if len(args) > 4 else ""
    try:
        with open(status_file) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    found = False
    for e in entries:
        if e["name"] == name:
            e["status"] = status
            if detail:
                e["detail"] = detail
            if context:
                e["context"] = context
            found = True
            break
    if not found:
        entries.append({
            "name": name, "file": src_file, "status": status,
            "detail": detail, "context": context,
        })
    with open(status_file, "w") as f:
        json.dump(entries, f, indent=2)


def cmd_draft_pr_body(status_file):
    """Generate the draft PR body from a JSON status file.

    The status file is a JSON array of entries:
        {"name": "func", "file": "src/...", "status": "pending|matched|failed",
         "detail": "100%", "context": "..."}

    Outputs the markdown body to stdout.
    """
    try:
        with open(status_file) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []

    matched = [e for e in entries if e["status"] == "matched"]
    failed = [e for e in entries if e["status"] == "failed"]
    pending = [e for e in entries if e["status"] == "pending"]

    lines = []
    lines.append("## Progress")
    lines.append("")
    lines.append(f"**{len(matched)}** matched, **{len(failed)}** failed"
                 + (f", **{len(pending)}** in progress" if pending else ""))
    lines.append("")

    if pending:
        lines.append("### In progress")
        lines.append("| File | Function | Status |")
        lines.append("|------|----------|--------|")
        for e in pending:
            basename = os.path.basename(e.get("file", "?"))
            lines.append(f"| `{basename}` | `{e['name']}` | ⏳ pending |")
        lines.append("")

    if matched:
        lines.append("### Matched")
        lines.append("| File | Function | Status |")
        lines.append("|------|----------|--------|")
        for e in matched:
            basename = os.path.basename(e.get("file", "?"))
            detail = e.get("detail", "100%")
            lines.append(f"| `{basename}` | `{e['name']}` | ✓ {detail} |")
        lines.append("")

    if failed:
        lines.append("### Failed")
        for e in failed:
            detail = e.get("detail", "?")
            lines.append(f"- `{e['name']}` — {detail}")
        lines.append("")

    # Collect game context summaries
    contexts = {}
    for e in matched:
        ctx = e.get("context", "")
        if ctx:
            basename = os.path.basename(e.get("file", "?"))
            contexts.setdefault(basename, []).append(ctx)
    if contexts:
        lines.append("## What these functions do")
        for basename, ctxs in contexts.items():
            # Deduplicate (batches share a context line)
            for ctx in dict.fromkeys(ctxs):
                lines.append(f"**{basename}** — {ctx}")
                lines.append("")

    lines.append("---")
    lines.append("🤖 Generated with [Claude Code](https://claude.ai/claude-code)")
    print("\n".join(lines))


SUBCOMMANDS = {
    "ninja-progress": (cmd_ninja_progress, 0),
    "stream-monitor": (cmd_stream_monitor, 2),  # optional 3rd arg: done_flag
    "parse-result": (cmd_parse_result, 1),
    "token-usage": (cmd_token_usage, 1),
    "extract-log": (cmd_extract_log, 2),
    "filter-stubs": (cmd_filter_stubs, 3),
    "trim-context": (cmd_trim_context, 1),  # additional args: func names
    "extract-asm": (cmd_extract_asm, 2),
    "cutoff-epoch": (cmd_cutoff_epoch, 1),
    "progress-save": (cmd_progress_save, 3),
    "progress-check": (cmd_progress_check, 2),
    "parse-rate-limit": (cmd_parse_rate_limit, 2),
    "resolve-sda-constants": (cmd_resolve_sda_constants, 1),
    "extract-struct": (cmd_extract_struct, 2),
    "draft-pr-body": (cmd_draft_pr_body, 1),
    "draft-pr-set-status": (cmd_draft_pr_set_status, 3),
    "draft-pr-clear-pending": (cmd_draft_pr_clear_pending, 1),
    "github-exclusions": (cmd_github_exclusions, 1),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SUBCOMMANDS:
        print(f"Usage: {sys.argv[0]} <subcommand> [args...]", file=sys.stderr)
        print(f"Subcommands: {', '.join(SUBCOMMANDS)}", file=sys.stderr)
        sys.exit(1)

    cmd_name = sys.argv[1]
    func, nargs = SUBCOMMANDS[cmd_name]
    args = sys.argv[2:]

    if len(args) < nargs:
        print(f"Error: {cmd_name} requires {nargs} arguments, got {len(args)}", file=sys.stderr)
        sys.exit(1)

    func(*args)


if __name__ == "__main__":
    main()
