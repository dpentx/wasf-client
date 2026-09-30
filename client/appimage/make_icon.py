#!/usr/bin/env python3
"""Bağımlılıksız 256x256 PNG ikon üretir (repoda ikili dosya tutmamak için)."""
import struct, sys, zlib

W = H = 256
BG = (0x0F, 0x76, 0x6E)   # teal
FG = (0xF0, 0xFD, 0xFA)   # açık


def px(x, y):
    # yuvarlatılmış kare
    r = 44
    cx = min(max(x, r), W - 1 - r)
    cy = min(max(y, r), H - 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return (0, 0, 0, 0)
    # 5 ses çubuğu
    heights = [70, 120, 170, 120, 70]
    bw, gap = 20, 14
    total = 5 * bw + 4 * gap
    x0 = (W - total) // 2
    for i, h in enumerate(heights):
        bx = x0 + i * (bw + gap)
        if bx <= x < bx + bw and (H - h) // 2 <= y < (H + h) // 2:
            return FG + (255,)
    return BG + (255,)


raw = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(W)) for y in range(H))


def chunk(t, d):
    c = struct.pack(">I", len(d)) + t + d
    return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)


png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw, 9))
       + chunk(b"IEND", b""))
open(sys.argv[1], "wb").write(png)
