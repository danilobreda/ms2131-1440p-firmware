#!/usr/bin/env python3
"""Read / write the SPI flash of MacroSilicon MS2130 / MS2131 capture cards over USB HID.

The HID protocol is a Python port of https://github.com/steve-m/ms213x_flash
(Copyright (C) 2026 Steve Markgraf, MIT license).

Usage:
    python ms2131_flash.py read  <out.bin>     # read-only, reads twice and compares
    python ms2131_flash.py info  <file.bin>    # check the checksums of an image file
    python ms2131_flash.py write <file.bin> --yes-write-flash [--expect-sha256 <hash of current content>]

`write` erases the flash and programs the file. DO NOT unplug the card while it runs.
The running firmware lives in the chip's RAM, so the card keeps answering until it is
unplugged; the new firmware only takes effect after you unplug and re-plug the USB cable.

Requires: pip install hidapi
Tested on Windows 11 only. On other systems prefer ms213x_flash for writing.
"""
import hashlib
import sys
import time

import hid

DEVICES = [(0x345F, 0x2131, "MS2131"), (0x345F, 0x2130, "MS2130"), (0x534D, 0x2130, "MS213x-ROM")]
FLASH_SIZE = 0x10000
PAGE = 256
HEADER_LEN = 0x30
REPORT_ID = 0x01
SFR_P2, SFR_P3 = 0xA0, 0xB0
SPI_OUT = (1 << 4) | (1 << 3) | (1 << 2)
SPI_IN = 1 << 5


class Ms213x:
    def __init__(self):
        self.dev = None
        for vid, pid, name in DEVICES:
            if hid.enumerate(vid, pid):
                self.dev = hid.device()
                self.dev.open(vid, pid)
                self.name = name
                break
        if self.dev is None:
            raise SystemExit("Capture card not found")

    def close(self):
        self.dev.close()

    def _set(self, cmd):
        data = bytes(cmd) + bytes(8 - len(cmd))
        if self.dev.send_feature_report(bytes([REPORT_ID]) + data) < 0:
            raise IOError("send_feature_report failed")

    def _get(self):
        # The Python hidapi wrapper returns max_length-1 bytes: [report_id, data...]
        resp = self.dev.get_feature_report(REPORT_ID, 10)
        if len(resp) < 9:
            raise IOError(f"short get_feature_report: {len(resp)}")
        return bytes(resp[1:9])

    def read_reg(self, addr):
        """Read one byte of XDATA / hardware register space (16-bit address)."""
        self._set([0xB5, addr >> 8, addr & 0xFF])
        return self._get()[3]

    def write_reg(self, addr, val):
        """Write one byte of XDATA / hardware register space. Volatile: lost on re-plug."""
        self._set([0xB6, addr >> 8, addr & 0xFF, val])

    def read_sfr(self, addr):
        self._set([0xC5, addr])
        return self._get()[2]

    def write_sfr(self, addr, val):
        self._set([0xC6, addr, val])

    def setup_spi(self):
        """Route the SPI flash pins (volatile registers only)."""
        self.write_reg(0xF01F, 0x00)
        self.write_sfr(SFR_P3, (self.read_sfr(SFR_P3) | SPI_IN) & ~SPI_OUT & 0xFF)
        self.write_sfr(SFR_P2, self.read_sfr(SFR_P2) | SPI_OUT)
        self.write_reg(0xF01F, 0x10)

    def read_page(self, addr):
        self._set([0xF7, 0x01, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, PAGE >> 8, PAGE & 0xFF])
        out = bytearray()
        for offs in range(0, PAGE, 8):
            self._set([0xF7, 0x00, 0x00, 0x00, 0x00, offs >> 8, offs & 0xFF, 0x00])
            out += self._get()
        return bytes(out)

    def read_flash(self, size=FLASH_SIZE, quiet=False):
        buf = bytearray()
        for addr in range(0, size, PAGE):
            buf += self.read_page(addr)
            if not quiet and addr % 0x4000 == 0:
                print(f"  reading {addr:#07x}/{size:#x}", flush=True)
        return bytes(buf)

    def erase_flash(self):
        self._set([0xFE])

    def write_page(self, addr, image):
        """Program the 256-byte page at `addr`, taken from the full image.

        Same sequence as ms213x_flash: 43 packets of 6 bytes (258 bytes); the 2 extra bytes
        are the first bytes of the following page, exactly like the reference tool sends.
        """
        self._set([0xF8, 0x01, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, PAGE >> 8, PAGE & 0xFF])
        padded = (image[addr:addr + PAGE + 6] + b"\xff" * 6)[:PAGE + 6]
        for offs in range(0, PAGE, 6):
            self._set([0xF8, 0x00] + list(padded[offs:offs + 6]))


