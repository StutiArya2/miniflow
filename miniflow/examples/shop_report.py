"""Online shop nightly sales report

Every night, online shops add up the day's sales. This shop sells to seven countries in seven currencies, so first it needs the real exchange rates (European Central Bank, via Frankfurter) for every day of the last month. Then it converts all 30 days of orders to rupees, works out revenue by country and by product, flags unusual days, writes the report and emails it to the founders. The orders are sample data; the exchange rates are live.

Run it:  miniflow example shop_report --ui
"""

from __future__ import annotations

import asyncio
import csv
import random
import statistics
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from miniflow import task
from miniflow.examples._common import fetch_json, inr, reports_dir, send_simulated, write_report

# Steps whose results best show what the run found (the dashboard's results card).
HIGHLIGHTS = ["revenue_by_country", "revenue_by_product", "spot_unusual_days", "write_sales_report"]

WATCH = (
    "Reading the order export and downloading both sets of exchange rates happen at the same "
    "time. Currency conversion waits for both, then three different analyses run side by side "
    "on separate CPU cores before the report is written and emailed."
)

DAYS = 30
RATES = "https://api.frankfurter.dev/v1/{path}?base=INR&symbols={symbols}"
MARKETS = {  # country: (currency, share of orders)
    "India": ("INR", 0.45), "United States": ("USD", 0.15), "United Kingdom": ("GBP", 0.10),
    "Germany": ("EUR", 0.08), "Singapore": ("SGD", 0.08), "Australia": ("AUD", 0.08),
    "Japan": ("JPY", 0.06),
}  # fmt: skip
PRODUCTS = {  # product: price in INR (local prices are set from these)
    "Wireless earbuds": 2499, "Yoga mat": 1199, "Coffee beans 1 kg": 899,
    "Desk lamp": 1799, "Steel water bottle": 649, "Phone case": 499,
}  # fmt: skip
REFERENCE = {
    "INR": 1,
    "USD": 0.0118,
    "GBP": 0.0089,
    "EUR": 0.0102,
    "SGD": 0.0153,
    "AUD": 0.0179,
    "JPY": 1.78,
}


def _period() -> tuple[date, date]:
    end = date.today() - timedelta(days=1)
    return end - timedelta(days=DAYS - 1), end


@task(kind="io")
def read_order_export() -> dict[str, Any]:
    """Read the last 30 days of orders from the shop's order export (sample data)."""
    start, end = _period()
    path = reports_dir() / f"sample-orders-{end:%Y%m%d}.csv"
    if not path.exists():  # write the sample export once per day, then read it like a real file
        rng = random.Random(end.toordinal())
        flash_sale = start + timedelta(days=rng.randrange(5, DAYS - 3))
        with path.open("w", newline="", encoding="utf-8") as f:
            out = csv.writer(f)
            out.writerow(
                ["order_id", "date", "country", "currency", "product", "quantity", "amount"]
            )
            oid = 10_000
            for d in range(DAYS):
                day = start + timedelta(days=d)
                n = (
                    rng.randint(55, 85)
                    * (1.3 if day.weekday() >= 5 else 1)
                    * (3 if day == flash_sale else 1)
                )
                for _ in range(int(n)):
                    country = rng.choices(list(MARKETS), [s for _, s in MARKETS.values()])[0]
                    cur = MARKETS[country][0]
                    product = rng.choice(list(PRODUCTS))
                    qty = rng.choices([1, 2, 3], [0.8, 0.15, 0.05])[0]
                    price = round(
                        PRODUCTS[product] * REFERENCE[cur] * (0.7 if day == flash_sale else 1), 2
                    )
                    oid += 1
                    out.writerow(
                        [oid, day.isoformat(), country, cur, product, qty, round(price * qty, 2)]
                    )
    with path.open(encoding="utf-8") as f:
        orders = list(csv.DictReader(f))
    currencies = len({o["currency"] for o in orders})
    return {
        "summary": f"{len(orders):,} orders from {start:%d %b} to {end:%d %b} in {currencies} currencies",
        "orders": orders,
    }


def _symbols() -> str:
    return ",".join(sorted({c for c, _ in MARKETS.values()} - {"INR"}))


@task(kind="io", retries=3, timeout=20)
def download_rate_history() -> dict[str, Any]:
    """Download the official exchange rate for every day of the period (European Central Bank)."""
    start, end = _period()
    data = fetch_json(
        RATES.format(path=f"{start}..{end}", symbols=_symbols()), snapshot="rates_history"
    )
    days = sorted(data["rates"])
    return {
        "summary": f"Exchange rates for {len(days)} banking days ({days[0]} to {days[-1]})",
        "rates": data["rates"],
    }


@task(kind="io", retries=3, timeout=20)
def download_todays_rates() -> dict[str, Any]:
    """Download today's exchange rates, to show what foreign revenue is worth now."""
    data = fetch_json(RATES.format(path="latest", symbols=_symbols()), snapshot="rates_latest")
    shown = ", ".join(
        f"1 {c} = {inr(1 / r)}"
        for c, r in sorted(data["rates"].items())
        if c in {"USD", "GBP", "EUR"}
    )
    return {
        "summary": f"Rates on {data['date']}: {shown}",
        "date": data["date"],
        "rates": data["rates"],
    }


