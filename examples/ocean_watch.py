"""Ocean Watch: find oil spills, and the ships that may have caused them, in satellite radar images.

A radar satellite has imaged 240 patches of sea ("tiles", 5.12 x 5.12 km each).
This flow downloads them from the ground station, scans every tile for oil
spills and ships, and writes a report of the tiles the coast guard should check.

    miniflow run examples/ocean_watch.py     # then open examples/ocean_watch_data/report.html
    miniflow ui --flows examples             # or run, cancel and resume it from the dashboard

Shape (28 tasks):

    prepare_tiles -> download_01..12 (io) -> detect_01..12 (cpu) -> combine -> write_report
                                                                            -> alert_coast_guard

- Downloads mostly wait on the network, so they share a pool of threads.
- Detection is pure-Python pixel work, so each batch runs in its own process, on its own core.
- A failed download is retried; if it still fails, its detect step and everything after it
  are skipped, and ``resume`` later reruns only those.

Environment:
    MINIFLOW_FAIL_RATE       chance that a download attempt times out (default 0.2)
    MINIFLOW_TILES           number of tiles (default 240)
    MINIFLOW_OCEAN_DIR       where tiles and the report go (default examples/ocean_watch_data)
    MINIFLOW_DOWNLOAD_DELAY  seconds per download, standing in for the satellite link (default 1.5)
    MINIFLOW_SEED            seed for the injected faults (default: random)

The tiles are synthetic (``_ocean_imaging.make_tile``), so the report can say how many of
the real ships and spills were found.
"""

import asyncio
import os
import random
import time
from pathlib import Path

import _ocean_imaging as img

from miniflow import task

TILES = int(os.environ.get("MINIFLOW_TILES", "240"))
BATCHES = 12
OUT = Path(os.environ.get("MINIFLOW_OCEAN_DIR") or Path(__file__).parent / "ocean_watch_data")
FAIL_RATE = float(os.environ.get("MINIFLOW_FAIL_RATE", "0.2"))
DELAY = float(os.environ.get("MINIFLOW_DOWNLOAD_DELAY", "1.5"))
_rng = random.Random(os.environ.get("MINIFLOW_SEED"))


@task(kind="io")
def prepare_tiles():
    """Create the satellite tiles the first time; later runs reuse them."""
    return img.prepare(OUT / "tiles", TILES)


def download(folder, batch):
    """Fetch one batch of tiles from the ground station (the link is simulated)."""
    time.sleep(DELAY * (0.7 + 0.6 * _rng.random()))
    if _rng.random() < FAIL_RATE:
        raise TimeoutError(f"batch {batch}: ground station link timed out")
    return [img.load_tile(folder, i) for i in img.batch(TILES, BATCHES, batch)]


def detect(tiles):
    """Find ships and oil spills in every tile of one batch (heavy pixel work)."""
    return [img.analyse(tile) for tile in tiles]


# One download and one detect task per batch: download_01 -> detect_01, and so on.
for b in range(1, BATCHES + 1):
    globals()[f"download_{b:02d}"] = task(
        download, name=f"download_{b:02d}", kind="io", retries=3, after=[prepare_tiles], args=(b,)
    )
    globals()[f"detect_{b:02d}"] = task(
        detect, name=f"detect_{b:02d}", kind="cpu", after=[f"download_{b:02d}"]
    )


@task(kind="io", after=[f"detect_{b:02d}" for b in range(1, BATCHES + 1)])
def combine(*batches):
    """Merge the findings of all batches and score them against the known answers."""
    return img.summarise([tile for batch in batches for tile in batch], OUT)


@task(kind="io", after=[combine])
def write_report(summary):
    """Write report.html (alerts with marked-up images) and detections.csv."""
    return str(img.write_report(summary, OUT))


@task(kind="async", after=[combine])
async def alert_coast_guard(summary):
    """Send the alerts on (simulated: a real flow would call the coast guard's API)."""
    await asyncio.sleep(0.3)
    high = sum(1 for a in summary["alerts"] if a["priority"] == "HIGH")
    return f"sent {len(summary['alerts'])} alerts ({high} high priority)"
