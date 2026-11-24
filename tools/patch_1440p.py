#!/usr/bin/env python3
"""Patch an MS2131 firmware dump so the card captures true 2560x1440 @ 30 fps over USB 3.0.

Usage:
    python patch_1440p.py <your_backup.bin> <patched_output.bin> [--no-sharpen]

  --no-sharpen   also turn off the chip's artificial sharpening filter in YUY2 mode
                 (recommended: at 1:1 it produces harsh, dotted edges)

The input must be a 64K dump of a firmware identical (in the patched places) to the one this
was developed on: build string "Jul 22 2026", code length 0xf86b, code checksum 0x9c72.
Every patched location is checked first; if anything differs the script aborts and writes
nothing, so it cannot silently produce a wrong image for a different firmware version.

What changes (file offset = ROM address + 0x30, the image starts with a 0x30-byte header):

  1. USB3 mode slot 12 (3840x2160 @ 18 fps) becomes 2560x1440 @ 30 fps
       - width/height tables for MJPEG and YUY2          file 0x018c, 0x01bc
       - the two UVC frame descriptors (YUY2 and MJPEG)   file 0x09f8, 0x0c21
  2. Maximum internal (frame memory) image size 1920x1080 -> 2560x1440
       - init record of XDATA 0x268c                      file 0x344a
  3. Frame memory line pitch chosen per mode instead of the fixed boot value
       - the end of the routine that programs the frame memory read size (ROM 0xc263) jumps
         to a new routine appended after the original code (ROM 0xf86b), which finishes the
         original writes and then sets registers 0xf597/0xf599
       - the code length in the header grows by the size of that routine
  4. (--no-sharpen) the stream setup code calls set_sharpen(1) for YUY2 and set_sharpen(0)
     for MJPEG; the "mov r7,#0x01" operand becomes 0x00          file 0x4ddc
  5. Header and code checksums

Why the pitch has to be dynamic
-------------------------------
The frame memory holds 2^22 pixels. Registers 0xf597/0xf599 set how many pixel slots each
line reserves, in units that depend on the input:

  - input pixel clock <= 170 MHz ("single-pixel" mode, e.g. 1080p60, or 4K60 4:2:0 which the
    chip halves at its input): widths are in pixels, the pitch is in units of 2 pixels
  - input pixel clock  > 170 MHz ("dual-pixel" mode, e.g. 2560x1440@60): widths are in units
    of 2 pixels, the pitch is in units of 4 pixels

The stock value 960 means 1920 px in single-pixel mode (exactly a 1080p line) and 3840 px
in dual-pixel mode, where it leaves room for only 1092 lines. A fixed 640 gives 2560 px and
1638 lines in dual-pixel mode, but only 1280 px in single-pixel mode, so 1080p-class inputs
overlap their own lines. The appended routine therefore picks:

    W = frame memory width as programmed (register 0xf660, in the mode's width unit)
    H = frame memory height (register 0xf662)
    if W > 1920:                          pitch = W / 2   (exact fit; wide single-pixel input)
    elif H > 1092 and dual-pixel mode:    pitch = 640     (2560 px, 1638 lines)
    else:                                 pitch = 960     (stock)
"""
import hashlib
import struct
import sys

HDR = 0x30
OLD_W, OLD_H, OLD_IV, OLD_FPS = 3840, 2160, 555555, 18
NEW_W, NEW_H, NEW_FPS = 2560, 1440, 30
NEW_IV = 10_000_000 // NEW_FPS            # 333333, same value the other 30 fps modes use
OLD_CODE_LEN = 0xF86B

WH_TABLES = [0x18C, 0x1BC]                # 12th entry of the USB3 tables (MJPEG, YUY2), big-endian
FRAME_DESCS = [0x9F8, 0xC21]              # VS_FRAME index 12 (YUY2, MJPEG) in the USB3 configuration
TIMING_INIT = 0x3305                      # C51 init record: 61 26 1a 76 + 14 records of 21 bytes
TIMING_COUNT = HDR + 0xF86A               # number of timing records (14)
MEMMAX_REC = 0x3447                       # C51 init record: 44 26 8c + W(be16) H(be16)
PITCH_INIT = HDR + 0xB0AF                 # boot-time init of 0xf597..0xf59a (left at the stock 960)
HOOK_ROM = 0xC263                         # mov a,r6 / inc dptr / movx @dptr,a / ret
DUALPIX_TEST = HDR + 0x5A8D               # the firmware's own "input pixel clock > 0x4268" test
SHARPEN_SITE = HDR + 0x4DA4               # ... anl a,#0xfd / movx / mov r7,#0x01 / lcall set_sharpen
SHARPEN_FUNC = HDR + 0xF136               # set_sharpen: bit 0 of register 0xf6b0


