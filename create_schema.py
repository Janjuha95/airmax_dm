"""create_schema.py — run schema.sql against the airquality database."""

import sys
from pathlib import Path

from db import connect

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    sql = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    conn = connect()
    with conn, conn.cursor() as cur:
        cur.execute(sql)
        # confirm the objects exist
        cur.execute("SELECT to_regclass('public.measurements'), to_regclass('public.current_air_quality_3h')")
        table, view = cur.fetchone()
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'measurements' ORDER BY indexname
        """)
        indexes = [r[0] for r in cur.fetchall()]
    conn.close()
    print("Schema applied.")
    print(f"  table : {table}")
    print(f"  view  : {view}")
    print(f"  indexes: {indexes}")


if __name__ == "__main__":
    main()
