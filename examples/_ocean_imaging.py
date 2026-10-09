"""Image code for the ocean-watch example (standard library only).

``ocean_watch.py`` is the flow; this module holds the pixel work it calls. The
leading underscore keeps ``miniflow ui --flows examples`` from offering it as a flow.

How satellite radar (SAR) sees the sea: wind ripples scatter the radar pulse back,
so open water is mid-grey with a grainy "speckle". Oil calms those ripples, so a
spill shows up as a dark patch. A ship's metal hull reflects strongly, so it is a
small bright spot. The tiles here are synthetic, and :func:`make_tile` records
where it put every ship and spill, so the report can check the detector against
the right answers.
"""

from __future__ import annotations

import base64
import csv
import html
import json
import math
import random
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

SIZE = 256  # tile width and height in pixels
PIXEL_M = 20  # metres per pixel, so a tile covers 5.12 x 5.12 km
SUSPECT_M = 500  # a ship this close to a spill may be the one that caused it
THUMB = SIZE // 2  # report thumbnails are shown at half size
_VERSION = 2  # bump to regenerate tiles made by an older make_tile

Tile = dict[str, Any]
Truth = dict[str, list[list[float]]]


# --------------------------------------------------------------------- PNG
def png_bytes(pixels: bytes, width: int, height: int, channels: int = 1) -> bytes:
    """Encode 8-bit grayscale (``channels=1``) or RGB (``channels=3``) pixels as PNG."""
    stride = width * channels
    raw = b"".join(b"\x00" + pixels[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    color = {1: 0, 3: 2}[channels]
    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, pixels: bytes, width: int, height: int, channels: int = 1) -> None:
    path.write_bytes(png_bytes(pixels, width, height, channels))


def read_png(path: Path) -> tuple[bytes, int, int]:
    """Read a grayscale PNG written by :func:`write_png`: ``(pixels, width, height)``."""
    data = path.read_bytes()
    pos, idat = 8, b""
    width = height = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            if (depth, color) != (8, 0):
                raise ValueError(f"{path}: expected an 8-bit grayscale PNG")
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows = []
    for y in range(height):
        start = y * (width + 1)
        if raw[start] != 0:
            raise ValueError(f"{path}: unsupported PNG row filter")
        rows.append(raw[start + 1 : start + 1 + width])
    return b"".join(rows), width, height


# ------------------------------------------------------------------- tiles
def _ellipse(cx: float, cy: float, a: float, b: float, angle: float) -> list[int]:
    """Pixel indices inside a rotated ellipse."""
    cos, sin = math.cos(angle), math.sin(angle)
    r = math.ceil(a) + 1
    out = []
    for y in range(max(0, int(cy) - r), min(SIZE, int(cy) + r + 1)):
        for x in range(max(0, int(cx) - r), min(SIZE, int(cx) + r + 1)):
            dx, dy = x - cx, y - cy
            u, v = dx * cos + dy * sin, -dx * sin + dy * cos
            if (u / a) ** 2 + (v / b) ** 2 <= 1:
                out.append(y * SIZE + x)
    return out


def _far_from(x: float, y: float, pixels: list[int], gap: float) -> bool:
    return all((p % SIZE - x) ** 2 + (p // SIZE - y) ** 2 >= gap * gap for p in pixels)


def make_tile(index: int) -> tuple[bytes, Truth]:
    """Synthetic radar tile ``index`` and where its ships and spills really are.

    The same index always gives the same tile.
    """
    rng = random.Random(f"ocean-tile-{index}")
    level = rng.randint(80, 120)  # sea state: a windier sea looks brighter
    value = [float(level)] * (SIZE * SIZE)
    truth: Truth = {"ships": [], "spills": []}

    spill_pixels: list[int] = []
    for _ in range(rng.choices([0, 1, 2], weights=[62, 30, 8])[0]):
        cx, cy = rng.uniform(55, SIZE - 55), rng.uniform(55, SIZE - 55)
        if any(math.dist((cx, cy), s) < 100 for s in truth["spills"]):
            continue
        a, b = rng.uniform(20, 44), rng.uniform(7, 14)
        slick = _ellipse(cx, cy, a, b, rng.uniform(0, math.pi))
        for p in slick:
            value[p] = level * 0.3  # oil damps the ripples: little radar comes back
        spill_pixels += slick
        truth["spills"].append([round(cx, 1), round(cy, 1)])

    for p in range(SIZE * SIZE):  # speckle: radar images are grainy
        value[p] *= 1 + 0.2 * rng.gauss(0, 1)

    wanted = rng.choices([0, 1, 2, 3, 4], weights=[28, 30, 22, 12, 8])[0]
    if truth["spills"] and rng.random() < 0.5:
        wanted += 1  # often there is a ship near a slick: the polluter, perhaps
    for _ in range(wanted * 20):
        if len(truth["ships"]) == wanted:
            break
        if truth["spills"] and not truth["ships"] and rng.random() < 0.7:
            sx, sy = rng.choice(truth["spills"])
            x, y = sx + rng.uniform(-60, 60), sy + rng.uniform(-60, 60)
        else:
            x, y = rng.uniform(8, SIZE - 9), rng.uniform(8, SIZE - 9)
        if not (8 <= x <= SIZE - 9 and 8 <= y <= SIZE - 9):
            continue
        if any(math.dist((x, y), s) < 14 for s in truth["ships"]):
            continue
        if not _far_from(x, y, spill_pixels, 10):
            continue
        brightness = rng.uniform(220, 255)
        for p in _ellipse(
            x, y, rng.uniform(2.5, 4), rng.uniform(1.7, 2.2), rng.uniform(0, math.pi)
        ):
            value[p] = brightness
        truth["ships"].append([round(x, 1), round(y, 1)])

    return bytes(max(0, min(255, round(v))) for v in value), truth


def prepare(folder: Path, count: int) -> str:
    """Make ``count`` tiles in ``folder`` unless they are already there; return the folder."""
    manifest = folder / "manifest.json"
    if manifest.exists():
        info = json.loads(manifest.read_text(encoding="utf-8"))
        if info.get("count") == count and info.get("version") == _VERSION:
            return str(folder)
    folder.mkdir(parents=True, exist_ok=True)
    truth = {}
    for i in range(count):
        pixels, truth[f"tile_{i:03d}"] = make_tile(i)
        write_png(folder / f"tile_{i:03d}.png", pixels, SIZE, SIZE)
    info = {"count": count, "version": _VERSION, "pixel_m": PIXEL_M, "truth": truth}
    manifest.write_text(json.dumps(info), encoding="utf-8")
    return str(folder)


@lru_cache(maxsize=4)
def _truth(folder: str) -> dict[str, Truth]:
    info = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
    truth: dict[str, Truth] = info["truth"]
    return truth


def load_tile(folder: str, index: int) -> Tile:
    name = f"tile_{index:03d}"
    pixels, _, _ = read_png(Path(folder) / f"{name}.png")
    return {"tile": name, "pixels": pixels, "truth": _truth(folder)[name]}


def batch(count: int, batches: int, number: int) -> range:
    """Tile indices in batch ``number`` (1-based) of ``count`` tiles split ``batches`` ways."""
    size = math.ceil(count / batches)
    return range((number - 1) * size, min(count, number * size))


# --------------------------------------------------------------- detection
def _median(px: bytes | list[int], r: int) -> list[int]:
    """Median over a (2r+1) square around each pixel: removes speckle, keeps edges sharp."""
    n, w = SIZE, 2 * r + 1
    rows = [list(px[min(max(y, 0), n - 1) * n :][:n]) for y in range(-r, n + r)]
    padded = [[row[0]] * r + row + [row[-1]] * r for row in rows]
    middle = w * w // 2
    out = [0] * (n * n)
    for y in range(n):
        band = padded[y : y + w]
        for x in range(n):
            window = []
            for row in band:
                window += row[x : x + w]
            window.sort()
            out[y * n + x] = window[middle]
    return out


def _box_mean(px: list[int], r: int) -> list[float]:
    """Mean over a (2r+1) square around each pixel, using a summed-area table."""
    n = SIZE
    table = [[0] * (n + 1) for _ in range(n + 1)]
    for y in range(n):
        run = 0
        row, above = table[y + 1], table[y]
        for x in range(n):
            run += px[y * n + x]
            row[x + 1] = above[x + 1] + run
    out = [0.0] * (n * n)
    for y in range(n):
        y0, y1 = max(0, y - r), min(n, y + r + 1)
        for x in range(n):
            x0, x1 = max(0, x - r), min(n, x + r + 1)
            total = table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]
            out[y * n + x] = total / ((y1 - y0) * (x1 - x0))
    return out


def _components(mask: bytearray) -> list[list[int]]:
    """Groups of touching pixels (4-connected) where ``mask`` is set."""
    n = SIZE
    seen = bytearray(n * n)
    groups = []
    for start in range(n * n):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack, group = [start], []
        while stack:
            p = stack.pop()
            group.append(p)
            x, y = p % n, p // n
            for q, ok in ((p - 1, x > 0), (p + 1, x < n - 1), (p - n, y > 0), (p + n, y < n - 1)):
                if ok and mask[q] and not seen[q]:
                    seen[q] = 1
                    stack.append(q)
        groups.append(group)
    return groups


def _shape(group: list[int]) -> dict[str, Any]:
    xs = [p % SIZE for p in group]
    ys = [p // SIZE for p in group]
    k = len(group)
    cx, cy = sum(xs) / k, sum(ys) / k
    sxx = sum((x - cx) ** 2 for x in xs) / k
    syy = sum((y - cy) ** 2 for y in ys) / k
    sxy = sum((x - cx) * (y - cy) for x, y in zip(xs, ys, strict=True)) / k
    spread = math.sqrt(((sxx - syy) / 2) ** 2 + sxy**2)
    major, minor = (sxx + syy) / 2 + spread, max((sxx + syy) / 2 - spread, 0.25)
    return {
        "x": round(cx, 1),
        "y": round(cy, 1),
        "area_px": k,
        "box": [min(xs), min(ys), max(xs), max(ys)],
        "elongation": round(math.sqrt(major / minor), 2),
    }


def detect(pixels: bytes) -> dict[str, Any]:
    """Find ships (bright spots) and oil spills (large dark patches) in one tile."""
    clean = _median(pixels, 1)  # 3 x 3: speckle gone, small ships kept
    sea = sorted(clean)[len(clean) // 2]  # typical brightness of this tile's sea

    bright = bytearray(v > max(180, 1.8 * sea) for v in clean)
    ships = [_shape(g) for g in _components(bright) if 3 <= len(g) <= 150]
    for s in ships:
        del s["elongation"]

    smooth = _box_mean(_median(pixels, 2), 2)  # stronger smoothing to outline slicks
    dark = bytearray(v < 0.6 * sea for v in smooth)
    spills, slick_pixels = [], []
    for g in _components(dark):
        if len(g) < 150:
            continue
        shape = _shape(g)
        mean = sum(clean[p] for p in g) / len(g)
        shape["area_km2"] = round(len(g) * (PIXEL_M / 1000) ** 2, 4)
        shape["contrast"] = round(1 - mean / sea, 2)  # 1 = no radar return at all
        spills.append(shape)
        slick_pixels += g

    reach = SUSPECT_M / PIXEL_M
    suspects = [
        s for s in ships if slick_pixels and not _far_from(s["x"], s["y"], slick_pixels, reach)
    ]
    return {"sea_level": sea, "ships": ships, "spills": spills, "suspects": suspects}


def score(found: dict[str, Any], truth: Truth) -> dict[str, dict[str, int]]:
    """Compare detections with the known answers: found, missed and false alarms."""
    out = {}
    for kind, radius in (("ships", 5.0), ("spills", 20.0)):
        unmatched = [(d["x"], d["y"]) for d in found[kind]]
        hits = 0
        for t in truth[kind]:
            near = [d for d in unmatched if math.dist(d, t) <= radius]
            if near:
                unmatched.remove(min(near, key=lambda d: math.dist(d, t)))
                hits += 1
        out[kind] = {
            "found": hits,
            "missed": len(truth[kind]) - hits,
            "false_alarms": len(unmatched),
        }
    return out


def analyse(tile: Tile) -> dict[str, Any]:
    """Detections, features and accuracy for one tile (without its pixels)."""
    found = detect(tile["pixels"])
    spills = found["spills"]
    priority = "HIGH" if found["suspects"] else "ALERT" if spills else ""
    return {
        "tile": tile["tile"],
        "sea_level": found["sea_level"],
        "ships": len(found["ships"]),
        "spills": len(spills),
        "spill_area_km2": round(sum(s["area_km2"] for s in spills), 4),
        "max_elongation": max((s["elongation"] for s in spills), default=0),
        "suspect_ships": len(found["suspects"]),
        "priority": priority,
        "true_ships": len(tile["truth"]["ships"]),
        "true_spills": len(tile["truth"]["spills"]),
        "score": score(found, tile["truth"]),
        "found": found,
    }


# ------------------------------------------------------------------ report
def summarise(records: list[dict[str, Any]], folder: Path) -> dict[str, Any]:
    records = sorted(records, key=lambda r: r["tile"])
    totals = {
        kind: {
            k: sum(r["score"][kind][k] for r in records)
            for k in ("found", "missed", "false_alarms")
        }
        for kind in ("ships", "spills")
    }
    return {
        "tiles": len(records),
        "area_km2": round(len(records) * (SIZE * PIXEL_M / 1000) ** 2, 1),
        **totals,
        "alerts": [r for r in records if r["priority"]],
        "records": records,
        "folder": str(folder),
    }


_CSV_FIELDS = [
    "tile", "priority", "ships", "spills", "spill_area_km2", "max_elongation",
    "suspect_ships", "sea_level", "true_ships", "true_spills",
]  # fmt: skip


def annotate(pixels: bytes, found: dict[str, Any]) -> bytes:
    """Half-size RGB copy of a tile with boxes: red = spill, green = ship, yellow = suspect."""
    rgb = bytearray(b for v in pixels for b in (v, v, v))
    suspects = {(s["x"], s["y"]) for s in found["suspects"]}
    boxes = [(s["box"], (255, 60, 60), 1) for s in found["spills"]]
    for s in found["ships"]:
        color = (255, 214, 0) if (s["x"], s["y"]) in suspects else (60, 220, 90)
        boxes.append((s["box"], color, 3))
    for (x0, y0, x1, y1), color, pad in boxes:
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(SIZE - 1, x1 + pad), min(SIZE - 1, y1 + pad)
        edge = [(x, y) for x in range(x0, x1 + 1) for y in (y0, y0 + 1, y1 - 1, y1)]
        edge += [(x, y) for y in range(y0, y1 + 1) for x in (x0, x0 + 1, x1 - 1, x1)]
        for x, y in edge:  # two pixels wide, so the boxes survive halving
            rgb[3 * (y * SIZE + x) : 3 * (y * SIZE + x) + 3] = bytes(color)
    half = bytearray()
    for y in range(0, SIZE, 2):
        row = rgb[3 * y * SIZE : 3 * (y + 1) * SIZE]
        for x in range(0, SIZE, 2):
            half += row[3 * x : 3 * x + 3]
    return bytes(half)


def _accuracy(totals: dict[str, int], noun: str) -> str:
    real = totals["found"] + totals["missed"]
    share = f" ({100 * totals['found'] / real:.0f}%)" if real else ""
    return f"{totals['found']} of {real} real {noun} found{share}"


def write_report(summary: dict[str, Any], folder: Path) -> Path:
    """Write ``detections.csv`` and ``report.html`` into ``folder``; return the report path."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "detections.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary["records"])

    ships, spills, alerts = summary["ships"], summary["spills"], summary["alerts"]
    high = [a for a in alerts if a["priority"] == "HIGH"]
    false_alarms = ships["false_alarms"] + spills["false_alarms"]
    cards = [
        ("Tiles scanned", summary["tiles"], f"{summary['area_km2']:,} km² of sea"),
        ("Ships", ships["found"] + ships["false_alarms"], _accuracy(ships, "ships")),
        ("Oil spills", spills["found"] + spills["false_alarms"], _accuracy(spills, "spills")),
        ("Alerts", len(alerts), f"{len(high)} high priority (ship next to a spill)"),
        ("False alarms", false_alarms, "detections with no real object"),
    ]
    rows = []
    tiles = Path(summary["folder"]) / "tiles"
    for a in sorted(alerts, key=lambda r: (r["priority"] != "HIGH", -r["spill_area_km2"])):
        pixels, _, _ = read_png(tiles / f"{a['tile']}.png")
        thumb = png_bytes(annotate(pixels, a["found"]), THUMB, THUMB, 3)
        image = base64.b64encode(thumb).decode()
        rows.append(
            f"<tr><td><img src='data:image/png;base64,{image}' alt='{a['tile']}'></td>"
            f"<td class='mono'>{a['tile']}</td>"
            f"<td><span class='tag {a['priority'].lower()}'>{a['priority']}</span></td>"
            f"<td>{a['spills']} ({a['spill_area_km2']:.2f} km²)</td>"
            f"<td>{a['ships']}</td><td>{a['suspect_ships']}</td></tr>"
        )
    card_html = "".join(
        f"<div class='card'><div class='label'>{html.escape(t)}</div><div class='value'>{v}</div>"
        f"<div class='sub'>{html.escape(s)}</div></div>"
        for t, v, s in cards
    )
    page = _PAGE.format(
        cards=card_html, rows="".join(rows) or "<tr><td colspan='6'>No alerts.</td></tr>"
    )
    path = folder / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ocean Watch report</title>
<style>
:root {{
  --bg:#f4f6f9; --panel:#fff; --text:#1b2330; --muted:#637083; --border:#dfe4eb;
  --link:#2f6fdb; --high:#9b1c17; --high-bg:#ffdedc; --alert:#8a4300; --alert-bg:#ffe6cc;
}}
@media (prefers-color-scheme: dark) {{ :root {{
  --bg:#0f141b; --panel:#161c25; --text:#e3e8ef; --muted:#8d99ab; --border:#2a3341;
  --link:#7aa7f5; --high:#ffaaa5; --high-bg:#45191a; --alert:#ffc58a; --alert-bg:#43290f;
}} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui, sans-serif; }}
main {{ max-width:1000px; margin:0 auto; padding:24px 16px; }}
a {{ color:var(--link); }}
h1 {{ margin:0 0 4px; font-size:22px; }}
p {{ color:var(--muted); margin:0 0 16px; }}
.cards {{
  display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr));
  gap:12px; margin-bottom:20px;
}}
.card {{
  background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:10px 14px;
}}
.label, .sub {{ font-size:12px; color:var(--muted); }}
.value {{ font-size:24px; font-weight:650; }}
.legend span {{ margin-right:14px; }}
.sw {{ display:inline-block; width:10px; height:10px; border:2px solid; margin-right:4px; }}
.scroll {{ overflow-x:auto; }}
table {{
  width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--border); border-radius:12px;
}}
th, td {{
  text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:middle;
}}
th {{ font-size:12px; color:var(--muted); }}
img {{ width:160px; height:160px; image-rendering:pixelated; border-radius:6px; display:block; }}
.mono {{ font-family:ui-monospace, Menlo, Consolas, monospace; }}
.tag {{ padding:1px 8px; border-radius:999px; font-size:12px; font-weight:600; }}
.tag.high {{ background:var(--high-bg); color:var(--high); }}
.tag.alert {{ background:var(--alert-bg); color:var(--alert); }}
</style></head>
<body><main>
<h1>Ocean Watch: oil spills and ships</h1>
<p>Satellite radar tiles analysed by MiniFlow. Accuracy is measured against the known
positions of every ship and spill in these synthetic tiles. All figures:
<a href="detections.csv">detections.csv</a>.</p>
<div class="cards">{cards}</div>
<p class="legend"><span><i class="sw" style="border-color:#ff3c3c"></i>oil spill</span>
<span><i class="sw" style="border-color:#3cdc5a"></i>ship</span>
<span><i class="sw" style="border-color:#ffd600"></i>ship within 500 m of a spill
(possible polluter)</span></p>
<div class="scroll"><table>
<thead><tr><th>Tile</th><th>Id</th><th>Priority</th><th>Spills</th><th>Ships</th>
<th>Suspect ships</th></tr></thead>
<tbody>{rows}</tbody></table></div>
</main></body></html>
"""