def pitch_routine(org):
    """Assemble the appended routine at ROM address `org`. Returns bytes.

    Entered from ROM 0xc263 with dptr = 0xf662 (height low byte already written), r6 = height
    high byte. Uses a, dptr, carry and r4..r7, which the only caller (ROM 0x5b28) reloads.
    """
    prog = [
        # finish what the replaced instructions did: 0xf663 = height high
        "ee", "a3", "f0",                         # mov a,r6 / inc dptr / movx @dptr,a
        # r4:r5 = frame memory width as programmed (0xf660 low, 0xf661 high)
        "90f660", "e0", "fd", "a3", "e0", "fc",   # mov dptr,#f660 / movx a,@dptr / mov r5,a / inc dptr / movx a,@dptr / mov r4,a
        # carry = (0x0780 < W)
        "c3", "7480", "9d", "7407", "9c",         # clr c / mov a,#80 / subb a,r5 / mov a,#07 / subb a,r4
        ("jnc", "notwide"),
        "ec", "c3", "13", "fc", "ed", "13", "fd",  # r4:r5 >>= 1
        ("sjmp", "write"),
        ("label", "notwide"),
        "7c03", "7dc0",                           # r4:r5 = 960 (stock pitch)
        "a3", "e0", "ff",                         # inc dptr (0xf662) / movx a,@dptr / mov r7,a : height low
        # carry = (0x0444 < H)
        "c3", "7444", "9f", "7404", "9e",         # clr c / mov a,#44 / subb a,r7 / mov a,#04 / subb a,r6
        ("jnc", "write"),
        # dual-pixel mode? same test the firmware uses at ROM 0x5a8d: carry = (pclk <= 0x4268)
        "d3", "9022bd", "e0", "9468", "9022bc", "e0", "9442",
        ("jc", "write"),
        "7c02", "7d80",                           # r4:r5 = 640
        ("label", "write"),
        "90f597",                                 # mov dptr,#f597
        "ed", "f0", "a3", "ec", "f0", "a3",       # 0xf597 = low, 0xf598 = high
        "ed", "f0", "a3", "ec", "f0",             # 0xf599 = low, 0xf59a = high
        "22",                                     # ret
    ]
    op = {"jnc": 0x50, "jc": 0x40, "sjmp": 0x80}
    labels, pc = {}, org
    for item in prog:                             # pass 1: label addresses
        if isinstance(item, tuple):
            if item[0] == "label":
                labels[item[1]] = pc
            else:
                pc += 2
        else:
            pc += len(item) // 2
    out, pc = bytearray(), org
    for item in prog:                             # pass 2: emit
        if isinstance(item, tuple):
            if item[0] == "label":
                continue
            rel = labels[item[1]] - (pc + 2)
            assert -128 <= rel <= 127
            out += bytes([op[item[0]], rel & 0xFF])
            pc += 2
        else:
            b = bytes.fromhex(item)
            out += b
            pc += len(b)
    return bytes(out)


def check(cond, msg):
    if not cond:
        raise SystemExit(f"ABORT: {msg}\nThis firmware is not the version this patch was written for. Nothing written.")


def header_checksum(fw):
    return sum(b for i, b in enumerate(fw[:HDR]) if i >= 2 and not (0x0C <= i <= 0x0F)) & 0xFFFF


def code_checksum(fw, end):
    return sum(fw[HDR:end]) & 0xFFFF


