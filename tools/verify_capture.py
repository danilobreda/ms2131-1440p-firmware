#!/usr/bin/env python3
"""Capture from the card in several modes and report real fps, frame size and corruption.

Usage:
    python verify_capture.py [--regs] [--save]

  --regs   also read (read-only, over HID, while each stream is running) the detected HDMI
           input, the frame memory size and the frame memory line pitch
  --save   save one PNG per mode in the current directory

Requires: pip install opencv-python-headless numpy   (and hidapi for --regs)
Windows / DirectShow. Close OBS or anything else using the card first.

Reading the --regs output: "real picture stored" is the image the chip keeps in its frame
memory, which is what the capture is built from whatever the USB frame size says. With an
HDMI input above 170 MHz (for example 2560x1440@60) the firmware halves the stored width.
"""
import os
import sys
import time

import cv2
import numpy as np

MODES = [(2560, 1440, 30, "YUY2"), (2560, 1440, 30, "MJPG"), (1920, 1080, 60, "YUY2"),
         (1920, 1080, 60, "MJPG"), (1600, 1200, 60, "YUY2"), (1280, 720, 60, "YUY2")]


def find_card():
    """The card is the DirectShow device that accepts 1600x1200 YUY2 (most webcams do not)."""
    for idx in range(6):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUY2"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1600)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1200)
        ok = (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) == (1600, 1200)
        cap.release()
        if ok:
            return idx
    raise SystemExit("Capture card not found in DirectShow (is another program using it?)")


def garbage_rows(frame):
    """Rows that look like unwritten frame memory: noisy and green (YUV 0,0,0)."""
    g = frame[:, :800, 1].astype(np.int16)
    r = frame[:, :800, 2].astype(np.int16)
    noisy = (np.abs(np.diff(g, axis=1)).mean(axis=1) > 12) & ((g.mean(axis=1) - r.mean(axis=1)) > 25)
    return int(noisy.sum())


def read_regs():
    """Return a one-line description of the chip state. Call while a stream is running."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ms2131_flash import Ms213x
    ms = Ms213x()
    try:
        def blk(addr, n):
            return bytes(ms.read_reg(a) for a in range(addr, addr + n))
        inp, mem, pitch, lim = blk(0x22B3, 14), blk(0xF660, 4), blk(0xF597, 2), blk(0x268C, 4)
    finally:
        ms.close()
    in_w, in_h = int.from_bytes(inp[5:7], "big"), int.from_bytes(inp[7:9], "big")
    pclk = int.from_bytes(inp[9:11], "big") / 100
    mem_w, mem_h = int.from_bytes(mem[0:2], "little"), int.from_bytes(mem[2:4], "little")
    p = int.from_bytes(pitch, "little")
    note = " (width halved: input above 170 MHz)" if pclk > 170.0 else ""
    return (f"input {in_w}x{in_h} {pclk:.1f} MHz, real picture stored {mem_w}x{mem_h}{note}, "
            f"pitch {p} = {p * 2} slots per line ({(1 << 21) // (p * 2)} lines fit), "
            f"limit {int.from_bytes(lim[:2], 'big')}x{int.from_bytes(lim[2:], 'big')}")


def main():
    idx = find_card()
    for w, h, fps, fcc in MODES:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        frame, t_end = None, time.time() + 2          # discard start-up
        while time.time() < t_end:
            ok, f = cap.read()
            if ok:
                frame = f
        n, t0, worst = 0, time.time(), 0
        while time.time() - t0 < 5:
            ok, f = cap.read()
            if ok:
                n += 1
                frame = f
                if n % 10 == 0:
                    worst = max(worst, garbage_rows(f))
        dt = time.time() - t0
        regs = read_regs() if "--regs" in sys.argv and frame is not None else None
        cap.release()
        if frame is None:
            print(f"{w}x{h}@{fps} {fcc}: NO FRAME")
            continue
        if "--save" in sys.argv:
            cv2.imwrite(f"capture_{w}x{h}_{fcc}.png", frame)
        print(f"{w}x{h}@{fps} {fcc}: {n / dt:.2f} fps, frame {frame.shape[1]}x{frame.shape[0]}, "
              f"corrupt rows {worst}, mean brightness {frame.mean():.1f}")
        if regs:
            print(f"    {regs}")


if __name__ == "__main__":
    main()
