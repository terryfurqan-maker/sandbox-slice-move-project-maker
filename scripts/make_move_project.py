#!/usr/bin/env python3
"""Build a Midland Valley Move 2016 (.move + .movd) project from sandbox maps and slice photos."""

import argparse
import base64
import colorsys
import datetime
import gzip
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import time
from pathlib import Path
from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# CRC32C (Castagnoli) & LevelDB SSTable / MANIFEST writers
# ---------------------------------------------------------------------------

def _build_crc32c_table() -> list[int]:
    poly = 0x82F63B78
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ poly if (crc & 1) else (crc >> 1)
        table.append(crc)
    return table


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes) -> int:
    """Compute Castagnoli CRC32C checksum."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32C_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def mask_crc(crc: int) -> int:
    """Apply LevelDB CRC masking."""
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Minimal Protobuf codec
# ---------------------------------------------------------------------------

def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read an unsigned base-128 varint."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def parse_pb(buf: bytes) -> list[tuple[int, int, object]]:
    """Parse top-level Protobuf fields as (field_number, wire_type, value)."""
    pos = 0
    fields = []
    while pos < len(buf):
        tag, pos = read_varint(buf, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, pos = read_varint(buf, pos)
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            val = buf[pos:pos + 4]
            pos += 4
        else:
            break
        fields.append((fn, wt, val))
    return fields


def enc_varint(v: int) -> bytes:
    """Encode a non-negative integer as a Protobuf varint."""
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def pb_field(fn: int, wt: int, val: bytes) -> bytes:
    return enc_varint((fn << 3) | wt) + val


def pb_raw(fn: int, wt: int, val) -> bytes:
    if wt == 0:
        return pb_varint(fn, val)
    if wt == 2:
        return pb_bytes(fn, val)
    return pb_field(fn, wt, val)


def pb_varint(fn: int, v: int) -> bytes:
    return pb_field(fn, 0, enc_varint(v))


def pb_double(fn: int, d: float) -> bytes:
    return pb_field(fn, 1, struct.pack("<d", d))


def pb_float(fn: int, f: float) -> bytes:
    return pb_field(fn, 5, struct.pack("<f", f))


def pb_bytes(fn: int, b: bytes) -> bytes:
    return pb_field(fn, 2, enc_varint(len(b)) + b)


def pb_str(fn: int, s: str) -> bytes:
    return pb_bytes(fn, s.encode("utf-8"))


# ---------------------------------------------------------------------------
# LevelDB writer (uncompressed SSTable + MANIFEST + CURRENT)
# ---------------------------------------------------------------------------

def _build_block(items: list[tuple[bytes, bytes]], restart_interval: int = 16) -> bytes:
    buf = bytearray()
    restarts = []
    prev_key = b""
    for idx, (k, v) in enumerate(items):
        if idx % restart_interval == 0:
            restarts.append(len(buf))
            shared = 0
        else:
            shared = 0
            limit = min(len(prev_key), len(k))
            while shared < limit and prev_key[shared] == k[shared]:
                shared += 1
        unshared = len(k) - shared
        buf.extend(enc_varint(shared))
        buf.extend(enc_varint(unshared))
        buf.extend(enc_varint(len(v)))
        buf.extend(k[shared:])
        buf.extend(v)
        prev_key = k
    for r in restarts:
        buf.extend(struct.pack("<I", r))
    buf.extend(struct.pack("<I", len(restarts)))
    return bytes(buf)


def write_sst(path: Path, entries: list[tuple[bytes, bytes]]) -> tuple[bytes, bytes, int]:
    """Write an uncompressed LevelDB SSTable from sorted (internal_key, value) pairs."""
    file_buf = bytearray()
    index_items = []
    cur_items = []
    cur_size = 0

    def flush_block():
        nonlocal cur_items, cur_size
        if not cur_items:
            return
        raw = _build_block(cur_items)
        off = len(file_buf)
        sz = len(raw)
        file_buf.extend(raw)
        file_buf.append(0)  # uncompressed
        file_buf.extend(struct.pack("<I", mask_crc(crc32c(raw + b"\x00"))))
        index_items.append((cur_items[-1][0], enc_varint(off) + enc_varint(sz)))
        cur_items = []
        cur_size = 0

    for k, v in entries:
        cur_items.append((k, v))
        cur_size += len(k) + len(v) + 8
        if cur_size >= 4096:
            flush_block()
    flush_block()

    meta_raw = _build_block([])
    meta_off = len(file_buf)
    file_buf.extend(meta_raw)
    file_buf.append(0)
    file_buf.extend(struct.pack("<I", mask_crc(crc32c(meta_raw + b"\x00"))))

    idx_raw = _build_block(index_items)
    idx_off = len(file_buf)
    file_buf.extend(idx_raw)
    file_buf.append(0)
    file_buf.extend(struct.pack("<I", mask_crc(crc32c(idx_raw + b"\x00"))))

    handles = (
        enc_varint(meta_off) + enc_varint(len(meta_raw)) +
        enc_varint(idx_off) + enc_varint(len(idx_raw))
    )
    footer = handles.ljust(40, b"\x00") + struct.pack("<Q", 0xDB4775248B80FB57)
    file_buf.extend(footer)

    path.write_bytes(file_buf)
    return entries[0][0], entries[-1][0], len(file_buf)


def write_manifest(
    path: Path, file_num: int, file_size: int, smallest_ikey: bytes, largest_ikey: bytes, last_seq: int
) -> None:
    """Write a LevelDB MANIFEST log pointing to a single level-0 SSTable."""
    ve = bytearray()
    comp = b"leveldb.BytewiseComparator"
    ve.extend(enc_varint(1) + enc_varint(len(comp)) + comp)
    ve.extend(enc_varint(2) + enc_varint(0))
    ve.extend(enc_varint(3) + enc_varint(file_num + 2))
    ve.extend(enc_varint(4) + enc_varint(last_seq))
    ve.extend(enc_varint(7) + enc_varint(0) + enc_varint(file_num) + enc_varint(file_size))
    ve.extend(enc_varint(len(smallest_ikey)) + smallest_ikey)
    ve.extend(enc_varint(len(largest_ikey)) + largest_ikey)

    data = bytes(ve)
    crc = mask_crc(crc32c(b"\x01" + data))
    path.write_bytes(struct.pack("<IHB", crc, len(data), 1) + data)


# ---------------------------------------------------------------------------
# Transect line detection on sandbox map image
# ---------------------------------------------------------------------------

def _natural_sort_key(path: Path):
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", path.stem)]


def _fit_x_linear(pts: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pts)
    my = sum(y for _, y in pts) / n
    mx = sum(x for x, _ in pts) / n
    denom = sum((y - my) ** 2 for _, y in pts)
    a = sum((y - my) * (x - mx) for x, y in pts) / denom if denom else 0.0
    b = mx - a * my
    return a, b


def detect_transects_from_map(
    map_path: Path, expected_count: int, preview_path: Path | None = None
) -> list[dict]:
    """Detect annotated colored transect lines T1..TN on a top-view sandbox map image.

    Returns a list sorted right-to-left (T1..TN) with Move Y-up coordinates scaled in pixels:
    [{"name": "L1", "top": [x, y], "bot": [x, y], "rgb": [r, g, b]}, ...].
    """
    im = Image.open(map_path).convert("RGB")
    w, h = im.size
    px = im.load()

    y_scan_min = int(h * 0.14)
    y_scan_max = int(h * 0.72)

    def circ_mean_deg(angles_deg: list[float]) -> float:
        sx = sum(math.cos(math.radians(a)) for a in angles_deg)
        sy = sum(math.sin(math.radians(a)) for a in angles_deg)
        return math.degrees(math.atan2(sy, sx)) % 360.0

    # Collect thin saturated horizontal runs per scanline
    row_clusters: list[tuple[int, float, float, tuple[int, int, int]]] = []
    for y in range(y_scan_min, y_scan_max, 3):
        run: list[tuple[int, float, tuple[int, int, int]]] = []
        for x in range(int(w * 0.10), int(w * 0.96)):
            r, g, b = px[x, y]
            hue, sat, val = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if sat > 0.45 and val > 0.35:
                if not run or x - run[-1][0] <= 3:
                    run.append((x, hue * 360.0, (r, g, b)))
                else:
                    if 2 <= len(run) <= 12 and (run[-1][0] - run[0][0]) <= 12:
                        cx = sum(i[0] for i in run) / len(run)
                        mh = circ_mean_deg([i[1] for i in run])
                        mid_rgb = run[len(run) // 2][2]
                        row_clusters.append((y, cx, mh, mid_rgb))
                    run = [(x, hue * 360.0, (r, g, b))]
        if 2 <= len(run) <= 12 and (run[-1][0] - run[0][0]) <= 12:
            cx = sum(i[0] for i in run) / len(run)
            mh = circ_mean_deg([i[1] for i in run])
            mid_rgb = run[len(run) // 2][2]
            row_clusters.append((y, cx, mh, mid_rgb))

    # Estimate dominant line slope dx/dy from nearest vertical neighbor pairs across rows
    # Group hits into tracks using nearest-expected-x greedy tracking with adaptive slope
    rows_by_y: dict[int, list[tuple[float, float, tuple[int, int, int]]]] = {}
    for y, cx, mh, rgb in row_clusters:
        rows_by_y.setdefault(y, []).append((cx, mh, rgb))

    # Seed tracks from middle row band where all lines are clearly separated
    mid_y = (y_scan_min + y_scan_max) // 2
    seed_rows = [y for y in sorted(rows_by_y) if abs(y - mid_y) <= 15]
    best_seed_y = max(seed_rows, key=lambda yy: len(rows_by_y[yy]))
    seeds = sorted(rows_by_y[best_seed_y], key=lambda t: t[0])

    def hue_dist(h1: float, h2: float) -> float:
        d = abs(h1 - h2)
        return min(d, 360.0 - d)

    # Track upward and downward from seed row
    tracks: list[dict] = []
    for sx, shue, srgb in seeds:
        pts = [(sx, float(best_seed_y))]
        rgbs = [srgb]
        # Walk up then down, updating linear slope as points accumulate
        for direction in (-1, 1):
            cur_x = sx
            slope = 0.34  # initial mild guess; refined once >= 4 points
            ys_dir = [y for y in sorted(rows_by_y, reverse=(direction < 0)) if (y - best_seed_y) * direction > 0]
            last_y = best_seed_y
            for yy in ys_dir:
                if len(pts) >= 5:
                    slope, _ = _fit_x_linear(pts)
                pred_x = cur_x + slope * (yy - last_y)
                cands = [
                    c for c in rows_by_y[yy]
                    if abs(c[0] - pred_x) < 18 and hue_dist(c[1], shue) < 40
                ]
                if cands:
                    best_c = min(cands, key=lambda c: abs(c[0] - pred_x))
                    pts.append((best_c[0], float(yy)))
                    rgbs.append(best_c[2])
                    cur_x = best_c[0]
                    last_y = yy
        y_vals = [p[1] for p in pts]
        if len(pts) >= 40 and (max(y_vals) - min(y_vals)) >= 0.30 * h:
            tracks.append({"pts": pts, "rgb": rgbs[len(rgbs) // 2]})

    # Keep the strongest tracks if extra spurious tracks appeared
    tracks.sort(key=lambda t: len(t["pts"]), reverse=True)
    if len(tracks) > expected_count:
        tracks = tracks[:expected_count]

    # Sort right-to-left (T1 is rightmost -> T8 is leftmost)
    tracks.sort(key=lambda t: sum(x for x, _ in t["pts"]) / len(t["pts"]), reverse=True)

    if len(tracks) != expected_count:
        raise RuntimeError(
            f"Detected {len(tracks)} transect lines on {map_path.name}, expected {expected_count}. "
            "Pass --transects JSON to specify endpoints manually."
        )

    vis = im.copy() if preview_path else None
    draw = ImageDraw.Draw(vis) if vis else None

    results = []
    for idx, tr in enumerate(tracks, start=1):
        a, b = _fit_x_linear(tr["pts"])
        line_ys = []
        for y in range(int(h * 0.05), int(h * 0.90)):
            cx = int(round(a * y + b))
            sat_count = 0
            for dx in range(-12, 13):
                x = cx + dx
                if 0 <= x < w:
                    r, g, bv = px[x, y]
                    _, sat, val = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, bv / 255.0)
                    if sat > 0.45 and val > 0.35:
                        sat_count += 1
            if 2 <= sat_count <= 11:
                line_ys.append(y)

        runs = []
        for y in line_ys:
            if not runs or y - runs[-1][-1] > 4:
                runs.append([y])
            else:
                runs[-1].append(y)
        best_run = max(runs, key=len)
        y_top, y_bot = best_run[0], best_run[-1]
        x_top, x_bot = a * y_top + b, a * y_bot + b

        results.append({
            "name": f"L{idx}",
            "top": [round(x_top, 2), round(h - y_top, 2)],
            "bot": [round(x_bot, 2), round(h - y_bot, 2)],
            "rgb": list(tr["rgb"]),
        })
        if draw:
            draw.line([(x_top, y_top), (x_bot, y_bot)], fill=(255, 255, 255), width=2)
            for ex, ey in [(x_top, y_top), (x_bot, y_bot)]:
                draw.ellipse([ex - 7, ey - 7, ex + 7, ey + 7], outline=(255, 255, 255), width=3)

    if vis and preview_path:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        vis.save(preview_path)

    return results


# ---------------------------------------------------------------------------
# Move 2016 object encoders
# ---------------------------------------------------------------------------

class MoveDefaults:
    """Loads bundled Move 2016 schema/template blobs from resources/move2016_defaults.json.gz."""

    def __init__(self, resource_path: Path):
        with gzip.open(resource_path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        self.db = {k: base64.b64decode(v) for k, v in raw["db"].items()}
        self.wkt_txt = raw["wkt.txt"]
        self.strat_xml = raw["StratColumn-9.xml"]


def make_chunkgeo(v0: tuple[float, float, float], v1: tuple[float, float, float]) -> bytes:
    """Encode 2 vertices in QDataStream big-endian format."""
    return (
        struct.pack(">Q", 0) + struct.pack(">ddd", *v0) + b"\x00\x00" +
        struct.pack(">Q", 1) + struct.pack(">ddd", *v1) + b"\x00\x00"
    )


def make_polyline(tmpl: bytes, obj_id: int, name: str, p0, p1, rgb=(255, 255, 255)) -> bytes:
    xmin, xmax = min(p0[0], p1[0]), max(p0[0], p1[0])
    ymin, ymax = min(p0[1], p1[1]), max(p0[1], p1[1])
    zmin, zmax = min(p0[2], p1[2]), max(p0[2], p1[2])
    b = bytearray()
    b.extend(pb_varint(1, obj_id) + pb_varint(2, 4) + pb_str(3, "PolyLine"))
    for fn, d in [(4, xmin), (5, ymin), (6, zmin), (7, xmax), (8, ymax), (9, zmax)]:
        b.extend(pb_double(fn, d))
    for fn, wt, val in parse_pb(tmpl):
        if fn == 10:
            sub = parse_pb(val)
            if sub and sub[0][0] == 1 and sub[0][2] == 82:
                b.extend(pb_bytes(10, pb_varint(1, 82) + pb_str(5, name)))
            elif sub and sub[0][0] == 1 and sub[0][2] == 133:
                b.extend(pb_bytes(10, pb_varint(1, 133) + pb_varint(7, rgb[0]) + pb_varint(8, rgb[1]) + pb_varint(9, rgb[2])))
            else:
                b.extend(pb_bytes(10, val))
    b.extend(pb_varint(11, 1) + pb_bytes(27, pb_varint(1, 2)) + pb_varint(30, 2))
    return bytes(b)


def make_post2(tmpl: bytes, obj_id: int, x: float, y: float, zmin: float, zmax: float) -> bytes:
    b = bytearray()
    b.extend(pb_varint(1, obj_id) + pb_varint(2, 10) + pb_str(3, "Post2"))
    for fn, d in [(4, x), (5, y), (6, zmin), (7, x), (8, y), (9, zmax)]:
        b.extend(pb_double(fn, d))
    for fn, wt, val in parse_pb(tmpl):
        if fn == 10:
            b.extend(pb_bytes(10, val))
    b.extend(pb_varint(11, 1) + pb_bytes(27, pb_varint(1, 2)) + pb_varint(30, 2))
    return bytes(b)


def make_setgeo(set_id: int, name: str, member_ids: list[int], idx_val: int) -> bytes:
    b = bytearray()
    b.extend(pb_varint(1, set_id) + pb_varint(2, 0) + pb_str(3, name) + pb_str(4, "Section"))
    for mid in member_ids:
        b.extend(pb_varint(5, mid))
    b.extend(pb_bytes(6, pb_varint(1, 180) + pb_varint(3, idx_val)))
    b.extend(pb_bytes(6, pb_varint(1, 133) + pb_varint(7, 255) + pb_varint(8, 255) + pb_varint(9, 255)))
    b.extend(pb_bytes(6, pb_varint(1, 82) + pb_str(5, name)))
    return bytes(b)


def make_horizontal_image(tmpl: bytes, obj_id: int, name: str, width: float, height: float) -> bytes:
    b = bytearray()
    b.extend(pb_varint(1, obj_id) + pb_varint(2, 13) + pb_str(3, "HorizontalImage"))
    for fn, d in [(4, 0.0), (5, 0.0), (6, 0.0), (7, width), (8, height), (9, 0.0)]:
        b.extend(pb_double(fn, d))
    for fn, wt, val in parse_pb(tmpl):
        if fn == 10:
            sub = parse_pb(val)
            if sub and sub[0][0] == 1 and sub[0][2] == 82:
                b.extend(pb_bytes(10, pb_varint(1, 82) + pb_str(5, name)))
            else:
                b.extend(pb_bytes(10, val))
    b.extend(pb_varint(11, 1))
    ext = pb_double(1, 0.0) + pb_double(2, 0.0) + pb_double(3, 0.0) + pb_double(4, height) + pb_double(5, width)
    b.extend(pb_bytes(22, pb_varint(1, 2) + pb_bytes(2, b"") + pb_bytes(3, ext)))
    b.extend(pb_varint(30, 4))
    return bytes(b)


def make_segy_image(
    tmpl: bytes, obj_id: int, name: str, orig_path: str, p_top: tuple[float, float], p_bot: tuple[float, float], z_min: float
) -> bytes:
    xmin, xmax = min(p_top[0], p_bot[0]), max(p_top[0], p_bot[0])
    ymin, ymax = min(p_top[1], p_bot[1]), max(p_top[1], p_bot[1])
    b = bytearray()
    b.extend(pb_varint(1, obj_id) + pb_varint(2, 12) + pb_str(3, "SegyImage"))
    for fn, d in [(4, xmin), (5, ymin), (6, z_min), (7, xmax), (8, ymax), (9, 0.0)]:
        b.extend(pb_double(fn, d))
    for fn, wt, val in parse_pb(tmpl):
        if fn == 10:
            sub = parse_pb(val)
            if sub and sub[0][0] == 1 and sub[0][2] == 82:
                b.extend(pb_bytes(10, pb_varint(1, 82) + pb_str(5, name)))
            else:
                b.extend(pb_bytes(10, val))
    b.extend(pb_varint(11, 1))
    tmpl_f22 = [val for fn, _, val in parse_pb(tmpl) if fn == 22][0]
    tmpl_f5 = [val for fn, _, val in parse_pb(tmpl_f22) if fn == 5][0]
    f5_new = bytearray()
    pt_idx = 0
    for sfn, swt, sval in parse_pb(tmpl_f5):
        if sfn == 47:
            f5_new.extend(pb_double(47, z_min))
        elif sfn == 48:
            f5_new.extend(pb_double(48, 0.0))
        elif sfn == 49:
            px, py = p_top if pt_idx == 0 else p_bot
            tex = 0.0 if pt_idx == 0 else 1.0
            f5_new.extend(pb_bytes(49, pb_double(1, px) + pb_double(2, py) + pb_float(3, tex)))
            pt_idx += 1
        else:
            f5_new.extend(pb_raw(sfn, swt, sval))
    f22 = pb_varint(1, 4) + pb_str(2, orig_path) + pb_bytes(5, bytes(f5_new))
    b.extend(pb_bytes(22, f22) + pb_varint(30, 4))
    return bytes(b)


def make_itemview(obj_id: int, name: str = "None", kind: int = 2, is_3d: bool = False) -> bytes:
    b = pb_varint(1, obj_id) + pb_varint(2, 1) + pb_float(3, 0.0) + pb_field(4, 5, struct.pack("<I", kind)) + pb_str(5, name)
    if kind == 4 and is_3d:
        b += pb_varint(6, obj_id * 2)
    return b


def make_3d_view(tmpl: bytes, view_id: int, pos_xyz: tuple[float, float, float]) -> bytes:
    b = bytearray()
    for fn, wt, val in parse_pb(tmpl):
        if fn == 1:
            b.extend(pb_varint(1, view_id))
        elif fn == 9:
            cam = bytearray()
            for cfn, cwt, cval in parse_pb(val):
                if cfn in (2, 3, 4):
                    cam.extend(pb_float(cfn, pos_xyz[cfn - 2]))
                elif cfn == 12:
                    cam.extend(pb_float(12, 2200.0))
                else:
                    cam.extend(pb_raw(cfn, cwt, cval))
            b.extend(pb_bytes(9, bytes(cam)))
        else:
            b.extend(pb_raw(fn, wt, val))
    return bytes(b)


def make_2d_map_view(tmpl: bytes, view_id: int, cam_xyz: tuple[float, float, float]) -> bytes:
    b = bytearray()
    for fn, wt, val in parse_pb(tmpl):
        if fn == 1:
            b.extend(pb_varint(1, view_id))
        elif fn == 33:
            b.extend(pb_bytes(33, pb_float(1, cam_xyz[0]) + pb_float(2, cam_xyz[1]) + pb_float(3, cam_xyz[2])))
        else:
            b.extend(pb_raw(fn, wt, val))
    return bytes(b)


def make_section_view(tmpl: bytes, view_id: int, set_id: int, set_name: str, cam_xyz: tuple[float, float, float]) -> bytes:
    b = bytearray()
    for fn, wt, val in parse_pb(tmpl):
        if fn == 1:
            b.extend(pb_varint(1, view_id))
        elif fn == 21:
            b.extend(pb_varint(21, set_id))
        elif fn == 22:
            b.extend(pb_str(22, set_name))
        elif fn == 33:
            b.extend(pb_bytes(33, pb_float(1, cam_xyz[0]) + pb_float(2, cam_xyz[1]) + pb_float(3, cam_xyz[2])))
        else:
            b.extend(pb_raw(fn, wt, val))
    return bytes(b)


# ---------------------------------------------------------------------------
# Project Builder
# ---------------------------------------------------------------------------

def _rmtree_force(path: Path) -> None:
    def _onexc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        func(p)
    if path.exists():
        shutil.rmtree(path, onexc=_onexc)


def build_move_project(
    map_path: Path,
    sections_dir: Path,
    out_dir: Path,
    project_name: str,
    map_width_m: float | None = None,
    post_depth: float = -900.0,
    transects_json: Path | None = None,
    preview_path: Path | None = None,
) -> Path:
    """Create a Move 2016 (.move + .movd) project from a map image and vertical slice photos."""
    res_path = Path(__file__).resolve().parent.parent / "resources" / "move2016_defaults.json.gz"
    defaults = MoveDefaults(res_path)

    img_exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
    sec_images = sorted(
        [p for p in sections_dir.iterdir() if p.is_file() and p.suffix.lower() in img_exts],
        key=_natural_sort_key,
    )
    if not sec_images:
        raise FileNotFoundError(f"No section images found in {sections_dir}")

    map_im = Image.open(map_path).convert("RGB")
    px_w, px_h = float(map_im.size[0]), float(map_im.size[1])
    scale = (map_width_m / px_w) if map_width_m else 1.0
    map_w = px_w * scale
    map_h = px_h * scale

    if transects_json:
        transects = json.loads(transects_json.read_text(encoding="utf-8"))
        for idx, t in enumerate(transects):
            if "image" not in t:
                t["image"] = str(sec_images[idx])
    else:
        transects = detect_transects_from_map(map_path, len(sec_images), preview_path=preview_path)
        for t, img_p in zip(transects, sec_images):
            t["top"] = [t["top"][0] * scale, t["top"][1] * scale]
            t["bot"] = [t["bot"][0] * scale, t["bot"][1] * scale]
            t["image"] = str(img_p)

    out_dir.mkdir(parents=True, exist_ok=True)
    movd_dir = out_dir / f"{project_name}.movd"
    _rmtree_force(movd_dir)
    db_dir = movd_dir / "db"
    geos_dir = movd_dir / "geos"
    db_dir.mkdir(parents=True, exist_ok=True)
    (geos_dir / "HorizontalImage" / "08").mkdir(parents=True, exist_ok=True)
    (geos_dir / "SegyImage").mkdir(parents=True, exist_ok=True)

    (movd_dir / "wkt.txt").write_text(defaults.wkt_txt, encoding="utf-8")
    (movd_dir / "StratColumn-9.xml").write_text(defaults.strat_xml, encoding="utf-8")
    map_im.save(geos_dir / "HorizontalImage" / "08" / "000000000000008.png")

    kv: dict[bytes, bytes] = {}
    for k in [
        "PropertyToSystemTypes", "SurveyCoordinates", "bookmarks", "wkt",
        "geo|000000000000001", "geo|000000000000002", "geo|000000000000003",
        "geo|000000000000004", "geo|000000000000005", "geo|000000000000006",
        "geo|000000000000007", "geo|000000000000009",
        "modelBrowserNodeDetails/Images//class mvBase::QMVSimpleFolder",
        "modelBrowserNodeDetails/Polylines//class mvBase::QMVSimpleFolder",
        "modelBrowserNodeDetails/Vertical Images//class mvBase::QMVSimpleFolder",
    ]:
        kv[k.encode()] = defaults.db[k]

    now_str = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    kv[b"model"] = (
        pb_varint(1, 1) + pb_varint(2, 1) + pb_varint(3, 48) + pb_varint(4, 9) +
        pb_str(6, f"Last saved on {now_str} by User using Move2016.1 bld 12981") +
        pb_varint(7, 2) + pb_varint(8, 2)
    )
    kv[b"model_bbox"] = (
        struct.pack(">I", 34) + "ModelBoundingBox2".encode("utf-16-be") +
        struct.pack(">ddddddddd", 0.0, 0.0, post_depth, map_w, map_h, 0.0, 200.0, 200.0, 100.0) +
        b"\x00\x00\x00\x00\x01\x01"
    )
    kv[b"viewdetails"] = (
        struct.pack(">I", 30) + "ViewMetaDetails".encode("utf-16-be") +
        struct.pack(">III", 0, 4, 20 + len(transects) * 10 + 50)
    )

    kv[b"geo|000000000000008"] = make_horizontal_image(
        defaults.db["geo|000000000000008"], 8, map_path.stem, map_w, map_h
    )
    kv[b"view|00015"] = make_3d_view(defaults.db["view|00015"], 15, (map_w * 0.48, map_h * 0.12, max(map_w, map_h) * 0.9))
    kv[b"view|00016"] = make_2d_map_view(defaults.db["view|00016"], 16, (map_w * 0.5, -map_h * 0.5, max(map_w, map_h) * 1.8))

    all_view_items = [(8, map_path.stem, 4)]
    l1_items = []
    l1_set_id = 0
    l1_len = 1000.0

    for i, tr in enumerate(transects, start=1):
        base = 20 + (i - 1) * 10
        poly_id, set_id, post1_id, post2_id, img_id = base, base + 1, base + 2, base + 3, base + 4
        sec_name = tr["name"]
        p_top = (float(tr["top"][0]), float(tr["top"][1]))
        p_bot = (float(tr["bot"][0]), float(tr["bot"][1]))
        rgb = tuple(tr.get("rgb", [255, 255, 255]))
        sec_img_path = Path(tr["image"])

        kv[f"geo|{poly_id:015d}".encode()] = make_polyline(
            defaults.db["geo|000000000000052"], poly_id, sec_name, (*p_top, 0.0), (*p_bot, 0.0), rgb
        )
        kv[f"chunkgeo|{poly_id:015d}|vertex00000".encode()] = make_chunkgeo((*p_top, 0.0), (*p_bot, 0.0))

        kv[f"geo|{post1_id:015d}".encode()] = make_post2(defaults.db["geo|000000000000056"], post1_id, *p_top, post_depth, 0.0)
        kv[f"chunkgeo|{post1_id:015d}|vertex00000".encode()] = make_chunkgeo((*p_top, 0.0), (*p_top, post_depth))

        kv[f"geo|{post2_id:015d}".encode()] = make_post2(defaults.db["geo|000000000000056"], post2_id, *p_bot, post_depth, 0.0)
        kv[f"chunkgeo|{post2_id:015d}|vertex00000".encode()] = make_chunkgeo((*p_bot, 0.0), (*p_bot, post_depth))

        sim = Image.open(sec_img_path).convert("RGB")
        sw, sh = sim.size
        seg_len = math.hypot(p_bot[0] - p_top[0], p_bot[1] - p_top[1])
        z_min = -seg_len * (sh / sw)
        img_name = sec_img_path.stem

        kv[f"geo|{img_id:015d}".encode()] = make_segy_image(
            defaults.db["geo|000000000000101"], img_id, img_name, str(sec_img_path), p_top, p_bot, z_min
        )
        sub_dir = geos_dir / "SegyImage" / f"{img_id % 100:02d}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        sim.save(sub_dir / f"{img_id:015d}.png")

        kv[f"setgeo|{set_id:015d}".encode()] = make_setgeo(set_id, sec_name, [poly_id, post1_id, post2_id, img_id], (i - 1) * 2)
        all_view_items.extend([
            (poly_id, sec_name, 2),
            (post1_id, "Post", 2),
            (post2_id, "Post", 2),
            (img_id, img_name, 4),
        ])
        if i == 1:
            l1_set_id = set_id
            l1_len = seg_len
            l1_items = [(poly_id, "None", 2), (post1_id, "None", 2), (post2_id, "None", 2), (img_id, "None", 4)]

    kv[b"view|00018"] = make_section_view(defaults.db["view|00018"], 18, l1_set_id, transects[0]["name"], (l1_len * 0.5, -90.0, l1_len * 1.9))
    for oid, oname, okind in all_view_items:
        kv[f"itemview|00015|{oid:05d}".encode()] = make_itemview(oid, oname, okind, is_3d=True)
        kv[f"itemview|00016|{oid:05d}".encode()] = make_itemview(oid, oname if oid == 8 else "None", okind, is_3d=False)
    for oid, oname, okind in l1_items:
        kv[f"itemview|00018|{oid:05d}".encode()] = make_itemview(oid, oname, okind, is_3d=False)

    entries = []
    for seq, uk in enumerate(sorted(kv.keys()), start=1):
        entries.append((uk + struct.pack("<Q", (seq << 8) | 1), kv[uk]))

    sk, lk, fsz = write_sst(db_dir / "000005.sst", entries)
    write_manifest(db_dir / "MANIFEST-000002", 5, fsz, sk, lk, len(entries))
    (db_dir / "CURRENT").write_bytes(b"MANIFEST-000002\n")

    movd_unix = str(movd_dir).replace("\\", "/")
    mve_xml = (
        f"<!DOCTYPE MVE>\n"
        f'<MVE minorVersion="2" creator="Move 2016.1.0 build 12981" majorVersion="2" date="{now_str}">\n'
        f" <project>{movd_unix}</project>\n"
        f"</MVE>"
    )
    move_path = out_dir / f"{project_name}.move"
    move_path.write_text(mve_xml, encoding="utf-8")
    (movd_dir / "index.move").write_text(mve_xml, encoding="utf-8")
    print(f"Created Move project: {move_path} ({len(transects)} sections, {len(entries)} DB entries)")
    return move_path


def verify_in_move(move_path: Path, capture_dir: Path | None = None) -> None:
    """Open the generated .move file in Move 2016.1 (Windows) to verify clean loading."""
    import ctypes
    from ctypes import wintypes

    move_exe = Path(r"C:\Program Files\Midland Valley\Move2016.1\bin\Move.exe")
    if not move_exe.exists():
        raise FileNotFoundError(f"Move.exe not found at {move_exe}")

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    os.system("taskkill /F /IM Move.exe >nul 2>&1")
    time.sleep(0.5)

    def find_windows(pid):
        found = []
        def cb(h, _):
            if user32.IsWindowVisible(h):
                wpid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
                if wpid.value == pid:
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(h, buf, 256)
                    found.append((h, buf.value))
            return True
        proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc_t(cb), 0)
        return found

    def capture(hwnd, out_png: Path):
        rc = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        hdc = user32.GetWindowDC(hwnd)
        mfc_dc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mfc_dc, bmp)
        user32.PrintWindow(hwnd, mfc_dc, 2)
        class BMI(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
            ]
        bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).save(out_png)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hdc)

    proc = subprocess.Popen([str(move_exe), str(move_path)])
    try:
        time.sleep(10)
        wins = find_windows(proc.pid)
        print("Move windows:", wins)
        for wh, wt in wins:
            if "Import Summary" in wt:
                if capture_dir:
                    capture_dir.mkdir(parents=True, exist_ok=True)
                    capture(wh, capture_dir / "verify_import_summary.png")
                user32.PostMessageW(wh, 0x0010, 0, 0)
                time.sleep(2.5)
        for wh, wt in find_windows(proc.pid):
            if "Move2016" in wt:
                if capture_dir:
                    capture(wh, capture_dir / "verify_move_main.png")
                print("Verified loaded window:", wt)
    finally:
        os.system("taskkill /F /IM Move.exe >nul 2>&1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Move 2016 project from sandbox map + vertical slice photos.")
    parser.add_argument("--map", required=True, type=Path, help="Path to top-view map image")
    parser.add_argument("--sections-dir", required=True, type=Path, help="Folder of vertical slice images")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output folder (default: parent of --map)")
    parser.add_argument("--name", default="sandbox_project", help="Project name (without .move)")
    parser.add_argument("--map-width", type=float, default=None, help="Map horizontal width in meters (default: pixel width)")
    parser.add_argument("--post-depth", type=float, default=-900.0, help="Bottom Z of section posts (default: -900.0)")
    parser.add_argument("--transects", type=Path, default=None, help="Optional JSON file with explicit transect coordinates")
    parser.add_argument("--preview", type=Path, default=None, help="Optional PNG path to save transect detection overlay")
    parser.add_argument("--verify", action="store_true", help="Open generated .move in Move 2016 to verify load")
    args = parser.parse_args()

    out_dir = args.out_dir or args.map.parent
    move_path = build_move_project(
        map_path=args.map,
        sections_dir=args.sections_dir,
        out_dir=out_dir,
        project_name=args.name,
        map_width_m=args.map_width,
        post_depth=args.post_depth,
        transects_json=args.transects,
        preview_path=args.preview,
    )
    if args.verify:
        cap_dir = args.preview.parent if args.preview else out_dir
        verify_in_move(move_path, capture_dir=cap_dir)


if __name__ == "__main__":
    main()
