"""Central configuration — the single place for region, queue, window, cities, guidelines,
palette, paths, and IRCEL settings. Nothing here is a secret; secrets come from .env / ~/.aws.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # project root
SQL_SCHEMA = BASE_DIR / "sql" / "schema.sql"

# --- AWS / SQS (override via env; never hardcode secrets) ---
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "openaq-sarthak")

# --- ingestion ---
RAW_BACKUP_PATH = BASE_DIR / "raw_measurements.jsonl"
RECEIVE_BATCH = 10
VISIBILITY_TIMEOUT = 60
WAIT_TIME_SECONDS = 1
EMPTY_POLLS_BEFORE_STOP = 3  # confirm the queue is really empty before stopping
PROGRESS_EVERY = 5000

# --- aggregation window (the view is fixed at this many hours) ---
WINDOW_HOURS = 3

# --- pollutants / colours ---
POLLUTANT_ORDER = ["pm25", "pm10", "o3", "no2", "so2", "co"]
DEFAULT_POLLUTANT = "pm25"
PALETTE = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"]  # YlGnBu, colour-blind-safe

# --- WHO 2021 short-term guideline values (indicative vs our 3h avg, not formal compliance) ---
WHO_GUIDELINES = {"pm25": 15, "pm10": 45, "no2": 25, "so2": 40, "o3": 100, "co": 4}

# --- 15 major cities (name, lat, lon) ---
TARGET_CITIES: list[tuple[str, float, float]] = [
    ("Brussels", 50.85, 4.35), ("Antwerp", 51.22, 4.40), ("Ghent", 51.05, 3.72),
    ("Charleroi", 50.41, 4.44), ("Liège", 50.63, 5.57), ("Bruges", 51.21, 3.22),
    ("Namur", 50.47, 4.87), ("Leuven", 50.88, 4.70), ("Mons", 50.45, 3.95),
    ("Mechelen", 51.03, 4.48), ("Kortrijk", 50.83, 3.27), ("Hasselt", 50.93, 5.34),
    ("Ostend", 51.23, 2.92), ("Aalst", 50.94, 4.04), ("Genk", 50.97, 5.50),
]
CITY_BOX_DLAT, CITY_BOX_DLON = 0.135, 0.215  # ~15 km half-box around a city centroid

# --- map render ---
MAP_CENTER = (50.6, 4.6)
MAP_ZOOM = 8
MAP_TILES = "CartoDB positron"
MAJOR_CITIES_HTML = BASE_DIR / "belgium_major_cities.html"
COVERAGE_HTML = BASE_DIR / "belgium_air_quality_map_v2.html"
PROVINCES_HTML = BASE_DIR / "belgium_provinces_choropleth.html"
# True-WGS84 provinces (real lon/lat). The repo's be-provinces-unk* file is a Highcharts
# map with PROJECTED geometry — unusable for lon/lat point-in-polygon.
PROVINCES_GEOJSON_URL = ("https://raw.githubusercontent.com/mathiasleroy/belgium-geographic-data/"
                         "master/dist/polygons/geojson/Belgium.provinces.WGS84.geojson")
PROVINCES_GEOJSON_CACHE = BASE_DIR / "belgium_provinces.geojson"

# --- WHO / IRCEL outputs ---
WHO_CSV = BASE_DIR / "who_exceedance_summary.csv"
IRCEL_CSV = BASE_DIR / "ircel_vs_openaq.csv"
IRCEL_CITIES = [c for c in TARGET_CITIES
                if c[0] in {"Brussels", "Antwerp", "Ghent", "Liège", "Charleroi", "Bruges"}]
# OpenAQ pollutant -> IRCEL RIO hourly-mean feature name (resolved to RioFeature in ircel.py).
# CO has no RIO grid; SO2's grid is often unavailable (handled gracefully).
IRCEL_RIO_FEATURES = {"pm25": "PM25_HMEAN", "pm10": "PM10_HMEAN", "no2": "NO2_HMEAN",
                      "o3": "O3_HMEAN", "so2": "SO2_HMEAN"}
IRCEL_LOOKBACK_HOURS = 8  # RIO grids publish with a lag; step back to find the latest


def resolve_aws_credentials() -> str:
    """If ~/.aws is absent, point boto3 at a project-local .aws/ (or aws/) folder.

    Sets only file *paths* as env vars; boto3 reads the files itself. Returns a label.
    """
    home = Path.home() / ".aws" / "credentials"
    if home.exists() and home.stat().st_size > 0:
        return "default (~/.aws)"
    for folder in (".aws", "aws"):
        creds = BASE_DIR / folder / "credentials"
        if creds.exists():
            os.environ.setdefault("AWS_SHARED_CREDENTIALS_FILE", str(creds))
            cfg = BASE_DIR / folder / "config"
            if cfg.exists():
                os.environ.setdefault("AWS_CONFIG_FILE", str(cfg))
            return f"project-local ({creds.parent})"
    return "default chain (env vars / instance role)"
