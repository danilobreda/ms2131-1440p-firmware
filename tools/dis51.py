#!/usr/bin/env python3
"""Minimal 8051 disassembler used to study the firmware.

Usage: python dis51.py <firmware.bin> <rom_start_hex> <rom_end_hex>
ROM address = file offset - 0x30 (the image starts with a 0x30-byte header).
"""
import sys

HDR = 0x30
R = [f"r{i}" for i in range(8)]


def build():
    t = {}

    def op(code, fmt, size=1):
        t[code] = (fmt, size)

    op(0x00, "nop"); op(0x02, "ljmp {a16}", 3); op(0x03, "rr a"); op(0x04, "inc a"); op(0x05, "inc {d}", 2)
    op(0x06, "inc @r0"); op(0x07, "inc @r1")
    op(0x10, "jbc {b},{rel}", 3); op(0x12, "lcall {a16}", 3); op(0x13, "rrc a"); op(0x14, "dec a")
    op(0x15, "dec {d}", 2); op(0x16, "dec @r0"); op(0x17, "dec @r1")
    op(0x20, "jb {b},{rel}", 3); op(0x22, "ret"); op(0x23, "rl a"); op(0x24, "add a,#{i}", 2)
    op(0x25, "add a,{d}", 2); op(0x26, "add a,@r0"); op(0x27, "add a,@r1")
    op(0x30, "jnb {b},{rel}", 3); op(0x32, "reti"); op(0x33, "rlc a"); op(0x34, "addc a,#{i}", 2)
    op(0x35, "addc a,{d}", 2); op(0x36, "addc a,@r0"); op(0x37, "addc a,@r1")
    op(0x40, "jc {rel}", 2); op(0x42, "orl {d},a", 2); op(0x43, "orl {d},#{i}", 3); op(0x44, "orl a,#{i}", 2)
    op(0x45, "orl a,{d}", 2); op(0x46, "orl a,@r0"); op(0x47, "orl a,@r1")
    op(0x50, "jnc {rel}", 2); op(0x52, "anl {d},a", 2); op(0x53, "anl {d},#{i}", 3); op(0x54, "anl a,#{i}", 2)
    op(0x55, "anl a,{d}", 2); op(0x56, "anl a,@r0"); op(0x57, "anl a,@r1")
    op(0x60, "jz {rel}", 2); op(0x62, "xrl {d},a", 2); op(0x63, "xrl {d},#{i}", 3); op(0x64, "xrl a,#{i}", 2)
    op(0x65, "xrl a,{d}", 2); op(0x66, "xrl a,@r0"); op(0x67, "xrl a,@r1")
    op(0x70, "jnz {rel}", 2); op(0x72, "orl c,{b}", 2); op(0x73, "jmp @a+dptr"); op(0x74, "mov a,#{i}", 2)
    op(0x75, "mov {d},#{i}", 3); op(0x76, "mov @r0,#{i}", 2); op(0x77, "mov @r1,#{i}", 2)
    op(0x80, "sjmp {rel}", 2); op(0x82, "anl c,{b}", 2); op(0x83, "movc a,@a+pc"); op(0x84, "div ab")
    op(0x85, "mov {d2},{d1}", 3); op(0x86, "mov {d},@r0", 2); op(0x87, "mov {d},@r1", 2)
    op(0x90, "mov dptr,#{i16}", 3); op(0x92, "mov {b},c", 2); op(0x93, "movc a,@a+dptr"); op(0x94, "subb a,#{i}", 2)
    op(0x95, "subb a,{d}", 2); op(0x96, "subb a,@r0"); op(0x97, "subb a,@r1")
    op(0xA0, "orl c,/{b}", 2); op(0xA2, "mov c,{b}", 2); op(0xA3, "inc dptr"); op(0xA4, "mul ab")
    op(0xA5, "db 0xa5"); op(0xA6, "mov @r0,{d}", 2); op(0xA7, "mov @r1,{d}", 2)
    op(0xB0, "anl c,/{b}", 2); op(0xB2, "cpl {b}", 2); op(0xB3, "cpl c"); op(0xB4, "cjne a,#{i},{rel}", 3)
    op(0xB5, "cjne a,{d},{rel}", 3); op(0xB6, "cjne @r0,#{i},{rel}", 3); op(0xB7, "cjne @r1,#{i},{rel}", 3)
    op(0xC0, "push {d}", 2); op(0xC2, "clr {b}", 2); op(0xC3, "clr c"); op(0xC4, "swap a")
    op(0xC5, "xch a,{d}", 2); op(0xC6, "xch a,@r0"); op(0xC7, "xch a,@r1")
    op(0xD0, "pop {d}", 2); op(0xD2, "setb {b}", 2); op(0xD3, "setb c"); op(0xD4, "da a")
    op(0xD5, "djnz {d},{rel}", 3); op(0xD6, "xchd a,@r0"); op(0xD7, "xchd a,@r1")
    op(0xE0, "movx a,@dptr"); op(0xE2, "movx a,@r0"); op(0xE3, "movx a,@r1"); op(0xE4, "clr a")
    op(0xE5, "mov a,{d}", 2); op(0xE6, "mov a,@r0"); op(0xE7, "mov a,@r1")
    op(0xF0, "movx @dptr,a"); op(0xF2, "movx @r0,a"); op(0xF3, "movx @r1,a"); op(0xF4, "cpl a")
    op(0xF5, "mov {d},a", 2); op(0xF6, "mov @r0,a"); op(0xF7, "mov @r1,a")
    for n in range(8):
        op(0x08 + n, f"inc {R[n]}"); op(0x18 + n, f"dec {R[n]}"); op(0x28 + n, f"add a,{R[n]}")
        op(0x38 + n, f"addc a,{R[n]}"); op(0x48 + n, f"orl a,{R[n]}"); op(0x58 + n, f"anl a,{R[n]}")
        op(0x68 + n, f"xrl a,{R[n]}"); op(0x78 + n, f"mov {R[n]},#{{i}}", 2)
        op(0x88 + n, f"mov {{d}},{R[n]}", 2); op(0x98 + n, f"subb a,{R[n]}")
        op(0xA8 + n, f"mov {R[n]},{{d}}", 2); op(0xB8 + n, f"cjne {R[n]},#{{i}},{{rel}}", 3)
        op(0xC8 + n, f"xch a,{R[n]}"); op(0xD8 + n, f"djnz {R[n]},{{rel}}", 2)
        op(0xE8 + n, f"mov a,{R[n]}"); op(0xF8 + n, f"mov {R[n]},a")
    for hi in range(8):
        t[(hi << 5) | 0x01] = ("ajmp {a11}", 2)
        t[(hi << 5) | 0x11] = ("acall {a11}", 2)
    return t


