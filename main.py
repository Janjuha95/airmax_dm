"""Thin CLI for the airquality pipeline.

Usage:
    python main.py ingest      # drain the SQS queue into Postgres (JSONL backup + upsert)
    python main.py aggregate   # (re)create the schema/view and print a validation summary
    python main.py maps        # build the three Folium maps
    python main.py who         # WHO-guideline exceedance summary + CSV
    python main.py ircel       # cross-check vs IRCEL-CELINE (live network)
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from airquality import aggregate, ingest, ircel, maps, who  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Belgian air-quality pipeline (OpenAQ simulator).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="drain the SQS queue into Postgres")
    sub.add_parser("aggregate", help="create schema/view and print a validation summary")
    sub.add_parser("maps", help="build the three Folium maps")
    sub.add_parser("who", help="WHO guideline exceedance summary + CSV")
    sub.add_parser("ircel", help="cross-check against IRCEL-CELINE (live)")
    args = parser.parse_args()

    # UTF-8 console so µg/m³ and accented city names render correctly (esp. on Windows).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    for noisy in ("botocore", "boto3", "aiohttp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    {
        "ingest": ingest.drain,
        "aggregate": aggregate.report,
        "maps": maps.build_all,
        "who": who.summarise,
        "ircel": ircel.cross_check,
    }[args.command]()


if __name__ == "__main__":
    main()
