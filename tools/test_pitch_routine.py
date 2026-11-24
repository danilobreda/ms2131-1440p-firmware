#!/usr/bin/env python3
"""Run the patched firmware's frame-memory-size tail and the appended pitch routine in a tiny
8051 interpreter and check the registers it writes for a set of input/output combinations.

Usage: python test_pitch_routine.py <patched.bin>

This executes the real bytes of the image, starting at ROM 0xc25f (mov dptr,#0xf662) with the
state the original code has at that point, until the routine returns.
"""
import sys

HDR = 0x30


def run(code, w_units, height, pclk):
    """Return {register: value} written, given the frame memory width/height and input pixel clock."""
    x = {0xF660: w_units & 0xFF, 0xF661: w_units >> 8, 0x22BC: pclk >> 8, 0x22BD: pclk & 0xFF}
    written = {}
    a, c, dptr = height & 0xFF, 0, 0
    r = [0] * 8
    r[6] = height >> 8
    pc, steps = 0xC25F, 0

    def rel(v):
        return v - 256 if v > 127 else v

    while True:
        steps += 1
        assert steps < 500, "runaway"
        o = code[pc]
        if o == 0x90:
            dptr, pc = (code[pc + 1] << 8) | code[pc + 2], pc + 3
        elif o == 0xF0:
            x[dptr] = a
            written[dptr] = a
            pc += 1
        elif o == 0xE0:
            a, pc = x[dptr], pc + 1
        elif o == 0xA3:
            dptr, pc = (dptr + 1) & 0xFFFF, pc + 1
        elif o == 0x02:
            pc = (code[pc + 1] << 8) | code[pc + 2]
        elif 0xE8 <= o <= 0xEF:
            a, pc = r[o & 7], pc + 1
        elif 0xF8 <= o <= 0xFF:
            r[o & 7], pc = a, pc + 1
        elif 0x78 <= o <= 0x7F:
            r[o & 7], pc = code[pc + 1], pc + 2
        elif o == 0x74:
            a, pc = code[pc + 1], pc + 2
        elif o == 0xC3:
            c, pc = 0, pc + 1
        elif o == 0xD3:
            c, pc = 1, pc + 1
        elif o == 0x13:
            a, c, pc = (a >> 1) | (c << 7), a & 1, pc + 1
        elif 0x98 <= o <= 0x9F or o == 0x94:
            v = code[pc + 1] if o == 0x94 else r[o & 7]
            t = a - v - c
            a, c = t & 0xFF, 1 if t < 0 else 0
            pc += 2 if o == 0x94 else 1
        elif o in (0x40, 0x50, 0x80):
            taken = (o == 0x80) or (o == 0x40 and c) or (o == 0x50 and not c)
            pc = pc + 2 + (rel(code[pc + 1]) if taken else 0)
        elif o == 0x22:
            return written
        else:
            raise AssertionError(f"unexpected opcode {o:#04x} at {pc:#06x}")


CASES = [
    # (description, stored width (register 0xf660), stored height, input pixel clock in 10 kHz, expected pitch)
    # Cases with more than 1092 lines at 170 MHz or less only check the routine's arithmetic;
    # those images do not fit the frame memory (see the README).
    ("1440p60 in (above 170 MHz, width halved), 1280x1440 stored", 1280, 1440, 24151, 640),
    ("1440p60 in (above 170 MHz, width halved), 1920x1080 stored", 960, 1080, 24151, 960),
    ("1440p60 in (above 170 MHz, width halved), 1600x1200 stored", 800, 1200, 24151, 640),
    ("1440p60 in (above 170 MHz, width halved), x1092 stored", 1280, 1092, 24151, 960),
    ("1440p60 in (above 170 MHz, width halved), x1093 stored", 1280, 1093, 24151, 640),
    ("1080p60 in (170 MHz or less), 1920x1080 stored", 1920, 1080, 14850, 960),
    ("4K60 4:2:0 in, halved to 1920x1080 (170 MHz or less)", 1920, 1080, 14838, 960),
    ("1080p60 in, 640x480 stored", 640, 480, 14850, 960),
    ("1920x1200@60 in (170 MHz or less), 1920x1200 stored", 1920, 1200, 15400, 960),
    ("1600x1200@60 in (170 MHz or less), 1600x1200 stored", 1600, 1200, 16200, 960),
    ("2560x1440@30 in (170 MHz or less), stored", 2560, 1440, 11980, 1280),
    ("width 1922 single-pixel (just over the stock pitch)", 1922, 1080, 14850, 961),
    ("pixel clock exactly 170.00 MHz, tall memory -> not halved", 1280, 1440, 0x4268, 960),
    ("pixel clock 170.01 MHz, tall memory -> halved", 1280, 1440, 0x4269, 640),
]


def main():
    fw = open(sys.argv[1], "rb").read()
    code = fw[HDR:]
    ok = True
    for desc, w, h, pclk, want in CASES:
        wr = run(code, w, h, pclk)
        got = wr.get(0xF597, -1) | (wr.get(0xF598, -1) << 8)
        got2 = wr.get(0xF599, -1) | (wr.get(0xF59A, -1) << 8)
        height_ok = wr.get(0xF662) == (h & 0xFF) and wr.get(0xF663) == (h >> 8)
        good = got == got2 == want and height_ok and set(wr) == {0xF662, 0xF663, 0xF597, 0xF598, 0xF599, 0xF59A}
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {desc}: pitch {got} (expected {want}), height written {'ok' if height_ok else 'WRONG'}")
    print("ALL PASSED" if ok else "FAILURES")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