def checksums(fw):
    """Return (code_len, header_ok, code_ok, header_csum, code_csum)."""
    if fw[0] != 0x3C or fw[1] != 0xC3:
        raise ValueError("3C C3 signature missing")
    code_len = (fw[2] << 8) | fw[3]
    end = HEADER_LEN + code_len
    hcalc = sum(b for i, b in enumerate(fw[:HEADER_LEN]) if i >= 2 and not (0x0C <= i <= 0x0F)) & 0xFFFF
    ccalc = sum(fw[HEADER_LEN:end]) & 0xFFFF
    hstored = (fw[end] << 8) | fw[end + 1]
    cstored = (fw[end + 2] << 8) | fw[end + 3]
    return code_len, hcalc == hstored, ccalc == cstored, hcalc, ccalc


def info(fw):
    code_len, hok, cok, hc, cc = checksums(fw)
    print(f"code_len={code_len:#x}  header_csum={hc:04x} ({'ok' if hok else 'BAD'})  "
          f"code_csum={cc:04x} ({'ok' if cok else 'BAD'})  sha256={hashlib.sha256(fw).hexdigest()}")
    return hok and cok


def program(ms, image):
    """Erase, program and verify. Returns True if the flash now equals the image."""
    used = len(image.rstrip(b"\xff"))
    n_pages = (used + PAGE - 1) // PAGE
    print("Erasing flash...", flush=True)
    ms.erase_flash()
    deadline = time.time() + 30
    while True:
        try:
            if ms.read_page(0) == b"\xff" * PAGE and ms.read_page((n_pages - 1) * PAGE) == b"\xff" * PAGE:
                break
        except IOError:
            pass
        if time.time() > deadline:
            print("Flash did not read back erased within 30 s")
            return False
        time.sleep(0.2)
    print(f"Programming {n_pages} pages...", flush=True)
    for p in range(n_pages):
        ms.write_page(p * PAGE, image)
        if p % 64 == 0:
            print(f"  programming {p * PAGE:#07x}", flush=True)
    print("Verifying...", flush=True)
    back = ms.read_flash(quiet=True)
    if back == image:
        return True
    bad = [i for i in range(len(image)) if back[i] != image[i]]
    print(f"MISMATCH in {len(bad)} bytes, first at {bad[0]:#x}")
    return False


def cmd_write(path, expect_sha):
    image = open(path, "rb").read()
    if len(image) < FLASH_SIZE:
        image += b"\xff" * (FLASH_SIZE - len(image))
    if len(image) != FLASH_SIZE:
        raise SystemExit("Image must be at most 64K")
    if not info(image):
        raise SystemExit("Image checksums are invalid. Nothing was written.")

    ms = Ms213x()
    try:
        print(f"Found: {ms.name}")
        ms.setup_spi()
        print("Reading current content (pre-write check)")
        current = ms.read_flash(quiet=True)
        cur_sha = hashlib.sha256(current).hexdigest()
        print(f"  current sha256: {cur_sha}")
        if expect_sha and cur_sha != expect_sha.lower():
            raise SystemExit("Current content differs from the expected hash. Nothing was written.")
        if current == image:
            print("Flash already contains this image. Nothing to do.")
            return 0

        for attempt in (1, 2, 3):
            print(f"== Attempt {attempt}")
            if program(ms, image):
                print("WRITTEN AND VERIFIED: flash equals the image. Unplug and re-plug the card.")
                return 0
        print("Failed 3 times. Trying to restore the previous content...")
        for attempt in (1, 2, 3):
            if program(ms, current):
                print("Previous content RESTORED and verified.")
                return 2
        print("COULD NOT RESTORE. DO NOT UNPLUG THE CARD; retry the write.")
        return 3
    finally:
        ms.close()


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("read", "info", "write"):
        raise SystemExit(__doc__)
    if args[0] == "info":
        with open(args[1], "rb") as f:
            fw = f.read()
        raise SystemExit(0 if info(fw + b"\xff" * (FLASH_SIZE - len(fw))) else 1)
    if args[0] == "write":
        if "--yes-write-flash" not in args:
            raise SystemExit("Writing requires --yes-write-flash")
        expect = args[args.index("--expect-sha256") + 1] if "--expect-sha256" in args else None
        raise SystemExit(cmd_write(args[1], expect))

    ms = Ms213x()
    try:
        print(f"Found: {ms.name}")
        ms.setup_spi()
        print("Read pass 1")
        a = ms.read_flash()
        print("Read pass 2 (comparison)")
        b = ms.read_flash()
    finally:
        ms.close()
    if a != b:
        raise SystemExit("The two reads differ. Backup is NOT reliable.")
    with open(args[1], "xb") as f:
        f.write(a)
    print(f"Saved: {args[1]} ({len(a)} bytes, two identical reads)")
    info(a)


if __name__ == "__main__":
    main()
