"""Cross-check our OpenAQ (simulated) values against IRCEL-CELINE RIO (official network).

A METHOD demonstration + order-of-magnitude plausibility check — NOT a time-aligned
validation (our data is a historical simulated batch; IRCEL is live now).
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
from open_irceline import IrcelineApiError, IrcelineRioClient, RioFeature

from . import config
from .aggregate import major_city_aggregates
from .db import connection

log = logging.getLogger(__name__)

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Resolve config's feature-name strings to RioFeature enum members.
RIO_FEATURE = {p: getattr(RioFeature, name) for p, name in config.IRCEL_RIO_FEATURES.items()}


async def _latest_rio_hour(client, lat, lon):
    """Most recent hour with a published RIO PM2.5 value (grids lag by an hour or two)."""
    for back in range(config.IRCEL_LOOKBACK_HOURS):
        ts = (datetime.now(timezone.utc) - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            res = await client.get_data(features=[RioFeature.PM25_HMEAN], position=(lat, lon), timestamp=ts)
        except IrcelineApiError:
            continue
        if (res.get(RioFeature.PM25_HMEAN) or {}).get("value") is not None:
            return ts
    return None


async def _fetch_city(client, lat, lon, ts):
    """Fetch each feature separately so one unavailable RIO layer doesn't fail the others."""
    out = {}
    for p, feat in RIO_FEATURE.items():
        try:
            res = await client.get_data(features=[feat], position=(lat, lon), timestamp=ts)
            out[p] = (res.get(feat) or {}).get("value")
        except IrcelineApiError:
            out[p] = None
    return out


async def _collect():
    with connection() as conn:
        openaq, _missing, (start, anchor) = major_city_aggregates(conn, config.IRCEL_CITIES)
    out = []
    async with aiohttp.ClientSession() as session:
        client = IrcelineRioClient(session)
        ts = await _latest_rio_hour(client, config.IRCEL_CITIES[0][1], config.IRCEL_CITIES[0][2])
        log.info("IRCEL RIO timestamp used (UTC): %s", ts)
        for name, lat, lon in config.IRCEL_CITIES:
            ircel = await _fetch_city(client, lat, lon, ts) if ts else {}
            for p in RIO_FEATURE:
                oaq = openaq.get(name, {}).get("pollutants", {}).get(p)
                oaq_avg = round(oaq["avg"], 2) if oaq else None
                unit = oaq["unit"] if oaq else "µg/m³"
                irc = ircel.get(p)
                irc = round(irc, 2) if irc is not None else None
                diff = round(abs(oaq_avg - irc), 2) if (oaq_avg is not None and irc is not None) else None
                out.append((name, p, unit, oaq_avg, irc, diff))
    return out, start, anchor


def cross_check() -> None:
    rows, start, anchor = asyncio.run(_collect())
    with open(config.IRCEL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["city", "pollutant", "unit", "openaq_3h_avg", "ircel_current", "abs_diff"])
        w.writerows(rows)
    log.info("Wrote %s", config.IRCEL_CSV)
    fmt = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log.info("OpenAQ window (UTC): %s -> %s", fmt(start), fmt(anchor))
    log.info("%-10s %-5s %-6s %9s %9s %9s", "city", "poll", "unit", "OpenAQ", "IRCEL", "|diff|")
    f3 = lambda v: "-" if v is None else f"{v:.2f}"
    for name, p, unit, oaq, irc, diff in rows:
        log.info("%-10s %-5s %-6s %9s %9s %9s", name, p, unit, f3(oaq), f3(irc), f3(diff))
