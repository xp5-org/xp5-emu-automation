"""Build a self-extracting zx02 variant of a cc65 C64 program.

This is the generic, reusable form of the one-off script written for
sourcedir/c64src/hires_h_scroll_zx02/. Given the same src_dir/archtype/
linkerconf a normal test_compiletheprogram build would use, it produces a
*parallel* PRG+D64 pair: the program compressed with the host zx02 tool,
prefixed with a small ca65 stub (pyhelpers/zx02_assets/loader.s) that
unpacks it back to its real addresses and jumps in. See loader.s for the
decompression-safety argument (why the compressed blob has to be placed
fully below the program's own footprint, not just "before" it).

Steps, mirroring test_compiletheprogram's own compile logic so the two stay
in sync:

  1. Assemble a scratch copy of loader.s/loader.cfg once to learn where its
     CODE segment (and so comp_data) will load -- fixed size, independent
     of the payload.
  2. Take the target's linker cfg (the caller's linkerconf, or cc65's
     default {archtype}.cfg if none) and rewrite its `STARTADDRESS`
     FEATURES line to some candidate address above comp_data.
  3. Compile+link every .c/.s in src_dir against that cfg (same glob logic
     as test_compiletheprogram: cc65+ca65 per .c file, ca65 per .s file,
     one ld65 link), strip cc65's own 2-byte load-address prefix, and
     zx02-compress the rest.
  4. If the compressed blob's end still reaches into the write range,
     push the candidate start above it and repeat -- the compressed size
     depends slightly on the addresses compiled in, so this can take a
     couple of passes to settle.
  5. Patch the scratch loader.s with the final TARGET_START/ENTRY_ADDR
     (the payload's own __HEADER_LAST__, not a fixed offset -- cc65 doesn't
     always spend its full EXEHDR reservation), embed the compressed blob,
     assemble the final combined PRG, and write it to a fresh D64.

Supports c64, c128, pet and vic20. The loader's BASIC line and MEMORY
layout are read from the target's own base cfg (its `STARTADDRESS`
FEATURES override if it has one, e.g. c64.cfg and this repo's custom
c64-*.cfg files; otherwise the literal `HEADER: ... start = $....` in its
MEMORY block, which is how cc65's stock c128.cfg/pet.cfg/vic20.cfg declare
it) rather than assumed, so the loader always lands its BASIC line where
that machine's LOAD/RUN actually expects one. Not supported: any archtype
this repo's ViceInstance doesn't know how to launch (only c64/c128/pet/
vic20 map to an emulator binary), and any cfg that expresses its load
address neither way.

VIC-20 caveat: unexpanded VIC-20 has only ~2.5KB free between BASIC_START
and HIMEM (cc65's vic20.cfg: MAIN size = $0DF3 minus a 1KB stack). The
disjoint-ranges requirement above pushes the decompressed program's own
footprint further up into that same small window, so this can fail to
converge (or leave no headroom at all) for anything beyond a small
program -- it's a real memory ceiling, not a bug to work around here.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

from vicehelpers import (
    compile_cc65, assemble_ca65, create_blank_d64, format_and_copyd64,
)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zx02_assets")
LOADER_S_TEMPLATE = os.path.join(ASSETS_DIR, "loader.s")
LOADER_CFG_TEMPLATE = os.path.join(ASSETS_DIR, "loader.cfg")
INPLACE_S_TEMPLATE = os.path.join(ASSETS_DIR, "loader_inplace.s")
INPLACE_CFG_TEMPLATE = os.path.join(ASSETS_DIR, "loader_inplace.cfg")
ZX02_BIN = "/testsrc/zx02"
CC65_LIB = "/usr/share/cc65/lib"
CC65_CFG_DIR = "/usr/share/cc65/cfg"

TARGET_START_SET_RE = re.compile(r"(TARGET_START\s*=\s*)\$[0-9A-Fa-f]+")
COMP_DST_SET_RE = re.compile(r"(COMP_DST\s*=\s*)\$[0-9A-Fa-f]+")
COMP_LEN_SET_RE = re.compile(r"(COMP_LEN\s*=\s*)\$[0-9A-Fa-f]+")
# zx02's own report line: "File compressed from 51199 to 14473 bytes! (delta 8)"
DELTA_RE = re.compile(r"\(delta\s+(\d+)\)")
ENTRY_ADDR_SET_RE = re.compile(r"(ENTRY_ADDR\s*=\s*)\$[0-9A-Fa-f]+")
BASIC_START_SET_RE = re.compile(r"(BASIC_START\s*=\s*)\$[0-9A-Fa-f]+")
# loader.s's SYS line: `.byte " 2062", $00` -- the digits are this
# machine's BASIC_START+13 in decimal, zero-padded to 4 (see loader.s).
SYS_TEXT_SET_RE = re.compile(r'(\.byte\s+" )\d{4}(", \$00)')
STARTADDRESS_RE = re.compile(r"STARTADDRESS:\s*default\s*=\s*\$[0-9A-Fa-f]+;")
HEADER_START_RE = re.compile(r"HEADER:[^\n]*?start\s*=\s*\$([0-9A-Fa-f]+)")

SUPPORTED_ARCHTYPES = {"c64", "c128", "pet", "vic20"}


class Zx02BuildError(RuntimeError):
    pass


def _run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise Zx02BuildError(f"command failed: {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout


def _patch(path, pattern, replacement, required=True):
    with open(path) as f:
        text = f.read()
    new_text, n = pattern.subn(replacement, text)
    if required and n != 1:
        raise Zx02BuildError(f"pattern {pattern.pattern!r} not found in {path}")
    if new_text != text:
        with open(path, "w") as f:
            f.write(new_text)


def _segment_bounds(mapfile, segment_name):
    """(start, end) for segment_name from an ld65 map's Segment list.
    end is the last occupied byte (ld65's own convention -- inclusive)."""
    start = end = None
    in_segment_list = False
    with open(mapfile) as f:
        for line in f:
            if line.startswith("Segment list:"):
                in_segment_list = True
                continue
            if not in_segment_list:
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[0] == segment_name:
                start = int(parts[1], 16)
                end = int(parts[2], 16)
                break
    if start is None:
        raise Zx02BuildError(f"could not find segment {segment_name!r} in {mapfile}")
    return start, end


def _hiram_start(loader_cfg):
    """Start of the memory area loader_inplace.s's HICODE runs from -- the
    ceiling both the decompressed image and the moved blob have to stay
    under, since the loader is executing up there while they land."""
    with open(loader_cfg) as f:
        m = re.search(r"HIRAM:[^\n]*?start\s*=\s*\$([0-9A-Fa-f]+)", f.read())
    if not m:
        raise Zx02BuildError(f"no HIRAM area in {loader_cfg}")
    return int(m.group(1), 16)


def _assemble_stub(loader_s, loader_cfg, scratch, prg_name, archtype, segment=None):
    """Assemble loader_s (whatever comp.bin currently sits beside it) and
    return (comp_data_addr, prg_size) from the map file's Segment list.

    segment names the one comp_data starts: RODATA for a stub that has other
    segments after CODE (loader_inplace.s puts HICODE between them). Left
    None, comp_data is taken as the byte after CODE, which is what loader.s's
    simpler layout gives."""
    obj = os.path.join(scratch, os.path.splitext(prg_name)[0] + ".o")
    prg = os.path.join(scratch, prg_name)
    mapfile = os.path.join(scratch, os.path.splitext(prg_name)[0] + "_map.txt")
    loader_dir = os.path.dirname(loader_s)
    _run(["ca65", "-t", archtype, "-I", loader_dir, "--bin-include-dir", loader_dir,
          "-o", obj, loader_s])
    _run(["ld65", "-C", loader_cfg, "-o", prg, "-m", mapfile, obj])

    if segment:
        start, _ = _segment_bounds(mapfile, segment)
        return start, os.path.getsize(prg)
    _, code_end = _segment_bounds(mapfile, "CODE")
    # comp_data is the first byte of RODATA, i.e. one past CODE's last byte.
    return code_end + 1, os.path.getsize(prg)


def _get_base_start(cfg_text):
    """This machine's BASIC load address, straight from its own cfg.

    Prefer a `STARTADDRESS: default = $....;` FEATURES override (c64.cfg
    and this repo's custom c64-*.cfg files all use one) -- it's an
    explicit, intentional override point. Stock c128.cfg/pet.cfg/vic20.cfg
    have no such override; they hardcode the address directly as
    `HEADER: ... start = $....` in MEMORY, so fall back to reading that."""
    m = STARTADDRESS_RE.search(cfg_text)
    if m:
        return int(re.search(r"\$([0-9A-Fa-f]+)", m.group(0)).group(1), 16)
    m = HEADER_START_RE.search(cfg_text)
    if m:
        return int(m.group(1), 16)
    raise Zx02BuildError(
        "cfg has neither a STARTADDRESS override nor a literal HEADER start -- "
        "can't determine this machine's BASIC load address"
    )


def _third_area_name(cfg_text):
    """The MEMORY area STARTUP loads into -- named MAIN in c64/c128/vic20's
    stock cfgs, RAM in pet.cfg. Whatever it's called, it's the area whose
    start= needs to move in step with LOADADDR/HEADER."""
    m = re.search(r"STARTUP:\s*load\s*=\s*(\w+)", cfg_text)
    if not m:
        raise Zx02BuildError("could not find which MEMORY area the STARTUP segment loads into")
    return m.group(1)


def _write_shifted_cfg(base_cfg_text, base_start, target_start, scratch):
    """Return a cfg text with the program's load address moved to
    target_start, preserving everything else (VIC bank choices, custom
    segments, stack size, ...)."""
    if STARTADDRESS_RE.search(base_cfg_text):
        return STARTADDRESS_RE.sub(f"STARTADDRESS: default = ${target_start:04X};", base_cfg_text)

    # No override: shift LOADADDR/HEADER/<third area>'s literal start=
    # addresses by the same delta, so their relative offsets (LOADADDR =
    # HEADER-2, third area = HEADER+size) stay correct without needing to
    # know that size.
    delta = target_start - base_start
    new_cfg = base_cfg_text
    for label in ("LOADADDR", "HEADER", _third_area_name(base_cfg_text)):
        pattern = re.compile(rf"({label}:[^\n]*?start\s*=\s*)\$([0-9A-Fa-f]+)")

        def _shift(m, _delta=delta):
            return f"{m.group(1)}${int(m.group(2), 16) + _delta:04X}"

        new_cfg, n = pattern.subn(_shift, new_cfg)
        if n != 1:
            raise Zx02BuildError(f"could not find/shift {label} start= in cfg")
    return new_cfg


def _build_payload(src_dir, archtype, cfg_path, scratch):
    """Compile+link every .c/.s in src_dir, mirroring
    test_compiletheprogram's glob logic. Returns (prg_path, entry_addr)."""
    import glob

    link_objects = []
    for src_c in sorted(glob.glob(os.path.join(src_dir, "*.c"))):
        base = os.path.splitext(os.path.basename(src_c))[0]
        asm_out = os.path.join(scratch, f"{base}.s")
        obj = os.path.join(scratch, f"{base}.o")
        compile_cc65(src_c, asm_out, archtype, optimize=True)
        assemble_ca65(asm_out, obj, archtype)
        link_objects.append(obj)

    for src_s in sorted(glob.glob(os.path.join(src_dir, "*.s"))):
        base = os.path.splitext(os.path.basename(src_s))[0]
        obj = os.path.join(scratch, f"{base}_asm.o")
        assemble_ca65(src_s, obj, archtype)
        link_objects.append(obj)

    if not link_objects:
        raise Zx02BuildError(f"no .c or .s files found in {src_dir}")

    prg = os.path.join(scratch, "payload.prg")
    mapfile = os.path.join(scratch, "payload_map.txt")
    # Not vicehelpers.link_ld65: it doesn't take a -m flag, and the map file
    # (for STARTUP's start below) is the whole reason to link here at all.
    _run(["ld65", "-L", CC65_LIB, "-C", cfg_path, "-o", prg, "-m", mapfile,
          *link_objects, f"{archtype}.lib"])

    # STARTUP is always the first segment cc65 places in MAIN, right after
    # EXEHDR -- true on every target's cfg checked so far (c64/c128/pet/
    # vic20), and unlike __HEADER_LAST__ it doesn't depend on the cfg
    # explicitly requesting a linker-generated symbol (stock c128/pet/
    # vic20 cfgs don't; only c64.cfg-style ones do). Its start is exactly
    # the entry point: the real byte count cc65's exehdr.s used, not
    # whatever the cfg's own HEADER size *reserves* -- see loader.s's
    # ENTRY_ADDR note for why that reservation isn't reliable as-is.
    entry_addr, _ = _segment_bounds(mapfile, "STARTUP")
    return prg, entry_addr


def _sys_target(body):
    """The address cc65's own EXEHDR SYSes, read straight out of a built PRG
    body: [link][line#][$9E token][decimal digits][$00].

    This is the same number __HEADER_LAST__/STARTUP's start would give, but
    without a linker map -- and in-place mode deliberately never relinks the
    payload, so there is no map of its own to read. Anything else would mean
    rebuilding the program just to learn where it starts."""
    tok = body.find(b"\x9e", 0, 16)
    if tok < 0:
        raise Zx02BuildError("payload PRG has no BASIC SYS token in its first 16 bytes -- "
                             "it was not built with cc65's EXEHDR, so its entry point "
                             "cannot be recovered without a linker map")
    digits = b""
    for ch in body[tok + 1:tok + 12]:
        if 0x30 <= ch <= 0x39:
            digits += bytes([ch])
        elif digits:
            break
    if not digits:
        raise Zx02BuildError("payload PRG's BASIC SYS line has no digits after the token")
    return int(digits)


def build_zx02_inplace_variant(prg_in, out_dir, archtype, prg_out, d64_out,
                               linker_cfg=None):
    """Wrap an ALREADY-BUILT PRG in the in-place self-extracting loader.

    Unlike build_zx02_variant this does not compile or relink anything: it
    takes the PRG test_compiletheprogram already produced, compresses it
    whole, and prefixes loader_inplace.s. That is the point -- a program whose
    linker cfg pins segments to absolute addresses cannot be relinked to a
    shifted start, which is what the low-placement mode requires.

    Returns (success, log_text)."""
    log = []
    if archtype not in SUPPORTED_ARCHTYPES:
        return False, f"zx02build: archtype {archtype!r} not supported (only {sorted(SUPPORTED_ARCHTYPES)})"
    if not os.path.exists(prg_in):
        return False, f"zx02build: input PRG not found: {prg_in}"

    base_cfg = linker_cfg if linker_cfg else os.path.join(CC65_CFG_DIR, f"{archtype}.cfg")
    if not os.path.exists(base_cfg):
        return False, f"zx02build: linker cfg not found: {base_cfg}"
    with open(base_cfg) as f:
        base_cfg_text = f.read()

    try:
        basic_start = _get_base_start(base_cfg_text)
    except Zx02BuildError as e:
        return False, f"zx02build failed: {e}"
    sys_target = basic_start + 13
    if sys_target > 9999:
        return False, (f"zx02build: BASIC_START ${basic_start:04X} puts the SYS target "
                       f"({sys_target}) over 4 digits -- loader_inplace.s's fixed-width "
                       "SYS line needs widening for this machine")

    with open(prg_in, "rb") as f:
        data = f.read()
    target_start = data[0] | (data[1] << 8)
    body = data[2:]
    raw_size = len(body)
    target_end = target_start + raw_size - 1

    scratch = os.path.join(out_dir, "zx02_inplace_scratch")
    os.makedirs(scratch, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    loader_s = os.path.join(scratch, "loader_inplace.s")
    loader_cfg = os.path.join(scratch, "loader_inplace.cfg")
    shutil.copyfile(INPLACE_S_TEMPLATE, loader_s)
    shutil.copyfile(INPLACE_CFG_TEMPLATE, loader_cfg)
    _patch(loader_s, BASIC_START_SET_RE, rf"\g<1>${basic_start:04X}")
    _patch(loader_s, SYS_TEXT_SET_RE, rf"\g<1>{sys_target:04d}\g<2>")
    for label, addr in (("LOADADDR", basic_start - 2), ("HEADER", basic_start),
                        ("MAIN", basic_start + 13)):
        _patch(loader_cfg, re.compile(rf"({label}:[^\n]*?start\s*=\s*)\$[0-9A-Fa-f]+"),
               rf"\g<1>${addr:04X}")

    try:
        entry_addr = _sys_target(body)
        log.append(f"payload ${target_start:04X}-${target_end:04X} ({raw_size}B), "
                   f"entry ${entry_addr:04X}, BASIC_START=${basic_start:04X}")

        body_path = os.path.join(scratch, "payload_body.bin")
        with open(body_path, "wb") as f:
            f.write(body)
        comp_bin = os.path.join(scratch, "comp.bin")

        # zx02 is an optimal-parse compressor and takes minutes on a payload
        # this size, which it would otherwise pay on every single run even
        # when nothing about the program changed. Key a cache on the digest of
        # exactly what gets compressed; delta has to be cached with it, since
        # it is only ever printed on the compression run.
        digest = hashlib.sha256(body).hexdigest()
        stamp = os.path.join(scratch, "comp.stamp")
        delta = None
        if os.path.exists(comp_bin) and os.path.exists(stamp):
            try:
                with open(stamp) as f:
                    cached = json.load(f)
                if (cached.get("sha256") == digest
                        and cached.get("comp_size") == os.path.getsize(comp_bin)):
                    delta = int(cached["delta"])
                    log.append(f"reusing cached comp.bin ({digest[:12]}), skipping zx02")
            except (ValueError, KeyError, OSError):
                delta = None                     # unreadable stamp: recompress

        if delta is None:
            # No -q: the delta this mode depends on is only on that report line.
            zx_out = _run([ZX02_BIN, "-f", body_path, comp_bin])
            m = DELTA_RE.search(zx_out)
            if not m:
                raise Zx02BuildError(f"could not read zx02's delta from its output:\n{zx_out}")
            delta = int(m.group(1))
            with open(stamp, "w") as f:
                json.dump({"sha256": digest, "delta": delta,
                           "comp_size": os.path.getsize(comp_bin)}, f)

        comp_size = os.path.getsize(comp_bin)

        comp_dst = target_start + raw_size - comp_size + delta
        blob_end = comp_dst + comp_size - 1
        log.append(f"compressed {raw_size} -> {comp_size}B ({100 * comp_size // raw_size}%), "
                   f"delta {delta}; blob moved to ${comp_dst:04X}-${blob_end:04X}")

        hiram_start = _hiram_start(loader_cfg)
        if target_end >= hiram_start:
            raise Zx02BuildError(
                f"payload ends at ${target_end:04X}, at or above the relocated loader at "
                f"${hiram_start:04X} -- decompression would overwrite the loader mid-run")
        if blob_end >= hiram_start:
            raise Zx02BuildError(
                f"compressed blob would end at ${blob_end:04X}, at or above the relocated "
                f"loader at ${hiram_start:04X} -- moving it up would overwrite the loader")
        if blob_end > 0xFFFF:
            raise Zx02BuildError(f"compressed blob would end at ${blob_end:04X}, past the top of RAM")

        _patch(loader_s, TARGET_START_SET_RE, rf"\g<1>${target_start:04X}")
        _patch(loader_s, ENTRY_ADDR_SET_RE, rf"\g<1>${entry_addr:04X}")
        _patch(loader_s, COMP_DST_SET_RE, rf"\g<1>${comp_dst:04X}")
        _patch(loader_s, COMP_LEN_SET_RE, rf"\g<1>${comp_size:04X}")

        final_prg = os.path.join(scratch, "final.prg")
        comp_addr, final_size = _assemble_stub(loader_s, loader_cfg, scratch, "final.prg",
                                               archtype, segment="RODATA")
        # The runtime move copies downward from the top of the block, which is
        # only correct while the destination is above the source.
        if comp_dst <= comp_addr:
            raise Zx02BuildError(
                f"blob loads at ${comp_addr:04X} but would move to ${comp_dst:04X} -- the "
                "descending move in loader_inplace.s requires the destination above the source")
        log.append(f"blob loads at ${comp_addr:04X}, moves up {comp_dst - comp_addr} bytes")

        os.makedirs(os.path.dirname(prg_out), exist_ok=True)
        shutil.copyfile(final_prg, prg_out)

        success, out = create_blank_d64(d64_out)
        if not success:
            return False, "\n".join(log + [f"create_blank_d64 failed:\n{out}"])
        success, out = format_and_copyd64(d64_out, prg_out)
        if not success:
            return False, "\n".join(log + [f"format_and_copyd64 failed:\n{out}"])

        log.append(f"original PRG: {len(data)} bytes")
        log.append(f"final self-extracting PRG: {final_size} bytes "
                   f"({100 * final_size // len(data)}% of original)")
        log.append(f"ARTIFACT: {prg_out}")
        log.append(f"ARTIFACT: {d64_out}")
        return True, "\n".join(log)

    except Zx02BuildError as e:
        return False, "\n".join(log + [f"zx02build failed: {e}"])


def build_zx02_variant(src_dir, out_dir, archtype, prg_out, d64_out,
                        linker_cfg=None, margin_pad=16, max_passes=6):
    """Build a self-extracting zx02 PRG+D64 pair from src_dir.

    linker_cfg: same meaning as test_compiletheprogram's linkerconf -- a
    custom cc65 cfg using a `STARTADDRESS` FEATURES override, or None/""
    for cc65's stock {archtype}.cfg.

    Returns (success, log_text). Raises nothing on ordinary build failures
    (those come back as success=False); a Zx02BuildError escaping this
    function means something about the environment/inputs is wrong in a
    way retrying won't fix.
    """
    log = []
    if archtype not in SUPPORTED_ARCHTYPES:
        return False, (f"zx02build: archtype {archtype!r} not supported "
                        f"(only {sorted(SUPPORTED_ARCHTYPES)} -- the loader stub's BASIC "
                        "header and memory layout are C64-specific)")

    if not os.path.isdir(src_dir):
        return False, f"zx02build: src_dir not found: {src_dir}"

    base_cfg = linker_cfg if linker_cfg else os.path.join(CC65_CFG_DIR, f"{archtype}.cfg")
    if not os.path.exists(base_cfg):
        return False, f"zx02build: linker cfg not found: {base_cfg}"
    with open(base_cfg) as f:
        base_cfg_text = f.read()

    try:
        basic_start = _get_base_start(base_cfg_text)
    except Zx02BuildError as e:
        return False, f"zx02build failed: {e}"
    sys_target = basic_start + 13  # our own 13-byte header, always -- see loader.s
    if sys_target > 9999:
        return False, (f"zx02build: BASIC_START ${basic_start:04X} puts the SYS target "
                       f"({sys_target}) over 4 digits -- loader.s's fixed-width SYS line "
                       "needs widening for this machine")

    scratch = os.path.join(out_dir, "zx02_scratch")
    os.makedirs(scratch, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    # Per-run scratch copies -- never patch the checked-in template, or
    # concurrent/other-project runs would race on the same file. BASIC_START
    # and the SYS line text are fixed for this machine regardless of
    # TARGET_START, so patch those once, up front.
    loader_s = os.path.join(scratch, "loader.s")
    loader_cfg = os.path.join(scratch, "loader.cfg")
    shutil.copyfile(LOADER_S_TEMPLATE, loader_s)
    shutil.copyfile(LOADER_CFG_TEMPLATE, loader_cfg)
    _patch(loader_s, BASIC_START_SET_RE, rf"\g<1>${basic_start:04X}")
    _patch(loader_s, SYS_TEXT_SET_RE, rf"\g<1>{sys_target:04d}\g<2>")
    for label, addr in (("LOADADDR", basic_start - 2), ("HEADER", basic_start), ("MAIN", basic_start + 13)):
        _patch(loader_cfg, re.compile(rf"({label}:[^\n]*?start\s*=\s*)\$[0-9A-Fa-f]+"),
               rf"\g<1>${addr:04X}")
    log.append(f"BASIC_START=${basic_start:04X} (from {base_cfg}), SYS target={sys_target}")

    try:
        comp_bin = os.path.join(scratch, "comp.bin")
        if not os.path.exists(comp_bin):
            with open(comp_bin, "wb") as f:
                f.write(b"\x00")
        comp_data_addr, _ = _assemble_stub(loader_s, loader_cfg, scratch, "loader_probe.prg", archtype)
        log.append(f"comp_data loads at ${comp_data_addr:04X} (stub end)")

        target_start = comp_data_addr + margin_pad
        payload_prg = entry_addr = body = comp_size = None
        for i in range(max_passes):
            shifted_text = _write_shifted_cfg(base_cfg_text, basic_start, target_start, scratch)
            cfg_path = os.path.join(scratch, "shifted.cfg")
            with open(cfg_path, "w") as f:
                f.write(shifted_text)
            payload_prg, entry_addr = _build_payload(src_dir, archtype, cfg_path, scratch)
            with open(payload_prg, "rb") as f:
                data = f.read()
            body = data[2:]  # drop cc65's own load-address prefix
            body_path = os.path.join(scratch, "payload_body.bin")
            with open(body_path, "wb") as f:
                f.write(body)

            _run([ZX02_BIN, "-f", body_path, comp_bin])
            comp_size = os.path.getsize(comp_bin)
            comp_data_end = comp_data_addr + comp_size

            log.append(f"[pass {i}] TARGET_START=${target_start:04X} payload={len(data)}B "
                       f"comp={comp_size}B ({100 * comp_size // len(body)}%) "
                       f"comp_data_end=${comp_data_end:04X}")

            if target_start >= comp_data_end:
                break
            target_start = comp_data_end + margin_pad
        else:
            return False, "\n".join(log + [
                f"TARGET_START did not converge above comp_data_end after {max_passes} passes"])

        log.append(f"write range [{target_start:04X}, {target_start+len(body):04X}) vs "
                   f"read range [{comp_data_addr:04X}, {comp_data_addr+comp_size:04X}) -- disjoint")

        _patch(loader_s, TARGET_START_SET_RE, rf"\g<1>${target_start:04X}")
        _patch(loader_s, ENTRY_ADDR_SET_RE, rf"\g<1>${entry_addr:04X}")

        final_prg = os.path.join(scratch, "final.prg")
        _, final_size = _assemble_stub(loader_s, loader_cfg, scratch, "final.prg", archtype)
        # _assemble_stub always builds into scratch/<name>; move to the real
        # output path.
        os.makedirs(os.path.dirname(prg_out), exist_ok=True)
        shutil.copyfile(final_prg, prg_out)

        success, out = create_blank_d64(d64_out)
        if not success:
            return False, "\n".join(log + [f"create_blank_d64 failed:\n{out}"])
        success, out = format_and_copyd64(d64_out, prg_out)
        if not success:
            return False, "\n".join(log + [f"format_and_copyd64 failed:\n{out}"])

        orig_size = os.path.getsize(payload_prg)
        log.append(f"original uncompressed PRG: {orig_size} bytes")
        log.append(f"final self-extracting PRG: {final_size} bytes "
                   f"({100 * final_size // orig_size}% of original)")
        log.append(f"ARTIFACT: {prg_out}")
        log.append(f"ARTIFACT: {d64_out}")
        return True, "\n".join(log)

    except Zx02BuildError as e:
        return False, "\n".join(log + [f"zx02build failed: {e}"])
