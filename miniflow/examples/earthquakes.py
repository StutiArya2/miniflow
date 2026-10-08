"""Live earthquake monitor

Seismology agencies publish every earthquake within minutes. This pipeline works like a disaster-alerting service: it downloads the latest real readings from the US Geological Survey, finds the most active regions, raises alerts for strong quakes and writes a short report. The data changes every time you run it.

Run it:  miniflow example earthquakes --ui
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from typing import Any

from miniflow import task
from miniflow.examples._common import fetch_json, send_simulated, write_report

# Steps whose results best show what the run found (the dashboard's results card).
HIGHLIGHTS = ["find_alerts", "rank_regions", "weekly_trend", "write_daily_report"]

WATCH = (
    "The three downloads start together on separate threads (they mostly wait on the "
    "network). Cleaning and analysis then run on separate CPU cores, and the alerts go "
    "out on the async event loop while the report is written."
)

FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{}.geojson"
ALERT_MAGNITUDE = 5.0

US_STATES = {
    "CA": "California", "AK": "Alaska", "NV": "Nevada", "HI": "Hawaii", "WA": "Washington",
    "OR": "Oregon", "UT": "Utah", "TX": "Texas", "OK": "Oklahoma", "MT": "Montana",
    "WY": "Wyoming", "ID": "Idaho", "NM": "New Mexico", "AZ": "Arizona", "CO": "Colorado",
}  # fmt: skip


def _when(ms: float) -> str:
    return time.strftime("%d %b %H:%M UTC", time.gmtime(ms / 1000))


def _region(place: str | None) -> str:
    if not place:
        return "Unknown"
    region = place.rsplit(", ", 1)[-1].strip()
    return US_STATES.get(region, region)


# ---------------------------------------------------------------- downloads (io)
@task(kind="io", retries=2, timeout=30)
def download_last_day() -> dict[str, Any]:
    """Download every earthquake recorded worldwide in the last 24 hours (USGS live feed)."""
    data = fetch_json(FEED.format("all_day"), snapshot="usgs_all_day")
    n = len(data["features"])
    return {
        "summary": f"{n} earthquakes recorded in the last 24 hours",
        "features": data["features"],
    }


@task(kind="io", retries=2, timeout=30)
def download_strong_week() -> dict[str, Any]:
    """Download the strong earthquakes (magnitude 4.5 or more) of the last 7 days."""
    data = fetch_json(FEED.format("4.5_week"), snapshot="usgs_4_5_week")
    n = len(data["features"])
    return {
        "summary": f"{n} strong earthquakes (M4.5+) in the last 7 days",
        "features": data["features"],
    }


@task(kind="io", retries=2, timeout=30)
def download_significant_month() -> dict[str, Any]:
    """Download the quakes USGS marked as 'significant' this month (felt widely or damaging)."""
    data = fetch_json(FEED.format("significant_month"), snapshot="usgs_significant_month")
    n = len(data["features"])
    return {"summary": f"{n} significant earthquakes this month", "features": data["features"]}


# ---------------------------------------------------------------- analysis (cpu)
@task(kind="cpu", after=[download_last_day])
def clean_readings(day: dict[str, Any]) -> dict[str, Any]:
    """Turn the raw feed into tidy records: magnitude, place, region, depth and time."""
    quakes = []
    for f in day["features"]:
        p = f["properties"]
        if p.get("mag") is None:
            continue
        quakes.append(
            {
                "mag": float(p["mag"]),
                "place": p.get("place") or "Unknown location",
                "region": _region(p.get("place")),
                "time": p["time"],
                "depth_km": round(f["geometry"]["coordinates"][2], 1),
                "tsunami": bool(p.get("tsunami")),
                "url": p.get("url", ""),
            }
        )
    quakes.sort(key=lambda q: q["mag"], reverse=True)
    dropped = len(day["features"]) - len(quakes)
    top = quakes[0] if quakes else None
    summary = f"Kept {len(quakes)} readings with a measured magnitude"
    if dropped:
        summary += f" (dropped {dropped} without one)"
    if top:
        summary += f".\nStrongest: M{top['mag']:.1f}, {top['place']}"
    return {"summary": summary, "quakes": quakes}


@task(kind="cpu", after=[clean_readings])
def rank_regions(clean: dict[str, Any]) -> dict[str, Any]:
    """Count quakes per region and find where the ground was most active."""
    by_region: dict[str, list[float]] = defaultdict(list)
    for q in clean["quakes"]:
        by_region[q["region"]].append(q["mag"])
    ranking = sorted(by_region.items(), key=lambda kv: (len(kv[1]), max(kv[1])), reverse=True)
    rows = [
        {"region": r, "count": len(m), "max": max(m), "avg": sum(m) / len(m)} for r, m in ranking
    ]
    lines = [f"{r['region']}: {r['count']} quakes, strongest M{r['max']:.1f}" for r in rows[:5]]
    return {"summary": "Most active regions today:\n" + "\n".join(lines), "regions": rows}


@task(kind="cpu", after=[download_strong_week])
def weekly_trend(week: dict[str, Any]) -> dict[str, Any]:
    """Count strong quakes per day this week, to see if activity is rising or falling."""
    per_day: Counter[str] = Counter()
    strongest = None
    for f in week["features"]:
        p = f["properties"]
        if p.get("mag") is None:
            continue
        per_day[time.strftime("%a %d %b", time.gmtime(p["time"] / 1000))] += 1
        if strongest is None or p["mag"] > strongest["mag"]:
            strongest = p
    days = sorted(per_day.items(), key=lambda kv: time.strptime(kv[0], "%a %d %b"))
    busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else ("-", 0)
    summary = (
        f"{sum(per_day.values())} strong quakes this week; busiest day {busiest[0]} ({busiest[1]})"
    )
    if strongest:
        summary += f".\nStrongest of the week: M{strongest['mag']:.1f}, {strongest['place']}"
    return {"summary": summary, "per_day": days}


@task(kind="cpu", after=[clean_readings, download_significant_month])
def find_alerts(clean: dict[str, Any], significant: dict[str, Any]) -> dict[str, Any]:
    """Pick out the quakes people should be warned about: magnitude 5+ today, or any tsunami flag."""
    alerts = [q for q in clean["quakes"] if q["mag"] >= ALERT_MAGNITUDE or q["tsunami"]]
    seen = {q["url"] for q in alerts}
    for f in significant["features"]:
        p = f["properties"]
        recent = p["time"] >= time.time() * 1000 - 24 * 3600 * 1000
        if recent and p.get("url") not in seen and p.get("mag") is not None:
            alerts.append({"mag": float(p["mag"]), "place": p.get("place") or "?", "time": p["time"],
                           "tsunami": bool(p.get("tsunami")), "url": p.get("url", "")})  # fmt: skip
    alerts.sort(key=lambda q: q["mag"], reverse=True)
    if not alerts:
        return {
            "summary": f"No quake reached M{ALERT_MAGNITUDE:.0f} today: no alerts needed",
            "alerts": [],
        }
    lines = [
        f"M{q['mag']:.1f} {q['place']} ({_when(q['time'])})"
        + (" - TSUNAMI FLAG" if q["tsunami"] else "")
        for q in alerts[:6]
    ]
    return {"summary": f"{len(alerts)} alert(s):\n" + "\n".join(lines), "alerts": alerts}


# ------------------------------------------------------------------- outputs
@task(kind="async", after=[find_alerts])
async def send_alerts(found: dict[str, Any]) -> dict[str, Any]:
    """Notify subscribers about each alert (simulated: messages go to the reports outbox)."""
    messages = []
    for q in found["alerts"]:
        await asyncio.sleep(0.05)  # stands in for a call to an SMS / push service
        messages.append(
            f"EARTHQUAKE ALERT M{q['mag']:.1f}\n{q['place']}\n{_when(q['time'])}\n{q['url']}"
        )
    if not messages:
        return {"summary": "Nothing to send today"}
    path = send_simulated("earthquake-alerts", messages)
    return {"summary": f"Sent {len(messages)} alert message(s) (simulated, saved to {path})"}


@task(kind="io", after=[rank_regions, weekly_trend, find_alerts])
def write_daily_report(
    regions: dict[str, Any], trend: dict[str, Any], found: dict[str, Any]
) -> dict[str, Any]:
    """Write the day's earthquake report as a Markdown file."""
    lines = ["# Earthquake report", "", f"Generated {time.strftime('%d %b %Y %H:%M')}", ""]
    lines += [
        "## Most active regions (last 24 h)",
        "",
        "| Region | Quakes | Strongest |",
        "|---|---|---|",
    ]
    lines += [
        f"| {r['region']} | {r['count']} | M{r['max']:.1f} |" for r in regions["regions"][:10]
    ]
    lines += ["", "## This week", "", trend["summary"], "", "## Alerts", ""]
    lines += [f"- M{q['mag']:.1f} {q['place']}" for q in found["alerts"]] or ["- none"]
    path = write_report("earthquakes", "\n".join(lines) + "\n")
    top = regions["regions"][0] if regions["regions"] else None
    headline = f"Busiest region: {top['region']} ({top['count']} quakes). " if top else ""
    return {"summary": f"{headline}{len(found['alerts'])} alert(s). Report saved to {path}"}