@task(kind="cpu", after=[read_order_export, download_rate_history])
def convert_to_rupees(export: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    """Convert every order to rupees using the exchange rate of the day it was placed."""
    days = sorted(history["rates"])
    converted, foreign = [], 0
    for o in export["orders"]:
        # Weekends and holidays have no rate: use the last banking day before the order.
        rate_day = max((d for d in days if d <= o["date"]), default=days[0])
        rate = 1.0 if o["currency"] == "INR" else history["rates"][rate_day][o["currency"]]
        foreign += o["currency"] != "INR"
        converted.append({**o, "quantity": int(o["quantity"]), "inr": float(o["amount"]) / rate})
    total = sum(o["inr"] for o in converted)
    return {
        "summary": f"Total revenue {inr(total)} ({foreign:,} foreign-currency orders converted)",
        "orders": converted,
        "total": total,
    }


@task(kind="cpu", after=[convert_to_rupees])
def revenue_by_country(sales: dict[str, Any]) -> dict[str, Any]:
    """Add up revenue per country."""
    totals: dict[str, float] = defaultdict(float)
    for o in sales["orders"]:
        totals[o["country"]] += o["inr"]
    rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"{c}: {inr(v)} ({v / sales['total']:.0%})" for c, v in rows]
    return {"summary": "Revenue by country:\n" + "\n".join(lines), "rows": rows}


@task(kind="cpu", after=[convert_to_rupees])
def revenue_by_product(sales: dict[str, Any]) -> dict[str, Any]:
    """Add up revenue and units sold per product."""
    money: dict[str, float] = defaultdict(float)
    units: dict[str, int] = defaultdict(int)
    for o in sales["orders"]:
        money[o["product"]] += o["inr"]
        units[o["product"]] += o["quantity"]
    rows = sorted(money.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"{p}: {inr(v)} ({units[p]:,} sold)" for p, v in rows]
    return {
        "summary": "Best sellers:\n" + "\n".join(lines),
        "rows": [(p, v, units[p]) for p, v in rows],
    }


@task(kind="cpu", after=[convert_to_rupees])
def spot_unusual_days(sales: dict[str, Any]) -> dict[str, Any]:
    """Compare each day's revenue with a normal day and flag anything far out of line."""
    daily: dict[str, float] = defaultdict(float)
    for o in sales["orders"]:
        daily[o["date"]] += o["inr"]
    flagged = []
    for day, value in sorted(daily.items()):
        others = [v for d, v in daily.items() if d != day]
        mean, spread = statistics.mean(others), statistics.pstdev(others) or 1.0
        if abs(value - mean) / spread > 2.5:
            flagged.append((day, value, value / mean))
    if not flagged:
        return {"summary": "No unusual days: revenue stayed in its normal range", "flagged": []}
    lines = [
        f"{date.fromisoformat(d):%a %d %b}: {inr(v)}, {x:.1f}× a normal day" for d, v, x in flagged
    ]
    return {
        "summary": "Unusual days (check for a sale, outage or fraud):\n" + "\n".join(lines),
        "flagged": flagged,
    }


@task(
    kind="io",
    after=[revenue_by_country, revenue_by_product, spot_unusual_days, download_todays_rates],
)
def write_sales_report(
    countries: dict[str, Any],
    products: dict[str, Any],
    unusual: dict[str, Any],
    today: dict[str, Any],
) -> dict[str, Any]:
    """Write the month's sales report as a Markdown file."""
    start, end = _period()
    total = sum(v for _, v in countries["rows"])
    lines = [
        f"# Sales report {start:%d %b} - {end:%d %b %Y}",
        "",
        f"**Total revenue: {inr(total)}**",
        "",
    ]
    lines += ["## By country", "", "| Country | Revenue | Share |", "|---|---|---|"]
    lines += [f"| {c} | {inr(v)} | {v / total:.0%} |" for c, v in countries["rows"]]
    lines += ["", "## By product", "", "| Product | Revenue | Units |", "|---|---|---|"]
    lines += [f"| {p} | {inr(v)} | {u:,} |" for p, v, u in products["rows"]]
    lines += [
        "",
        "## Unusual days",
        "",
        unusual["summary"],
        "",
        f"Exchange rates: {today['summary']}",
    ]
    path = write_report("sales", "\n".join(lines) + "\n")
    top_country, top_product = countries["rows"][0][0], products["rows"][0][0]
    return {
        "summary": f"Revenue {inr(total)}. Top market {top_country}, best seller {top_product}. Report saved to {path}",
        "path": str(path),
        "total": total,
    }


@task(kind="async", after=[write_sales_report])
async def email_founders(report: dict[str, Any]) -> dict[str, Any]:
    """Email the report to the founders (simulated: saved to the reports outbox)."""
    await asyncio.sleep(0.2)  # stands in for an email service call
    path = send_simulated(
        "sales-email",
        [
            f"To: founders@example-shop.in\nSubject: Monthly sales {inr(report['total'])}\n\nReport attached: {report['path']}"
        ],
    )
    return {"summary": f"Emailed at {time.strftime('%H:%M')} (simulated, saved to {path})"}
