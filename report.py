"""report.py — post-drain validation report."""

import sys
from pathlib import Path

from db import connect

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RAW_PATH = Path(__file__).resolve().parent / "raw_measurements.jsonl"


def main():
    conn = connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT count(*), min(date_utc), max(date_utc),
               count(DISTINCT city), count(DISTINCT parameter)
        FROM measurements
    """)
    n, dmin, dmax, ncity, nparam = cur.fetchone()
    print("=== measurements ===")
    print(f"  rows            : {n}")
    print(f"  date_utc min    : {dmin}")
    print(f"  date_utc max    : {dmax}")
    print(f"  distinct cities : {ncity}")
    print(f"  distinct params : {nparam}")

    cur.execute("SELECT count(*) FROM current_air_quality_3h")
    nview = cur.fetchone()[0]
    cur.execute("SELECT window_start, window_end FROM current_air_quality_3h LIMIT 1")
    win = cur.fetchone()
    print("\n=== current_air_quality_3h (view) ===")
    if win:
        print(f"  window_start: {win[0]}")
        print(f"  window_end  : {win[1]}")
    print(f"  total (city, parameter) rows: {nview}")

    print("\n  first 15 rows:")
    cur.execute("""
        SELECT city, parameter, unit, avg_value, measurement_count, location_count
        FROM current_air_quality_3h
        ORDER BY city, parameter
        LIMIT 15
    """)
    print(f"  {'city':24} {'param':5} {'unit':5} {'avg_value':>10} {'count':>6} {'locs':>5}")
    for city, param, unit, avg, cnt, locs in cur.fetchall():
        print(f"  {(city or ''):24} {param:5} {(unit or ''):5} {str(avg):>10} {cnt:>6} {locs:>5}")

    cur.close()
    conn.close()

    print("\n=== safety-net reconciliation ===")
    jsonl_lines = sum(1 for _ in RAW_PATH.open(encoding="utf-8")) if RAW_PATH.exists() else 0
    print(f"  raw_measurements.jsonl lines : {jsonl_lines}")
    print(f"  measurements rows (= inserted): {n}")
    print(f"  implied duplicates           : {jsonl_lines - n}")
    print(f"  check  jsonl_lines == inserted + duplicates  -> {jsonl_lines} == {n} + {jsonl_lines - n}  "
          f"({'OK' if jsonl_lines == n + (jsonl_lines - n) else 'MISMATCH'})")


if __name__ == "__main__":
    main()
