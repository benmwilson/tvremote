"""Draws the logo (same shapes as app/logo.svg) into app/icon-180.png and app/icon-512.png with only the standard library.
Run from the repo root: python tools/make_icons.py"""
import math
import struct
import zlib

BG, SCREEN = (11, 14, 20), (19, 26, 40)
C0, C1 = (140, 200, 255), (79, 143, 255)


def sd_round_rect(px, py, cx, cy, hx, hy, r):
    qx, qy = abs(px - cx) - (hx - r), abs(py - cy) - (hy - r)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


TRI = [(44, 36), (44, 56), (61, 46)]


def tri_dist(px, py):
    sides = [(TRI[(i + 1) % 3][0] - TRI[i][0]) * (py - TRI[i][1]) - (TRI[(i + 1) % 3][1] - TRI[i][1]) * (px - TRI[i][0])
             for i in range(3)]
    inside = all(v >= 0 for v in sides) or all(v <= 0 for v in sides)  # either winding
    return 0 if inside else min(seg_dist(px, py, *TRI[i], *TRI[(i + 1) % 3]) for i in range(3))


def shade(x, y):
    """(r, g, b) of the logo at a point of the 100x100 design."""
    t = min(1, max(0, (x + y) / 200))
    grad = tuple(round(a + (b - a) * t) for a, b in zip(C0, C1))
    d = sd_round_rect(x, y, 50, 46, 36, 24, 12)
    if abs(d) <= 3 or tri_dist(x, y) <= 2.5 or seg_dist(x, y, 38, 82, 62, 82) <= 3 or seg_dist(x, y, 50, 73, 50, 82) <= 3:
        return grad
    return SCREEN if d < 0 else BG


def png(size, ss=3):
    rows = []
    for j in range(size):
        row = bytearray([0])
        for i in range(size):
            acc = [0, 0, 0]
            for sj in range(ss):
                for si in range(ss):
                    c = shade((i + (si + .5) / ss) * 100 / size, (j + (sj + .5) / ss) * 100 / size)
                    for k in range(3):
                        acc[k] += c[k]
            row += bytes(v // (ss * ss) for v in acc)
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for n in (180, 512):
        open(f"app/icon-{n}.png", "wb").write(png(n))
        print("wrote", n)
