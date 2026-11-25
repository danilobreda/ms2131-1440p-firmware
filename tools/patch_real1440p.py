#!/usr/bin/env python3
"""Build the "real 1440p" firmware: 2560x1440 with real pixels on both axes at 29.97 fps, no tearing.

Usage: python patch_real1440p.py <stock_backup.bin> <output.bin>

Input: your own backup of the stock firmware (build "Jul 22 2026", code length 0xf86b, SHA-256
45a74613c52727b3eae0aeec05a479a0b489e17254d15c2625f7f786be208134). Anything else is refused.

Step 1 is patch_1440p.py with --no-sharpen (2560x1440@30 USB mode, 2560x1440 size limit, dynamic
line pitch, no sharpening). Step 2 adds a routine to the firmware main loop (ROM 0x53df,
lcall 0xda4b -> lcall HOOK) and a small change to the stock output watchdog. Boot is unchanged.

On every pass of the main loop the routine checks: input is 2560x1440 (XDATA 0x22b8..0x22bb), output is
2560x1440 (XDATA 0x22de..0x22e1), format is YUY2 (IRAM 0x30 == 2) and the stream is running
(0xf007 & 3 == 3). If the configuration is not in place yet, it:
  - raises the DRAM clock (PLL2) from 180 to 210 MHz: N=140 with P=5 (168 MHz), ~10 ms, then P=4;
    only if M is the expected 4 (0xd121 == 0x03);
  - turns the two 2^21-slot frame buffers into one 2^22-slot buffer, sets the pre-scaler to 1:1
    (2560 stored columns) and the line pitch to 2560;
  - reads the stored frame 1:1 with a 120 MHz output pixel clock, htotal 2733, vtotal 1465 (29.971 fps,
    half of the 59.939 Hz a PS5 sends at 1440p, within a few ppm).
Once the configuration is in place it runs one step of a GENLOCK per pass (~8000 passes/s):
  - 0xe160 bit 0 toggles at every input frame (write-buffer index) and 0xf1ac changes at every output
    frame; the routine counts main-loop passes between them to get the output phase inside the input
    frame, tracks the passes per input frame, and steers vtotal to 1463/1465/1467 to hold the phase;
  - a reading counts only if an input edge was seen in the last 1.5 input frames; otherwise vtotal goes
    back to 1465. 0xe160 sometimes skips toggles, and the pass counter saturates at 255, below two
    input frames (~270 passes). While toggles were missing, the first genlock build read a constant
    false phase (255 - P), held vtotal at 1467 and walked the tear line through the picture;
  - the new vtotal is written ~10 passes (~1.2 ms) after the decision, so the output line counter has
    already wrapped and can never run past a shorter vtotal (that produced ~1 s frames);
  - with a single frame buffer the writer is twice as fast as the reader; holding the phase keeps the
    point where the writer passes the reader out of the picture, which removes the tear line.
The stock output watchdog (ROM 0x9be5) periodically re-strobes 0xf620 and restarts the output pipeline
if the strobe is not consumed; in this mode it is skipped (in every other mode it runs as stock).
In any other mode the routine restores the two stock frame buffers. PLL2 stays at 210 MHz until unplug.

Genlock state lives in unused XDATA at 0x5c00 (reset on every mode entry): +4 passes per input frame,
+5 target phase (/256, default 0), +6 dead band, +7 step, +8 enable, +11 last error, +12 last phase,
+0x40..0x7f log of the last 64 phase readings.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch_1440p  # noqa: E402

HDR = 0x30
STOCK_SHA = "45a74613c52727b3eae0aeec05a479a0b489e17254d15c2625f7f786be208134"
BASE_SHA = "532d3d8dde5f85aa3acaeee413bc660d6cc81e934ff4d5ff76234ebda391c0c2"   # patch_1440p --no-sharpen
RESULT_SHA = "5441711a25a4a1ea3486bc53b7353c4f90fa88bdc4b738a28f6cbd49d8710efb"
BASE_LEN = 0xF8BA
MAX_LEN = 0xFBA0          # largest code length seen in vendor MS2131 firmware
CALL_SITE = 0x53DF        # lcall 0xda4b in the main loop
DA4B, ED31 = 0xDA4B, 0xED31   # LED routine (original call target), ~1 us delay loop (r7 = us)
WD_SITE, WD_CONT, WD_SKIP = 0x9BE5, 0x9BE8, 0x9C15   # stock output watchdog: f620 check / continue / end
G = 0x5C00                # genlock state (unused XDATA RAM)
HT, VT = 2733, 1465
GL_DEFAULTS = (137, 0x00, 1, 2, 1)   # passes per input frame, target, dead band, step, enable
WRITE_DELAY = 10          # main-loop passes between the decision and the vtotal write

TABLE = [(0xF584, 0xFF), (0xF585, 0xFF), (0xF586, 0x1F), (0xF594, 0x00), (0xF595, 0x00), (0xF596, 0x00),
         (0xF604, 0x00), (0xF605, 0x0A), (0xF606, 0x00), (0xF607, 0x0A), (0xF600, 0x01),
         (0xF597, 0x00), (0xF598, 0x05), (0xF599, 0x00), (0xF59A, 0x05),
         (0xF660, 0x00), (0xF661, 0x0A), (0xF65B, 0x00), (0xF65C, 0x10),
         (0xF006, 0x03), (0xF622, HT & 0xFF), (0xF623, HT >> 8), (0xF624, VT & 0xFF), (0xF625, VT >> 8),
         (0xF620, 0x01), (G, 0x00)]
CHECKS = [(0xF006, 0x03), (0xF622, HT & 0xFF), (0xF586, 0x1F), (0xF596, 0x00), (0xF598, 0x05),
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


def emit_mode(a):
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
    a.emit(0x02, ("abs", "GL"))                             # configured: one genlock step
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


def emit_genlock(a):
    """One genlock step per main-loop pass. Uses a, b, r0..r7, dptr. Exits with ljmp 0xda4b."""
    a.label("GL")
    mov_dptr(a, G)
    a.emit(0xE0, 0xB4, 0xA7, ("rel", "GINIT0"))            # not initialised yet?
    mov_dptr(a, G + 14)                                     # pending vtotal write: count down
    a.emit(0xE0, 0x60, ("rel", "NOPEND"), 0x14, 0xF0, 0x70, ("rel", "NOPEND"))
    mov_dptr(a, G + 15)
    a.emit(0xE0)
    mov_dptr(a, 0xF624)
    a.emit(0xF0)                                            # vtotal low byte
    mov_dptr(a, 0xF620)
    a.emit(0x74, 0x01, 0xF0)                                # apply output timing
    a.label("NOPEND")
    mov_dptr(a, G + 3)                                      # CNT = passes since the last input edge
    a.emit(0xE0, 0x04, 0x70, ("rel", "C1"), 0x14)           # saturates at 0xff
    a.label("C1")
    a.emit(0xF0, 0xFF)                                      # r7 = CNT
    mov_dptr(a, 0xE160)                                     # input frame edge = toggle of e160 bit 0
    a.emit(0xE0, 0x54, 0x01, 0xFE)
    mov_dptr(a, G + 1)
    a.emit(0xE0, 0x6E, 0x60, ("rel", "NOIN"))
    a.emit(0xEE, 0xF0)
    mov_dptr(a, G + 3)
    a.emit(0xE4, 0xF0)                                      # CNT = 0
    mov_dptr(a, G + 4)
    a.emit(0xE0, 0xFD)                                      # r5 = P (passes per input frame)
    a.emit(0xC3, 0x13, 0xFC)                                # r4 = P/2
    a.emit(0xED, 0xC3, 0x9C, 0xFB)                          # accept the interval only in P/2 .. 3P/2
    a.emit(0xEF, 0xC3, 0x9B, 0x40, ("rel", "IN0"))
    a.emit(0xED, 0x2C, 0x50, ("rel", "HOK"), 0x74, 0xFF)
    a.label("HOK")
    a.emit(0xC3, 0x9F, 0x40, ("rel", "IN0"))                # (a missed edge gives ~2P)
    a.emit(0xEF, 0xC3, 0x9D, 0x60, ("rel", "IN0"))
    a.emit(0x40, ("rel", "PDEC"))                           # track P by +-1
    a.emit(0x0D, 0x80, ("rel", "PST"))
    a.label("PDEC")
    a.emit(0x1D)
    a.label("PST")
    mov_dptr(a, G + 4)
    a.emit(0xED, 0xF0)
    a.label("IN0")
    a.emit(0x7F, 0x00)
    a.label("NOIN")
    mov_dptr(a, 0xF1AC)                                     # output frame = change of f1ac
    a.emit(0xE0, 0xFE)
    mov_dptr(a, G + 2)
    a.emit(0xE0, 0x6E, 0x70, ("rel", "OUT1"))
    a.label("GEXIT")
    a.emit(0x02, DA4B >> 8, DA4B & 0xFF)
    a.label("GINIT0")
    a.emit(0x02, ("abs", "GINIT"))
    a.label("INV")                                          # no input edge for >= 1.5 frames: the
    a.emit(0x02, ("abs", "NOM"))                            # phase is unknown -> nominal vtotal
    a.label("OUT1")
    a.emit(0xEE, 0xF0)
    mov_dptr(a, G + 4)
    a.emit(0xE0, 0xFD)                                      # r5 = P
    a.emit(0xEF, 0xC3, 0x9D, 0x40, ("rel", "DOK"))          # CNT < P: d = CNT
    a.emit(0xFF, 0xED, 0x13)                                # r7 = CNT - P; a = P/2 (C = 0 here)
    a.emit(0xD3, 0x9F, 0x40, ("rel", "INV"))                # CNT - P >= P/2 (also CNT saturated at 255)
    a.label("DOK")
    mov_dptr(a, G + 12)
    a.emit(0xEF, 0xF0)                                      # last phase reading
    mov_dptr(a, G + 9)
    a.emit(0xE0, 0xFB, 0x04, 0x54, 0x3F, 0xF0)
    a.emit(0xEB, 0x24, 0x40, 0xF5, 0x82, 0x75, 0x83, G >> 8, 0xEF, 0xF0)   # log[idx] = d
    mov_dptr(a, G + 8)
    a.emit(0xE0, 0x60, ("rel", "GEXIT"))                    # disabled
    mov_dptr(a, G + 5)
    a.emit(0xE0, 0x8D, 0xF0, 0xA4, 0xAC, 0xF0)              # r4 = target * P / 256
    a.emit(0xEF, 0xC3, 0x13, 0xFF)                          # work in half passes (fits 8-bit signed)
    a.emit(0xEC, 0xC3, 0x13, 0xFC)
    a.emit(0xED, 0xC3, 0x13, 0xFD)
    a.emit(0xEF, 0xC3, 0x9C, 0xFB)                          # r3 = error
    a.emit(0xED, 0xC3, 0x13, 0xFA)                          # wrap the error into -P/4 .. +P/4 (half units)
    a.emit(0xEB, 0x64, 0x80, 0xF9)
    a.emit(0xEA, 0x24, 0x80, 0xF8)
    a.emit(0xE8, 0xC3, 0x99, 0x50, ("rel", "W1"))
    a.emit(0xEB, 0xC3, 0x9D, 0xFB, 0x80, ("rel", "W2"))
    a.label("W1")
    a.emit(0x74, 0x80, 0xC3, 0x9A, 0xF8)
    a.emit(0xE9, 0xC3, 0x98, 0x50, ("rel", "W2"))
    a.emit(0xEB, 0x2D, 0xFB)
    a.label("W2")
    mov_dptr(a, G + 11)
    a.emit(0xEB, 0xF0)                                      # last error
    mov_dptr(a, G + 6)
    a.emit(0xE0, 0xFA)                                      # r2 = dead band
    a.emit(0xEB, 0x64, 0x80, 0xF9)
    a.emit(0xEA, 0x24, 0x80, 0xC3, 0x99, 0x50, ("rel", "NL"))
    mov_dptr(a, G + 7)
    a.emit(0xE0, 0xF8)
    mov_dptr(a, G + 13)
    a.emit(0xE0, 0xC3, 0x98, 0x80, ("rel", "SETV"))         # output late: shorter frames
    a.label("NL")
    a.emit(0x74, 0x80, 0xC3, 0x9A, 0xF8)
    a.emit(0xE9, 0xC3, 0x98, 0x50, ("rel", "NOM"))
    mov_dptr(a, G + 7)
    a.emit(0xE0, 0xF8)
    mov_dptr(a, G + 13)
    a.emit(0xE0, 0x28, 0x80, ("rel", "SETV"))               # output early: longer frames
    a.label("NOM")
    mov_dptr(a, G + 13)
    a.emit(0xE0)                                            # inside the dead band: nominal vtotal
    a.label("SETV")
    a.emit(0xF8)
    mov_dptr(a, G + 10)
    a.emit(0xE0, 0x68, 0x60, ("rel", "GEXIT2"))             # unchanged: nothing to do
    a.emit(0xE8, 0xF0)
    mov_dptr(a, G + 15)
    a.emit(0xF0)                                            # pending value
    mov_dptr(a, G + 14)
    a.emit(0x74, WRITE_DELAY, 0xF0)                         # write it WRITE_DELAY passes from now
    a.label("GEXIT2")
    a.emit(0x02, DA4B >> 8, DA4B & 0xFF)
    a.label("GINIT")                                        # (re)initialise on every mode entry
    mov_dptr(a, 0xE160)
    a.emit(0xE0, 0x54, 0x01, 0xFF)
    mov_dptr(a, G + 1)
    a.emit(0xEF, 0xF0)
    mov_dptr(a, 0xF1AC)
    a.emit(0xE0, 0xFF)
    mov_dptr(a, G + 2)
    a.emit(0xEF, 0xF0)
    a.emit(0xA3, 0xE4, 0xF0)                                # +3 CNT = 0
    for v in GL_DEFAULTS + (0x00, VT & 0xFF, 0x00, 0x00, VT & 0xFF, 0x00, VT & 0xFF):   # +4..+15
        a.emit(0xA3, 0x74, v, 0xF0)
    mov_dptr(a, G)
    a.emit(0x74, 0xA7, 0xF0)
    a.emit(0x02, DA4B >> 8, DA4B & 0xFF)


def emit_watchdog(a):
    """Stock output watchdog (0x9be5): skipped in this mode (single frame buffer active, f586 == 1f)."""
    a.label("WD")
    mov_dptr(a, 0xF586)
    a.emit(0xE0, 0xB4, 0x1F, ("rel", "WDO"))
    a.emit(0x02, WD_SKIP >> 8, WD_SKIP & 0xFF)
    a.label("WDO")
    mov_dptr(a, 0xF620)                                     # the instruction replaced at 0x9be5
    a.emit(0x02, WD_CONT >> 8, WD_CONT & 0xFF)


def hook(org):
    a = Asm(org)
    emit_mode(a)
    emit_genlock(a)
    emit_watchdog(a)
    return a.assemble()


def build(stock):
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA:
        raise SystemExit("ABORT: input is not the expected stock firmware")
    base, _ = patch_1440p.build(stock, True)
    if hashlib.sha256(base).hexdigest() != BASE_SHA:
        raise SystemExit("ABORT: step 1 produced an unexpected image")
    fw = bytearray(base)
    site, wd = HDR + CALL_SITE, HDR + WD_SITE
    if bytes(fw[site:site + 3]) != bytes([0x12, 0xDA, 0x4B]) or bytes(fw[HDR + ED31:HDR + ED31 + 3]) != bytes([0x12, 0xE5, 0x35]):
        raise SystemExit("ABORT: main loop call or delay routine not found")
    if bytes(fw[wd:wd + 4]) != bytes([0x90, 0xF6, 0x20, 0xE0]) or bytes(fw[HDR + WD_SKIP:HDR + WD_SKIP + 3]) != bytes([0x90, 0x7F, 0x02]):
        raise SystemExit("ABORT: output watchdog not found")
    code, labels = hook(BASE_LEN)
    new_len = BASE_LEN + len(code)
    if new_len > MAX_LEN:
        raise SystemExit("ABORT: code too large")
    end = HDR + BASE_LEN
    fw[end:end + 4] = bytes([0xFF] * 4)
    fw[end:end + len(code)] = code
    fw[site + 1], fw[site + 2] = BASE_LEN >> 8, BASE_LEN & 0xFF
    fw[wd:wd + 3] = bytes([0x02, labels["WD"] >> 8, labels["WD"] & 0xFF])
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
