"""Schema/view creation and shared query helpers (the 3-hour window lives here)."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import config
from .db import connection

log = logging.getLogger(__name__)


def create_schema() -> None:
    """Apply sql/schema.sql (table, indexes, view)."""
    sql = config.SQL_SCHEMA.read_text(encoding="utf-8")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("Schema applied from %s", config.SQL_SCHEMA)


def window(conn) -> tuple[datetime, datetime]:
    """Return (window_start, anchor) where anchor = max(date_utc)."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(date_utc) FROM measurements")
        anchor = cur.fetchone()[0]
    if anchor is None:
        raise RuntimeError("measurements table is empty — run `ingest` first")
    return anchor - timedelta(hours=config.WINDOW_HOURS), anchor


def view_rows(conn) -> list[tuple]:
    """All rows of current_air_quality_3h."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT city, parameter, unit, avg_value, measurement_count, location_count,
                   latitude, longitude, window_start, window_end
            FROM current_air_quality_3h
        """)
        return cur.fetchall()


def in_window_measurements(conn) -> tuple[list[tuple], datetime, datetime]:
    """Raw (parameter, unit, value, latitude, longitude, location_id) rows inside the 3h window."""
    start, anchor = window(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT parameter, unit, value, latitude, longitude, location_id
                       FROM measurements WHERE date_utc > %s AND date_utc <= %s""", (start, anchor))
        return cur.fetchall(), start, anchor


def in_window_detailed(conn) -> tuple[list[tuple], datetime, datetime]:
    """In-window rows with timing columns, for coverage + latency analysis.

    Returns (rows of (parameter, value, latitude, longitude, location_id, date_utc,
    sent_timestamp), window_start, anchor).
    """
    start, anchor = window(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT parameter, value, latitude, longitude, location_id, date_utc, sent_timestamp
                       FROM measurements WHERE date_utc > %s AND date_utc <= %s""", (start, anchor))
        return cur.fetchall(), start, anchor


def major_city_aggregates(conn, cities=None) -> tuple[dict, list, tuple[datetime, datetime]]:
    """Average in-window measurements from sensors within ~15 km of each city centroid.

    Returns ({city: {lat, lon, pollutants: {param: {avg, unit, count}}}}, missing_pairs, window).
    """
    cities = cities or config.TARGET_CITIES
    rows, start, anchor = in_window_measurements(conn)
    acc = {name: defaultdict(lambda: {"sum": 0.0, "n": 0, "unit": None, "locs": set()})
           for name, _, _ in cities}
    for param, unit, value, lat, lon, loc in rows:
        if lat is None or lon is None:
            continue
        for name, clat, clon in cities:
            if abs(lat - clat) < config.CITY_BOX_DLAT and abs(lon - clon) < config.CITY_BOX_DLON:
                a = acc[name][param]
                a["sum"] += float(value)
                a["n"] += 1
                a["unit"] = unit
                a["locs"].add(loc)
    data = {name: {"lat": lat, "lon": lon, "pollutants": {}} for name, lat, lon in cities}
    missing = []
    for name, _, _ in cities:
        for p in config.POLLUTANT_ORDER:
            a = acc[name].get(p)
            if a and a["n"] > 0:
                sensors = len(a["locs"])
                data[name]["pollutants"][p] = {
                    "avg": a["sum"] / a["n"], "unit": a["unit"], "count": a["n"],
                    "sensors": sensors, "tier": config.confidence_tier(a["n"], sensors),
                }
            else:
                missing.append((name, p))
    return data, missing, (start, anchor)


def report() -> None:
    """Create the schema and log a validation summary (counts, window, view size)."""
    create_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*), min(date_utc), max(date_utc),
                                  count(DISTINCT city), count(DISTINCT parameter)
                           FROM measurements""")
            n, dmin, dmax, ncity, nparam = cur.fetchone()
            cur.execute("SELECT count(*) FROM current_air_quality_3h")
            nview = cur.fetchone()[0]
    log.info("measurements: rows=%d  date_utc=%s..%s  cities=%d  parameters=%d",
             n, dmin, dmax, ncity, nparam)
    log.info("current_air_quality_3h: %d (city, parameter) rows", nview)
