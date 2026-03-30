#!/usr/bin/env python3
"""Verify that a decompiled function matches the original, including relocations.

Compares instruction bytes and relocation targets between a compiled .o file
and the expected .o file from the original game.

Usage:
    python3 scripts/verify_match.py <compiled.o> <expected.o> <func_name> [func_name...]

Exit code 0 = all functions match. Exit code 1 = at least one mismatch.
"""

import argparse
import struct
import sys

from elftools.elf.elffile import ELFFile

from compare_bytes import get_function_info
from compare_bytes import get_relocations
from compare_bytes import mask_instruction


# R_PPC_EMB_SDA21 (109) and R_PPC_SDAREL16 (32) are SDA-relative
# relocations. The compiler resolves these at compile time, so they
# may appear in the split orig .o but not in our compiled .o (or
# vice versa). They don't affect call targets, so we only warn
# about them rather than failing.
SDA_RELOC_TYPES = {109, 32}

# Conservatively treat register-only differences as okay for the common
# D-form instructions where the register fields are bits 6..15.
SAFE_D_FORM_OPCODES = frozenset({
    7,    # mulli
    8,    # subfic
    10,   # cmpli
    11,   # cmpi
    12,   # addic
    13,   # addic.
    14,   # addi
    15,   # addis
    24,   # ori
    25,   # oris
    26,   # xori
    27,   # xoris
    28,   # andi.
    29,   # andis.
    32, 33, 34, 35, 36, 37, 38, 39,  # lwz/lwzu/lbz/lbzu/stw/stwu/stb/stbu
    40, 41, 42, 43, 44, 45, 46, 47,  # lhz/lhzu/lha/lhau/sth/sthu/lmw/stmw
    48, 49, 50, 51, 52, 53, 54, 55,  # lfs/lfsu/lfd/lfdu/stfs/stfsu/stfd/stfdu
})
REGISTER_DIFF_MASK_D_FORM = 0x03FF0000


def load_elf(path):
    """Open an ELF file and keep the file handle alive for the caller."""
    handle = open(path, "rb")
    try:
        return handle, ELFFile(handle)
    except Exception:
        handle.close()
        raise


def get_text_data(elf):
    """Return the raw .text bytes for an ELF, or None if absent."""
    text_section = elf.get_section_by_name(".text")
    if text_section is None:
        return None
    return text_section.data()


def get_func_relocs(relocs, func_off, func_size):
    """Get relocations within a function, keyed by 4-byte instruction offset."""
    result = {}
    for abs_off, (rtype, sym_name) in relocs.items():
        if func_off <= abs_off < func_off + func_size:
            rel_off = (abs_off & ~3) - func_off
            result[rel_off] = {
                "sym_name": sym_name,
                "type": rtype,
            }
    return result


def apply_relocation_masks(our_word, orig_word, our_reloc, orig_reloc):
    """Mask relocated bits on both words using any relocation present."""
    for reloc in (our_reloc, orig_reloc):
        if reloc is None:
            continue
        our_word = mask_instruction(our_word, reloc["type"])
        orig_word = mask_instruction(orig_word, reloc["type"])
    return our_word, orig_word


def is_register_only_difference(our_word, orig_word):
    """Return True for conservative register-allocation-only differences."""
    if our_word == orig_word:
        return False

    our_opcode = (our_word >> 26) & 0x3F
    orig_opcode = (orig_word >> 26) & 0x3F
    if our_opcode != orig_opcode:
        return False

    if our_opcode not in SAFE_D_FORM_OPCODES:
        return False

    return ((our_word ^ orig_word) & ~REGISTER_DIFF_MASK_D_FORM) == 0


def compare_relocations(index, our_reloc, orig_reloc):
    """Compare relocation metadata for one instruction offset."""
    our_non_sda = (
        our_reloc is not None and our_reloc["type"] not in SDA_RELOC_TYPES
    )
    orig_non_sda = (
        orig_reloc is not None and orig_reloc["type"] not in SDA_RELOC_TYPES
    )

    if our_non_sda and orig_non_sda:
        if our_reloc["sym_name"] != orig_reloc["sym_name"]:
            return (
                f"  [{index}] WRONG TARGET: bl {our_reloc['sym_name']} "
                f"should be {orig_reloc['sym_name']}"
            )
        if our_reloc["type"] != orig_reloc["type"]:
            return (
                f"  [{index}] RELOC TYPE MISMATCH: "
                f"{our_reloc['type']} should be {orig_reloc['type']}"
            )
        return None

    if our_non_sda and not orig_non_sda:
        return (
            f"  [{index}] EXTRA RELOC: {our_reloc['sym_name']} "
            f"(not in original)"
        )

    if orig_non_sda and not our_non_sda:
        return (
            f"  [{index}] MISSING RELOC: should reference "
            f"{orig_reloc['sym_name']}"
        )

    return None