TABLE = build()
SFR = {0x80: "p0", 0x81: "sp", 0x82: "dpl", 0x83: "dph", 0x87: "pcon", 0x88: "tcon", 0x89: "tmod",
       0x90: "p1", 0xA0: "p2", 0xA8: "ie", 0xB0: "p3", 0xD0: "psw", 0xE0: "acc", 0xF0: "b"}


def dname(v):
    return SFR.get(v, f"{v:#04x}")


def decode(code, pc):
    """Return (size, text, target_or_None)."""
    o = code[pc]
    fmt, size = TABLE[o]
    ops = code[pc + 1:pc + size]
    target = None
    f = {}
    if "{a16}" in fmt:
        target = (ops[0] << 8) | ops[1]
        f["a16"] = f"{target:#06x}"
    if "{a11}" in fmt:
        target = ((pc + 2) & 0xF800) | ((o >> 5) << 8) | ops[0]
        f["a11"] = f"{target:#06x}"
    if "{i16}" in fmt:
        f["i16"] = f"{(ops[0] << 8) | ops[1]:#06x}"
    if "{d2}" in fmt:
        f["d1"], f["d2"] = dname(ops[0]), dname(ops[1])
    if "{rel}" in fmt:
        r = ops[-1]
        target = (pc + size + (r - 256 if r > 127 else r)) & 0xFFFF
        f["rel"] = f"{target:#06x}"
    rest = list(ops[:-1]) if "{rel}" in fmt else list(ops)
    if "{b}" in fmt:
        f["b"] = f"bit{rest.pop(0):#04x}"
    if "{d}" in fmt:
        f["d"] = dname(rest.pop(0))
    if "{i}" in fmt:
        f["i"] = f"{rest.pop(0):#04x}"
    return size, fmt.format(**f), target


def disasm(code, start, end):
    pc = start
    while pc < end:
        size, text, _ = decode(code, pc)
        raw = code[pc:pc + size].hex(" ")
        print(f"{pc:04x}: {raw:<9} {text}")
        pc += size


if __name__ == "__main__":
    fw = open(sys.argv[1], "rb").read()
    code = fw[HDR:]
    disasm(code, int(sys.argv[2], 16), int(sys.argv[3], 16))
