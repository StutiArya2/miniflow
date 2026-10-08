"""City air-quality and outdoor-exercise advisor

Health apps tell you whether it is safe to go for a run. This pipeline does the same for eight Indian cities: for each city it downloads the live air-pollution and weather forecast (Open-Meteo), works out the air-quality level and the best time to be outside in the next 24 hours, ranks the cities, sends health alerts where the air is unhealthy, and writes a report.

Run it:  miniflow example air_quality --ui
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from miniflow import task
from miniflow.examples._common import fetch_json, send_simulated, write_report

# Steps whose results best show what the run found (the dashboard's results card).
HIGHLIGHTS = ["rank_cities", "send_health_alerts", "assess_vellore", "write_city_report"]

WATCH = (
    "Sixteen downloads (air and weather for eight cities) run at the same time on threads, "
    "so the whole fetch takes about as long as the slowest one. Each city's assessment then "
    "runs on a CPU core as soon as both of its downloads are in; it does not wait for the other cities."
)

CITIES = {
    "delhi": ("Delhi", 28.61, 77.21),
    "mumbai": ("Mumbai", 19.08, 72.88),
    "bengaluru": ("Bengaluru", 12.97, 77.59),
    "chennai": ("Chennai", 13.08, 80.27),
    "kolkata": ("Kolkata", 22.57, 88.36),
    "hyderabad": ("Hyderabad", 17.39, 78.49),
    "pune": ("Pune", 18.52, 73.86),
    "vellore": ("Vellore", 12.92, 79.13),
}
AIR = (
    "https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
    "&hourly=us_aqi,pm2_5,pm10&timezone=Asia%2FKolkata&forecast_days=2"
)
WEATHER = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m,precipitation_probability&timezone=Asia%2FKolkata&forecast_days=2"
)

# US EPA air-quality index bands (the scale Open-Meteo reports).
BANDS = [
    (50, "Good", "Great day to be outside."),
    (100, "Moderate", "Fine for most people; very sensitive people may want shorter workouts."),
    (150, "Unhealthy for sensitive groups", "Children, older people and asthmatics should limit long outdoor exercise."),
    (200, "Unhealthy", "Avoid long outdoor exercise; consider a mask outdoors."),
    (300, "Very unhealthy", "Stay indoors where possible."),
    (10_000, "Hazardous", "Stay indoors; health warning for everyone."),
]  # fmt: skip


def band(aqi: float) -> tuple[str, str]:
    for limit, name, advice in BANDS:
        if aqi <= limit:
            return name, advice
    return BANDS[-1][1], BANDS[-1][2]


# ------------------------------------------------------------- downloads (io)
def fetch_air(*, slug: str) -> dict[str, Any]:
    name, lat, lon = CITIES[slug]
    data = fetch_json(AIR.format(lat=lat, lon=lon), snapshot=f"air_{slug}")
    values = [v for v in data["hourly"]["us_aqi"] if v is not None]
    peak = max(values) if values else None
    text = f"{name}: air-quality forecast for {len(data['hourly']['time'])} hours"
    if peak is not None:
        text += f", peak AQI {peak:.0f}"
    return {"summary": text, "data": data}


def fetch_weather(*, slug: str) -> dict[str, Any]:
    name, lat, lon = CITIES[slug]
    data = fetch_json(WEATHER.format(lat=lat, lon=lon), snapshot=f"weather_{slug}")
    temps = [t for t in data["hourly"]["temperature_2m"] if t is not None]
    text = f"{name}: weather forecast, {min(temps):.0f}-{max(temps):.0f} °C" if temps else name
    return {"summary": text, "data": data}


# --------------------------------------------------------------- analysis (cpu)
def assess_city(air: dict[str, Any], weather: dict[str, Any], *, slug: str) -> dict[str, Any]:
    """Air-quality level and the best 2-hour window for outdoor exercise in the next 24 hours."""
    name = CITIES[slug][0]
    a, w = air["data"], weather["data"]
    offset = a.get("utc_offset_seconds", 19800)
    now = time.strftime("%Y-%m-%dT%H:00", time.gmtime(time.time() + offset))
    times = a["hourly"]["time"]
    start = next(
        (i for i, t in enumerate(times) if t >= now), 0
    )  # stale snapshot -> from the start
    hours = []
    for i in range(start, min(start + 24, len(times))):
        aqi = a["hourly"]["us_aqi"][i]
        temp = w["hourly"]["temperature_2m"][i] if i < len(w["hourly"]["temperature_2m"]) else None
        rain = (
            w["hourly"]["precipitation_probability"][i]
            if i < len(w["hourly"]["precipitation_probability"])
            else None
        )
        if aqi is not None and temp is not None:
            hours.append(
                {
                    "time": times[i],
                    "aqi": aqi,
                    "pm25": a["hourly"]["pm2_5"][i] or 0,
                    "temp": temp,
                    "rain": rain or 0,
                }
            )
    if not hours:
        return {
            "summary": f"{name}: no forecast data",
            "city": name,
            "avg_aqi": 0.0,
            "max_aqi": 0.0,
        }

    avg_aqi = sum(h["aqi"] for h in hours) / len(hours)
    max_aqi = max(h["aqi"] for h in hours)
    category, advice = band(max_aqi)

    def discomfort(h: dict[str, Any]) -> float:
        hour = int(h["time"][11:13])
        if hour < 5 or hour > 20:  # nobody plans a run at 2 am
            return float("inf")
        heat = max(0.0, float(h["temp"]) - 32) * 10
        return float(h["aqi"]) + heat + (40 if h["rain"] > 50 else 0)

    best = min(
        range(len(hours) - 1),
        key=lambda i: discomfort(hours[i]) + discomfort(hours[i + 1]),
        default=0,
    )
    window = f"{hours[best]['time'][11:16]}-{hours[min(best + 2, len(hours) - 1)]['time'][11:16]}"
    summary = (
        f"{name}: AQI up to {max_aqi:.0f} ({category}), average {avg_aqi:.0f}\n"
        f"Best time outside: {window} (AQI {hours[best]['aqi']:.0f}, {hours[best]['temp']:.0f} °C)\n"
        f"{advice}"
    )
    return {
        "summary": summary, "city": name, "avg_aqi": avg_aqi, "max_aqi": max_aqi,
        "category": category, "advice": advice, "best_window": window,
        "pm25": sum(h["pm25"] for h in hours) / len(hours),
    }  # fmt: skip


# One branch per city: two downloads (threads) feeding one assessment (a CPU worker).
# The tasks are module attributes so worker processes can find them by name.
for _slug, (_name, _lat, _lon) in CITIES.items():
    globals()[f"air_{_slug}"] = task(
        fetch_air, name=f"air_{_slug}", kind="io", retries=2, timeout=20, kwargs={"slug": _slug},
        description=f"Download the next 48 hours of air-pollution forecasts for {_name} (Open-Meteo).",
    )  # fmt: skip
    globals()[f"weather_{_slug}"] = task(
        fetch_weather, name=f"weather_{_slug}", kind="io", retries=2, timeout=20, kwargs={"slug": _slug},
        description=f"Download the next 48 hours of temperature and rain forecasts for {_name}.",
    )  # fmt: skip
    globals()[f"assess_{_slug}"] = task(
        assess_city, name=f"assess_{_slug}", kind="cpu", kwargs={"slug": _slug},
        after=[f"air_{_slug}", f"weather_{_slug}"],
        description=f"Work out {_name}'s air-quality level and the best time to exercise outside.",
    )  # fmt: skip


@task(kind="cpu", after=[f"assess_{s}" for s in CITIES])
def rank_cities(*assessments: dict[str, Any]) -> dict[str, Any]:
    """Rank the cities from cleanest to most polluted air over the next 24 hours."""
    ranked = sorted(assessments, key=lambda a: a["avg_aqi"])
    lines = [
        f"{i}. {a['city']}: average AQI {a['avg_aqi']:.0f} ({band(a['avg_aqi'])[0]})"
        for i, a in enumerate(ranked, 1)
    ]
    return {"summary": "Cleanest to most polluted:\n" + "\n".join(lines), "ranked": ranked}


@task(kind="async", after=[rank_cities])
async def send_health_alerts(ranking: dict[str, Any]) -> dict[str, Any]:
    """Warn residents of cities where the air will be unhealthy (simulated push notifications)."""
    bad = [a for a in ranking["ranked"] if a["max_aqi"] > 150]
    messages = []
    for a in bad:
        await asyncio.sleep(0.05)  # stands in for a push-notification service
        messages.append(
            f"AIR QUALITY ALERT - {a['city']}\nAQI up to {a['max_aqi']:.0f} ({a['category']}).\n{a['advice']}"
        )
    if not messages:
        return {"summary": "Air is not unhealthy anywhere today: no alerts sent"}
    path = send_simulated("air-quality-alerts", messages)
    cities = ", ".join(a["city"] for a in bad)
    return {"summary": f"Sent alerts for {cities} (simulated, saved to {path})"}


@task(kind="io", after=[rank_cities])
def write_city_report(ranking: dict[str, Any]) -> dict[str, Any]:
    """Write today's air-quality report for all cities as a Markdown file."""
    lines = [
        "# Air quality and outdoor advice",
        "",
        f"Generated {time.strftime('%d %b %Y %H:%M')}",
        "",
    ]
    lines += ["| City | Avg AQI | Peak AQI | Level | Best time outside |", "|---|---|---|---|---|"]
    lines += [
        f"| {a['city']} | {a['avg_aqi']:.0f} | {a['max_aqi']:.0f} | {a['category']} | {a['best_window']} |"
        for a in ranking["ranked"] if "best_window" in a
    ]  # fmt: skip
    path = write_report("air-quality", "\n".join(lines) + "\n")
    best, worst = ranking["ranked"][0], ranking["ranked"][-1]
    return {
        "summary": f"Cleanest air: {best['city']} (AQI {best['avg_aqi']:.0f}). "
        f"Most polluted: {worst['city']} (AQI {worst['avg_aqi']:.0f}). Report saved to {path}"
    }
