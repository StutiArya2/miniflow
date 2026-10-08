"""The ocean-watch example: synthetic radar tiles, ship and oil-spill detection, and the flow."""

from __future__ import annotations

import csv
import importlib
import math
import random
import sys
from pathlib import Path

import pytest

from miniflow import State, StateStore
from miniflow.cli import main

EXAMPLES = Path(__file__).parent.parent / "examples"
FLOW = EXAMPLES / "ocean_watch.py"


@pytest.fixture(scope="module")
def img():
    sys.path.insert(0, str(EXAMPLES))
    try:
        yield importlib.import_module("_ocean_imaging")
    finally:
        sys.path.remove(str(EXAMPLES))


def sea(img, level: int = 100, seed: int = 1) -> bytearray:
    """Calm sea with speckle, drawn independently of the module under test."""
    rng = random.Random(seed)
    return bytearray(
        max(0, min(255, round(level * (1 + 0.2 * rng.gauss(0, 1))))) for _ in range(img.SIZE**2)
    )


def draw_disc(px: bytearray, cx: int, cy: int, r: float, value: int) -> None:
    size = math.isqrt(len(px))
    for y in range(size):
        for x in range(size):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                px[y * size + x] = value


def draw_ellipse(px: bytearray, cx: int, cy: int, a: float, b: float, value: int) -> int:
    n, size = 0, math.isqrt(len(px))
    for y in range(size):
        for x in range(size):
            if ((x - cx) / a) ** 2 + ((y - cy) / b) ** 2 <= 1:
                px[y * size + x] = value
                n += 1
    return n


# ----------------------------------------------------------------- imaging
def test_png_round_trip(img, tmp_path):
    pixels = bytes(range(256)) * (img.SIZE**2 // 256)
    path = tmp_path / "tile.png"
    img.write_png(path, pixels, img.SIZE, img.SIZE)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert img.read_png(path) == (pixels, img.SIZE, img.SIZE)


def test_detects_a_ship_and_an_oil_spill(img):
    px = sea(img)
    draw_disc(px, 30, 30, 2.5, 250)
    spill_px = draw_ellipse(px, 80, 80, 20, 6, 30)
    found = img.detect(bytes(px))

    (ship,) = found["ships"]
    assert ship["x"] == pytest.approx(30, abs=1)
    assert ship["y"] == pytest.approx(30, abs=1)
    (spill,) = found["spills"]
    assert spill["x"] == pytest.approx(80, abs=2)
    assert spill["y"] == pytest.approx(80, abs=2)
    true_km2 = spill_px * (img.PIXEL_M / 1000) ** 2
    assert spill["area_km2"] == pytest.approx(true_km2, rel=0.25)
    assert spill["elongation"] > 2  # a 40 x 12 pixel slick


def test_calm_sea_has_no_detections(img):
    found = img.detect(bytes(sea(img, seed=7)))
    assert found["ships"] == []
    assert found["spills"] == []


def test_ship_next_to_a_spill_is_a_suspect(img):
    px = sea(img)
    draw_ellipse(px, 60, 60, 18, 5, 30)
    draw_disc(px, 85, 62, 2.5, 250)  # 7 px = 140 m from the end of the slick
    draw_disc(px, 10, 120, 2.5, 250)  # far away
    found = img.detect(bytes(px))
    assert len(found["ships"]) == 2
    assert [(round(s["x"]), round(s["y"])) for s in found["suspects"]] == [(85, 62)]


def test_generated_tiles_are_detected_accurately(img):
    totals = {"ships": [0, 0, 0], "spills": [0, 0, 0]}  # found, missed, false alarms
    for i in range(40):
        pixels, truth = img.make_tile(i)
        assert len(pixels) == img.SIZE**2
        score = img.score(img.detect(pixels), truth)
        for kind in totals:
            for j, key in enumerate(("found", "missed", "false_alarms")):
                totals[kind][j] += score[kind][key]
    for kind, (found, missed, false_alarms) in totals.items():
        assert found + missed > 5, kind  # the sample has something to find
        assert found / (found + missed) >= 0.9, (kind, totals)
        assert false_alarms <= 2, (kind, totals)


def test_tiles_are_the_same_every_time(img):
    assert img.make_tile(3) == img.make_tile(3)
    assert img.make_tile(3) != img.make_tile(4)


# -------------------------------------------------------------------- flow
@pytest.fixture
def small_mission(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIFLOW_OCEAN_DIR", str(tmp_path / "ocean"))
    monkeypatch.setenv("MINIFLOW_TILES", "24")
    monkeypatch.setenv("MINIFLOW_DOWNLOAD_DELAY", "0")
    return tmp_path


def test_flow_produces_a_report(small_mission, monkeypatch):
    monkeypatch.setenv("MINIFLOW_FAIL_RATE", "0")
    db = small_mission / "s.db"
    assert main(["run", str(FLOW), "--db", str(db), "--quiet"]) == 0

    out = small_mission / "ocean"
    assert len(list((out / "tiles").glob("tile_*.png"))) == 24
    with (out / "detections.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["tile"] for r in rows] == [f"tile_{i:03d}" for i in range(24)]
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "Ocean Watch" in report
    assert "data:image/png;base64," in report  # annotated thumbnails are embedded

    with StateStore(db) as store:
        run_id = store.latest_run("ocean_watch")
        states = store.get_states(run_id)
        results = store.load_completed(run_id)
    assert len(states) == 1 + 12 + 12 + 3
    assert set(states.values()) == {State.SUCCESS}
    summary = results["combine"]
    assert summary["tiles"] == 24
    assert (
        summary["ships"]["found"] / max(1, summary["ships"]["found"] + summary["ships"]["missed"])
        >= 0.9
    )
    assert results["alert_coast_guard"].startswith("sent ")


def test_resume_after_the_downlink_fails(small_mission, monkeypatch):
    db = small_mission / "s.db"
    monkeypatch.setenv("MINIFLOW_FAIL_RATE", "1")
    assert main(["run", str(FLOW), "--db", str(db), "--quiet", "--retry-base", "0.01"]) == 1
    with StateStore(db) as store:
        failed = store.get_states(store.latest_run("ocean_watch"))
    assert failed["prepare_tiles"] is State.SUCCESS
    assert failed["download_01"] is State.FAILED
    assert failed["detect_01"] is State.SKIPPED

    monkeypatch.setenv("MINIFLOW_FAIL_RATE", "0")
    assert main(["resume", str(FLOW), "--db", str(db), "--quiet"]) == 0
    with StateStore(db) as store:
        run_id = store.latest_run("ocean_watch")
        records = {r.task: r for r in store.changes_since(run_id, 0.0)}
    assert records["prepare_tiles"].from_cache
    assert not records["download_01"].from_cache
    assert all(r.state is State.SUCCESS for r in records.values())


def test_flow_validates(capsys):
    assert main(["validate", str(FLOW)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK: 28 tasks")
    assert out.index("prepare_tiles") < out.index("download_01") < out.index("detect_01")