def build(orig, no_sharpen):
    """Return the patched 64K image for the 64K dump `orig`."""
    fw = bytearray(orig)
    check(len(fw) == 0x10000, "input must be a dump of at most 64K")
    check(fw[0] == 0x3C and fw[1] == 0xC3, "3C C3 signature missing")
    check(((fw[2] << 8) | fw[3]) == OLD_CODE_LEN, "unexpected code length")
    end = HDR + OLD_CODE_LEN
    check((fw[end] << 8 | fw[end + 1]) == header_checksum(fw), "header checksum of the input is invalid")
    check((fw[end + 2] << 8 | fw[end + 3]) == code_checksum(fw, end), "code checksum of the input is invalid")
    check(all(b == 0xFF for b in fw[end + 4:]), "unexpected data after the firmware")

    # The firmware must already contain a 2560x1440 timing record: it picks the output timing
    # by looking the requested width x height up in this table.
    check(bytes(fw[TIMING_INIT:TIMING_INIT + 4]) == bytes([0x61, 0x26, 0x1A, 0x76]), "timing table header not found")
    table = fw[TIMING_INIT + 4:TIMING_INIT + 4 + 14 * 21]
    sizes = [struct.unpack(">HH", table[i * 21 + 5:i * 21 + 9]) for i in range(14)]
    check((NEW_W, NEW_H) in sizes, "no 2560x1440 timing record in this firmware")
    check(fw[TIMING_COUNT] == 14, "unexpected timing record count")

    # 1a. width/height tables
    for off in WH_TABLES:
        check(struct.unpack_from(">HH", fw, off) == (OLD_W, OLD_H), f"W/H table entry at {off:#x} is not 3840x2160")
        struct.pack_into(">HH", fw, off, NEW_W, NEW_H)

    # 1b. UVC frame descriptors (same length, one discrete frame interval)
    fmt = "<BBBBBHHIIIIBI"
    for off in FRAME_DESCS:
        ln, typ, sub, idx, caps, w, h, minbr, maxbr, maxbuf, defiv, ntype, iv = struct.unpack_from(fmt, fw, off)
        check((ln, typ, idx, w, h, ntype, iv, defiv) == (30, 0x24, 12, OLD_W, OLD_H, 1, OLD_IV, OLD_IV)
              and sub in (5, 7) and minbr == maxbr == OLD_W * OLD_H * 16 * OLD_FPS and maxbuf == OLD_W * OLD_H * 2,
              f"frame descriptor at {off:#x} is not the expected 3840x2160@18 one")
        br = NEW_W * NEW_H * 16 * NEW_FPS
        struct.pack_into(fmt, fw, off, ln, typ, sub, idx, caps, NEW_W, NEW_H, br, br, NEW_W * NEW_H * 2,
                         NEW_IV, 1, NEW_IV)

    # 2. maximum internal image size
    check(bytes(fw[MEMMAX_REC:MEMMAX_REC + 7]) == bytes([0x44, 0x26, 0x8C, 0x07, 0x80, 0x04, 0x38]),
          "internal size limit init record is not 1920x1080")
    struct.pack_into(">HH", fw, MEMMAX_REC + 3, NEW_W, NEW_H)

    # 3. dynamic frame memory line pitch
    check(bytes(fw[PITCH_INIT:PITCH_INIT + 18]) == bytes.fromhex("90f597" "74c0f0" "a3" "7403f0" "a3" "74c0f0" "a3" "7403f0"),
          "frame memory init sequence not found")
    hook = HDR + HOOK_ROM
    check(bytes(fw[hook - 4:hook + 4]) == bytes.fromhex("90f662" "f0" "ee" "a3" "f0" "22"),
          "frame memory size routine tail not found")
    check(bytes(fw[DUALPIX_TEST:DUALPIX_TEST + 15]) == bytes.fromhex("d3" "9022bd" "e0" "9468" "9022bc" "e0" "9442" "4006"),
          "dual-pixel mode test not found")
    routine = pitch_routine(OLD_CODE_LEN)
    fw[hook:hook + 3] = bytes([0x02, OLD_CODE_LEN >> 8, OLD_CODE_LEN & 0xFF])      # ljmp routine
    fw[end:end + len(routine)] = routine          # overwrites the old checksum bytes too
    new_len = OLD_CODE_LEN + len(routine)
    fw[2], fw[3] = new_len >> 8, new_len & 0xFF
    end = HDR + new_len
    check(end + 4 <= 0x10000, "no room for the appended routine")

    # 4. optional: never enable the sharpening filter
    if no_sharpen:
        check(bytes(fw[SHARPEN_SITE:SHARPEN_SITE + 12]) == bytes.fromhex("90f04c" "e0" "54fd" "f0" "7f01" "12f136")
              and bytes(fw[SHARPEN_FUNC:SHARPEN_FUNC + 11]) == bytes.fromhex("ab07" "7d01" "7fb0" "7ef6" "02e9e0"),
              "sharpening enable code not found")
        fw[SHARPEN_SITE + 8] = 0x00

    # 5. checksums
    h, c = header_checksum(fw), code_checksum(fw, end)
    fw[end:end + 4] = bytes([h >> 8, h & 0xFF, c >> 8, c & 0xFF])
    return bytes(fw), len(routine)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_sharpen = "--no-sharpen" in sys.argv
    if len(args) != 2:
        raise SystemExit(__doc__)
    src, dst = args
    orig = open(src, "rb").read()
    orig += b"\xff" * (0x10000 - len(orig))
    fw, n = build(orig, no_sharpen)
    diff = sum(1 for a, b in zip(orig, fw) if a != b)
    print(f"input : sha256 {hashlib.sha256(orig).hexdigest()}")
    print(f"output: sha256 {hashlib.sha256(fw).hexdigest()}")
    print(f"{diff} bytes differ ({n} of them are the appended routine); code length "
          f"{OLD_CODE_LEN:#x} -> {(fw[2] << 8) | fw[3]:#x}")
    with open(dst, "xb") as f:
        f.write(fw)
    print(f"written: {dst}")


if __name__ == "__main__":
    main()
