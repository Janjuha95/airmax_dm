"""WHO 2021 guideline cross-check + exceedance summary.

NOTE: WHO short-term guidelines are 8h (o3) / 24h (others); we compare our 3-hour average
to them as an INDICATIVE flag only, not formal compliance.
"""

from __future__ import annotations

import csv
import logging

from . import config
from .aggregate import major_city_aggregates
from .db import connection

log = logging.getLogger(__name__)


def who_status(parameter: str, avg: float):
    """Return (guideline, ratio, 'ABOVE'|'within') or (None, None, None) if no guideline."""
    g = config.WHO_GUIDELINES.get(parameter)
    if g is None:
        return None, None, None
    return g, avg / g, ("ABOVE" if avg > g else "within")


def exceedance_rows(data: dict) -> list[dict]:
    """Per city: which pollutants exceed WHO, ranked by count then PM2.5 ratio."""
    rows = []
    for name, _, _ in config.TARGET_CITIES:
        polls = data[name]["pollutants"]
        per, exceeded = {}, []
        for p in config.POLLUTANT_ORDER:
            d = polls.get(p)
            if not d:
                continue
            g, ratio, st = who_status(p, d["avg"])
            per[p] = (d["avg"], ratio, st)
            if st == "ABOVE":
                exceeded.append(p)
        rows.append({"city": name, "num": len(exceeded), "exceeded": exceeded, "per": per,
                     "pm25_ratio": per.get("pm25", (0, 0, ""))[1]})
    rows.sort(key=lambda r: (-r["num"], -r["pm25_ratio"]))
    return rows


def summarise() -> None:
    """Build the ranked exceedance summary, write the CSV, and log headline counts."""
    with connection() as conn:
        data, _missing, _window = major_city_aggregates(conn)
    rows = exceedance_rows(data)

    with open(config.WHO_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = ["rank", "city", "num_exceeded", "exceeded"]
        for p in config.POLLUTANT_ORDER:
            header += [f"{p}_avg", f"{p}_ratio", f"{p}_status"]
        w.writerow(header)
        for i, r in enumerate(rows, 1):
            line = [i, r["city"], r["num"], ";".join(r["exceeded"])]
            for p in config.POLLUTANT_ORDER:
                if p in r["per"]:
                    avg, ratio, st = r["per"][p]
                    line += [f"{avg:.2f}", f"{ratio:.2f}", st]
                else:
                    line += ["", "", ""]
            w.writerow(line)
    log.info("Wrote %s (indicative; simulated data)", config.WHO_CSV)

    log.info("rank  %-14s #exc  %s", "city", "  ".join(p.rjust(9) for p in config.POLLUTANT_ORDER))
    for i, r in enumerate(rows, 1):
        cells = []
        for p in config.POLLUTANT_ORDER:
            _, ratio, st = r["per"].get(p, (None, None, None))
            cells.append((f"{ratio:.2f}{'*' if st == 'ABOVE' else ' '}") if ratio is not None else "-")
        log.info("%2d  %-14s %3d   %s", i, r["city"], r["num"], "  ".join(c.rjust(9) for c in cells))

    n = len(config.TARGET_CITIES)
    log.info("Headline (cities exceeding WHO 2021 short-term guideline; * = above):")
    for p in config.POLLUTANT_ORDER:
        above = sum(1 for r in rows if r["per"].get(p, (None, None, None))[2] == "ABOVE")
        unit = next((data[c]["pollutants"][p]["unit"] for c, _, _ in config.TARGET_CITIES
                     if p in data[c]["pollutants"]), "")
        log.info("  %2d of %d exceed WHO %s (%g %s)", above, n, p, config.WHO_GUIDELINES[p], unit)
