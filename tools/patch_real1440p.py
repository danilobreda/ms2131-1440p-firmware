#!/usr/bin/env python3
"""Build the "real 1440p" firmware: 2560x1440 with real pixels on both axes at ~30 fps (29.96).

Usage: python patch_real1440p.py <stock_backup.bin> <output.bin>

Input: your own backup of the stock firmware (build "Jul 22 2026", code length 0xf86b, SHA-256
45a74613c52727b3eae0aeec05a479a0b489e17254d15c2625f7f786be208134). Anything else is refused.

Step 1 is patch_1440p.py with --no-sharpen (2560x1440@30 USB mode, 2560x1440 size limit, dynamic
line pitch, no sharpening). Step 2 adds a small routine to the firmware main loop (ROM 0x53df,
`lcall 0xda4b` -> `lcall HOOK`). Boot is unchanged: DRAM training still runs at the stock 180 MHz and
USB/HID come up exactly as on the stock firmware.

On every pass of the main loop the routine checks: input is 2560x1440 (XDATA 0x22b8..0x22bb), output is
2560x1440 (XDATA 0x22de..0x22e1), format is YUY2 (IRAM 0x30 == 2, the YUY2 bFormatIndex) and the stream
is running (0xf007 & 3 == 3). If so, and the configuration is not already in place, it:
  - raises the DRAM clock (PLL2) from 180 to 210 MHz: N=140 with P=5 (168 MHz), ~10 ms, then P=4;
    only if M is the expected 4 (0xd121 == 0x03);
  - turns the two 2^21-slot frame buffers into one 2^22-slot buffer, sets the pre-scaler to 1:1
    (2560 stored columns) and the line pitch to 2560;
  - reads the stored frame 1:1 with a 120 MHz output pixel clock (htotal 2760, vtotal 1451 = 29.96 fps).
In any other mode it restores the two stock frame buffers. PLL2 stays at 210 MHz until the card is
unplugged.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch_1440p  # noqa: E402

HDR = 0x30
STOCK_SHA = "45a74613c52727b3eae0aeec05a479a0b489e17254d15c2625f7f786be208134"
BASE_SHA = "532d3d8dde5f85aa3acaeee413bc660d6cc81e934ff4d5ff76234ebda391c0c2"   # patch_1440p --no-sharpen
RESULT_SHA = "32325d91653f5dc94c8eb2468ec89558f6eb63226da12c3b5fc6c2f02f69e175"
BASE_LEN = 0xF8BA
MAX_LEN = 0xFBA0          # largest code length seen in vendor MS2131 firmware
CALL_SITE = 0x53DF        # lcall 0xda4b in the main loop
DA4B, ED31 = 0xDA4B, 0xED31   # LED routine (original call target), ~1 us delay loop (r7 = us)

TABLE = [(0xF584, 0xFF), (0xF585, 0xFF), (0xF586, 0x1F), (0xF594, 0x00), (0xF595, 0x00), (0xF596, 0x00),
         (0xF604, 0x00), (0xF605, 0x0A), (0xF606, 0x00), (0xF607, 0x0A), (0xF600, 0x01),
         (0xF597, 0x00), (0xF598, 0x05), (0xF599, 0x00), (0xF59A, 0x05),
         (0xF660, 0x00), (0xF661, 0x0A), (0xF65B, 0x00), (0xF65C, 0x10),
         (0xF006, 0x03), (0xF622, 0xC8), (0xF623, 0x0A), (0xF624, 0xAB), (0xF625, 0x05), (0xF620, 0x01)]
CHECKS = [(0xF006, 0x03), (0xF622, 0xC8), (0xF624, 0xAB), (0xF586, 0x1F), (0xF596, 0x00), (0xF598, 0x05),
          (0xF59A, 0x05), (0xF607, 0x0A), (0xF661, 0x0A), (0xF65C, 0x10)]


class Asm:
    """Two-pass 8051 byte assembler. Items: int, ('lbl', n), ('rel', n), ('abs', n), ('hi', n), ('lo', n)."""

    SIZE = {"rel": 1, "abs": 2, "hi": 1, "lo": 1}

    def __init__(self, org):
        self.org, self.items = org, []

    def emit(self, *xs):
        self.items.extend(xs)

    def label(self, name):
        self.items.append(("lbl", name))

    def assemble(self):
        labels, pc = {}, self.org
        for it in self.items:
            if isinstance(it, tuple):
                if it[0] == "lbl":
                    labels[it[1]] = pc
                else:
                    pc += self.SIZE[it[0]]
            else:
                pc += 1
        out, pc = bytearray(), self.org
        for it in self.items:
            if isinstance(it, tuple):
                kind, name = it
                if kind == "lbl":
                    continue
                t = labels[name]
                if kind == "rel":
                    d = t - (pc + 1)
                    if not -128 <= d <= 127:
                        raise SystemExit(f"relative jump out of range to {name} at {pc:#x}")
                    out.append(d & 0xFF)
                elif kind == "abs":
                    out += bytes([t >> 8, t & 0xFF])
                elif kind == "hi":
                    out.append(t >> 8)
                else:
                    out.append(t & 0xFF)
                pc += self.SIZE[kind]
            else:
                out.append(it)
                pc += 1
        return bytes(out), labels


def mov_dptr(a, v):
    a.emit(0x90, v >> 8, v & 0xFF)


def hook(org):
    a = Asm(org)
    for base in (0x22B8, 0x22DE):                           # input / output active size = 2560x1440
        mov_dptr(a, base)
        a.emit(0xE0, 0xB4, 0x0A, ("rel", "NOMODE"))
        a.emit(0xA3, 0xE0, 0x70, ("rel", "NOMODE"))
        a.emit(0xA3, 0xE0, 0xB4, 0x05, ("rel", "NOMODE"))
        a.emit(0xA3, 0xE0, 0xB4, 0xA0, ("rel", "NOMODE"))
    a.emit(0xE5, 0x30, 0xB4, 0x02, ("rel", "NOMODE"))      # IRAM 0x30 == 2 (YUY2)
    mov_dptr(a, 0xF007)
    a.emit(0xE0, 0x54, 0x03, 0xB4, 0x03, ("rel", "DONE"))  # stream running
    a.emit(0x80, ("rel", "CHECKS"))
    a.label("NOMODE")                                       # other mode: restore the two stock buffers
    mov_dptr(a, 0xF586)
    a.emit(0xE0, 0xB4, 0x1F, ("rel", "DONE"))
    a.emit(0x74, 0x0F, 0xF0)
    mov_dptr(a, 0xF596)
    a.emit(0x74, 0x10, 0xF0)
    a.label("DONE")
    a.emit(0x02, DA4B >> 8, DA4B & 0xFF)                    # ljmp to the original call target
    a.label("CHECKS")                                       # already configured?
    for reg, val in CHECKS:
        mov_dptr(a, reg)
        a.emit(0xE0, 0xB4, val, ("rel", "APPLY"))
    a.emit(0x80, ("rel", "DONE"))
    a.label("APPLY")
    mov_dptr(a, 0xD124)
    a.emit(0xE0, 0x54, 0x07, 0xB4, 0x03, ("rel", "PLLGO"))
    mov_dptr(a, 0xD123)
    a.emit(0xE0, 0xB4, 0x45, ("rel", "PLLGO"))
    a.emit(0x80, ("rel", "PIPE"))                           # PLL2 already at 210 MHz
    a.label("PLLGO")
    mov_dptr(a, 0xD121)
    a.emit(0xE0, 0xB4, 0x03, ("rel", "DONE"))               # unexpected M: change nothing
    mov_dptr(a, 0xD122)
    a.emit(0x74, 0x01, 0xF0)                                # (N-1)&1 = 1
    a.emit(0xA3, 0x74, 0x45, 0xF0)                          # (N-1)>>1 = 0x45 -> N=140 (168 MHz with P=5)
    a.emit(0x12, ("abs", "DLY"))
    mov_dptr(a, 0xD124)
    a.emit(0xE0, 0x54, 0xF8, 0x44, 0x03, 0xF0)              # P=4 -> 210 MHz
    a.emit(0x12, ("abs", "DLY"))
    a.label("PIPE")
    a.emit(0x7E, ("hi", "TAB"), 0x7F, ("lo", "TAB"))
    a.label("TL")                                           # write (addr_hi, addr_lo, value) triplets
    a.emit(0x8E, 0x83, 0x8F, 0x82)
    a.emit(0xE4, 0x93, 0x60, ("rel", "TEND"), 0xFA)
    a.emit(0x74, 0x01, 0x93, 0xFB)
    a.emit(0x74, 0x02, 0x93, 0xFC)
    a.emit(0xEF, 0x24, 0x03, 0xFF, 0xEE, 0x34, 0x00, 0xFE)
    a.emit(0x8A, 0x83, 0x8B, 0x82, 0xEC, 0xF0)
    a.emit(0x80, ("rel", "TL"))
    a.label("TEND")
    a.emit(0x02, DA4B >> 8, DA4B & 0xFF)
    a.label("DLY")                                          # ~10 ms: 50 x ed31(200 us); ed31 keeps r2/dptr
    a.emit(0x7A, 50)
    a.label("DL1")
    a.emit(0x7F, 200, 0x12, ED31 >> 8, ED31 & 0xFF, 0xDA, ("rel", "DL1"))
    a.emit(0x22)
    a.label("TAB")
    for reg, val in TABLE:
        a.emit(reg >> 8, reg & 0xFF, val)
    a.emit(0x00)
    return a.assemble()[0]


def build(stock):
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA:
        raise SystemExit("ABORT: input is not the expected stock firmware")
    base, _ = patch_1440p.build(stock, True)
    if hashlib.sha256(base).hexdigest() != BASE_SHA:
        raise SystemExit("ABORT: step 1 produced an unexpected image")
    fw = bytearray(base)
    site = HDR + CALL_SITE
    if bytes(fw[site:site + 3]) != b"\x12\xda\x4b" or bytes(fw[HDR + ED31:HDR + ED31 + 3]) != b"\x12\xe5\x35":
        raise SystemExit("ABORT: main loop call or delay routine not found")
    code = hook(BASE_LEN)
    new_len = BASE_LEN + len(code)
    if new_len > MAX_LEN:
        raise SystemExit("ABORT: code too large")
    end = HDR + BASE_LEN
    fw[end:end + 4] = b"\xff" * 4
    fw[end:end + len(code)] = code
    fw[site + 1], fw[site + 2] = BASE_LEN >> 8, BASE_LEN & 0xFF
    fw[2], fw[3] = new_len >> 8, new_len & 0xFF
    nend = HDR + new_len
    h, c = patch_1440p.header_checksum(fw), patch_1440p.code_checksum(fw, nend)
    fw[nend:nend + 4] = bytes([h >> 8, h & 0xFF, c >> 8, c & 0xFF])
    if hashlib.sha256(fw).hexdigest() != RESULT_SHA:
        raise SystemExit("ABORT: result differs from the tested image")
    return bytes(fw)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    fw = build(open(sys.argv[1], "rb").read())
    with open(sys.argv[2], "xb") as f:
        f.write(fw)
    print(f"OK: {sys.argv[2]}  sha256 {RESULT_SHA}")


if __name__ == "__main__":
    main()
