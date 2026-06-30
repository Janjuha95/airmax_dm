"""Per-city coverage / blind-spot + latency analysis off the 3-hour window.

For each target city: has-data, pollutants present vs expected, total measurements, distinct
sensors, staleness (anchor - newest in-window reading in the city box), and publish latency
(median / p90 of sent_timestamp - date_utc). Produces coverage_summary.csv, a ranked log
table, and the belgium_coverage.html map.
"""

from __future__ import annotations

import csv
import logging
import statistics
from datetime import timedelta

from . import config, maps
from .aggregate import in_window_detailed, major_city_aggregates
from .db import connection
from .who import eu_2030_context, who_band

log = logging.getLogger(__name__)

_BAND_RANK = {"good": 0, "moderate": 1, "poor": 2}
_CLASS_RANK = {"red": 0, "amber": 1, "green": 2}


def _p90(seconds: list[float]) -> float | None:
    if not seconds:
        return None
    s = sorted(seconds)
    if len(s) == 1:
        return s[0]
    k = 0.9 * (len(s) - 1)
    lo = int(k)
    return s[lo] + (s[lo + 1] - s[lo]) * (k - lo) if lo + 1 < len(s) else s[lo]


def _worst_band(bands: list[str | None]) -> str | None:
    present = [b for b in bands if b]
    return max(present, key=lambda b: _BAND_RANK[b]) if present else None


def _classify(present: set[str], readings: dict) -> str:
    """green = full pollutant coverage and mostly high-confidence; amber = any data; red = none."""
    if not present:
        return "red"
    high = sum(1 for p in present if readings[p]["tier"] == "high")
    full = present >= set(config.POLLUTANT_ORDER)
    return "green" if (full and high / len(present) >= config.COVERAGE_GREEN_MIN_HIGH_FRACTION) else "amber"


def city_coverage(conn) -> dict:
    """Compute the coverage record for every target city."""
    data, _missing, (start, anchor) = major_city_aggregates(conn)
    detail, _s, _a = in_window_detailed(conn)

    box = {name: {"dates": [], "latencies": [], "sensors": set(), "total": 0}
           for name, _, _ in config.TARGET_CITIES}
    for _param, _value, lat, lon, loc, date_utc, sent_ts in detail:
        if lat is None or lon is None:
            continue
        for name, clat, clon in config.TARGET_CITIES:
            if abs(lat - clat) < config.CITY_BOX_DLAT and abs(lon - clon) < config.CITY_BOX_DLON:
                b = box[name]
                b["total"] += 1
                b["sensors"].add(loc)
                b["dates"].append(date_utc)
                if sent_ts is not None:
                    b["latencies"].append((sent_ts - date_utc).total_seconds())

    cities = []
    for name, lat, lon in config.TARGET_CITIES:
        polls = data[name]["pollutants"]
        present = set(polls)
        b = box[name]
        secs = b["latencies"]
        readings, bands = {}, []
        for p in config.POLLUTANT_ORDER:
            pd = polls.get(p)
            if not pd:
                continue
            band = who_band(p, pd["avg"])
            readings[p] = {**pd, "who_band": band, "eu2030": eu_2030_context(p, pd["avg"])}
            bands.append(band)
        cities.append({
            "city": name, "lat": lat, "lon": lon,
            "has_data": bool(present), "klass": _classify(present, polls),
            "present": [p for p in config.POLLUTANT_ORDER if p in present],
            "missing": [p for p in config.POLLUTANT_ORDER if p not in present],
            "total": b["total"], "sensors": len(b["sensors"]),
            "staleness": (anchor - max(b["dates"])) if b["dates"] else None,
            "median_latency": timedelta(seconds=statistics.median(secs)) if secs else None,
            "p90_latency": timedelta(seconds=_p90(secs)) if secs else None,
            "city_band": _worst_band(bands), "readings": readings,
        })
    return {"start": start, "anchor": anchor, "cities": cities}


def _write_csv(cov: dict) -> None:
    with open(config.COVERAGE_SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["city", "coverage_class", "has_data", "pollutants_present", "pollutants_missing",
                    "total_measurements", "distinct_sensors", "staleness_seconds",
                    "median_latency_seconds", "p90_latency_seconds", "city_who_band"])
        for c in cov["cities"]:
            secs = lambda td: int(td.total_seconds()) if td is not None else ""
            w.writerow([c["city"], c["klass"], c["has_data"], ";".join(c["present"]),
                        ";".join(c["missing"]), c["total"], c["sensors"], secs(c["staleness"]),
                        secs(c["median_latency"]), secs(c["p90_latency"]), c["city_band"] or ""])
    log.info("Wrote %s", config.COVERAGE_SUMMARY_CSV)


def _log_table(cov: dict) -> None:
    rows = sorted(cov["cities"], key=lambda c: (_CLASS_RANK[c["klass"]], c["total"]))
    log.info("Per-city coverage (ranked worst-first):")
    log.info("  %-10s %-6s %6s %7s %8s  %-9s %-11s",
             "city", "class", "polls", "meas", "sensors", "staleness", "med latency")
    for c in rows:
        log.info("  %-10s %-6s %4d/%d %7d %8d  %-9s %-11s",
                 c["city"], c["klass"], len(c["present"]), len(config.POLLUTANT_ORDER),
                 c["total"], c["sensors"], maps._fmt_td(c["staleness"]), maps._fmt_td(c["median_latency"]))
    blind = [c["city"] for c in cov["cities"] if not c["has_data"]]
    log.info("Blind spots (no in-window data): %s", ", ".join(blind) if blind else "none")
    # productionization: latency here is the simulator's SQS publish-lag (sent_timestamp - date_utc).
    # For the real feed, latency must be measured against the genuine OpenAQ API/stream ingest time.


def run() -> None:
    """Build coverage_summary.csv + belgium_coverage.html and log the ranked table."""
    with connection() as conn:
        cov = city_coverage(conn)
    _write_csv(cov)
    _log_table(cov)
    maps.build_city_coverage(cov)