def verify_function(name, our_elf, orig_elf):
    """Verify a single function. Returns (ok, message)."""
    our_info = get_function_info(our_elf, name)
    if our_info is None:
        return False, "NOT FOUND in compiled .o"

    orig_info = get_function_info(orig_elf, name)
    if orig_info is None:
        return False, "NOT FOUND in original .o"

    our_text = get_text_data(our_elf)
    orig_text = get_text_data(orig_elf)
    if our_text is None:
        return False, "compiled .o is missing .text"
    if orig_text is None:
        return False, "original .o is missing .text"

    our_relocs = get_relocations(our_elf)
    orig_relocs = get_relocations(orig_elf)

    our_off, our_size = our_info
    orig_off, orig_size = orig_info

    if our_size != orig_size:
        return False, f"SIZE MISMATCH (ours={our_size}, orig={orig_size})"

    # Get relocations relative to function start.
    our_func_relocs = get_func_relocs(our_relocs, our_off, our_size)
    orig_func_relocs = get_func_relocs(orig_relocs, orig_off, orig_size)

    slot_count = (our_size + 3) // 4
    byte_mismatches = 0
    regswap_only = True
    reloc_mismatches = []

    for i in range(slot_count):
        rel_off = i * 4
        our_chunk = our_text[our_off + rel_off:our_off + rel_off + 4]
        orig_chunk = orig_text[orig_off + rel_off:orig_off + rel_off + 4]

        our_reloc = our_func_relocs.get(rel_off)
        orig_reloc = orig_func_relocs.get(rel_off)

        reloc_issue = compare_relocations(i, our_reloc, orig_reloc)
        if reloc_issue:
            reloc_mismatches.append(reloc_issue)

        if len(our_chunk) != len(orig_chunk):
            byte_mismatches += 1
            regswap_only = False
            continue

        if len(our_chunk) != 4:
            if our_chunk != orig_chunk:
                byte_mismatches += 1
                regswap_only = False
            continue

        our_word = struct.unpack(">I", our_chunk)[0]
        orig_word = struct.unpack(">I", orig_chunk)[0]

        our_word, orig_word = apply_relocation_masks(
            our_word,
            orig_word,
            our_reloc,
            orig_reloc,
        )

        if our_word != orig_word:
            byte_mismatches += 1
            if not is_register_only_difference(our_word, orig_word):
                regswap_only = False

    if reloc_mismatches:
        detail = "\n".join(reloc_mismatches)
        return (
            False,
            f"RELOC MISMATCH ({len(reloc_mismatches)} wrong targets):\n{detail}",
        )

    if byte_mismatches == 0:
        return True, f"100% match ({slot_count} instructions)"

    if regswap_only:
        pct = 100.0 * (slot_count - byte_mismatches) / slot_count
        return (
            True,
            f"{pct:.1f}% match ({byte_mismatches} regswaps, "
            f"{slot_count} instructions)",
        )

    pct = 100.0 * (slot_count - byte_mismatches) / slot_count
    return (
        False,
        f"{pct:.1f}% match ({byte_mismatches} mismatches, "
        f"{slot_count} instructions)",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Verify that compiled function bytes and relocation targets match "
            "the original object"
        )
    )
    parser.add_argument("compiled_o", help="Path to the compiled .o file")
    parser.add_argument("expected_o", help="Path to the original/split .o file")
    parser.add_argument("func_names", nargs="+", help="Function name(s) to verify")
    args = parser.parse_args()

    compiled_handle = None
    expected_handle = None
    try:
        compiled_handle, our_elf = load_elf(args.compiled_o)
        expected_handle, orig_elf = load_elf(args.expected_o)

        all_ok = True
        for name in args.func_names:
            ok, msg = verify_function(name, our_elf, orig_elf)
            status = "OK" if ok else "FAIL"
            print(f"  {name}: {status} - {msg}")
            if not ok:
                all_ok = False
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if compiled_handle is not None:
            compiled_handle.close()
        if expected_handle is not None:
            expected_handle.close()

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
