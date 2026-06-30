# Belgian Air Quality — OpenAQ ingestion, aggregation & visualisation

A small data pipeline for "AirMax": ingest Belgian air-quality measurements from an AWS SQS
queue, compute each city's **current air quality** (per-pollutant average over the **last 3
hours** + measurement count), store it in Postgres, and visualise it on interactive maps —
plus a WHO-guideline exceedance summary and a cross-check against the official IRCEL-CELINE
network.

> The SQS feed is a **simulator** (an SNS topic publishing per-measurement OpenAQ-shaped JSON
> for country=BE). Values are spatially random and unrealistic; everything that follows treats
> that as a finding, not a bug.

---

## Architecture

```
OpenAQ SNS simulator (us-east-1)
        │  (per-measurement JSON, country=BE, SNS-wrapped)
        ▼
SQS  openaq-sarthak  (eu-west-1)
        │  boto3 long-poll consumer  ── raw_measurements.jsonl  (safety net, written before delete)
        ▼
Postgres  airquality.measurements   (idempotent upsert: ON CONFLICT (location_id, parameter, date_utc))
        │
        ▼
SQL view  current_air_quality_3h    (anchor = max(date_utc); window = (anchor-3h, anchor])
        │
        ├─► maps   → belgium_major_cities.html, belgium_air_quality_map_v2.html,
        │            belgium_provinces_choropleth.html  (Folium)
        ├─► who    → who_exceedance_summary.csv         (WHO 2021, indicative)
        └─► ircel  → ircel_vs_openaq.csv                (cross-check vs IRCEL-CELINE RIO)
```

- **Ingestion** is a single-threaded boto3 drain. It writes every record to a JSONL backup
  *before* deleting from SQS, and upserts with `ON CONFLICT DO NOTHING`, so it is safe to crash
  and rerun.
- **Aggregation** is plain SQL. "Now" is the latest measurement in the data (the batch is
  historical), so the 3-hour window is anchored on `max(date_utc)`.
- **Deploy target (documented, not deployed):** an **RDS PostgreSQL `db.t4g.micro`** — the data
  volume (≈56k rows) is tiny, so anything larger is wasted spend. See [COST](#cost).

## Repository layout

```
README.md
requirements.txt            # pinned
.gitignore
.env.example                # DATABASE_URL + AWS_* (no secrets)
main.py                     # CLI: ingest | aggregate | maps | who | ircel
sql/
  schema.sql                # measurements table + current_air_quality_3h view
src/airquality/
  config.py                 # region, queue, window, cities, WHO_GUIDELINES, WHO_BAND_CUTS,
                            # EU_LIMITS, CONFIDENCE_TIERS, COVERAGE_*, IRCEL…  (all thresholds)
  db.py                     # context-managed Postgres connection from DATABASE_URL
  ingest.py                 # SQS drain (JSONL safety net + upsert) + backfill_latency
  aggregate.py              # schema/view creation + query helpers (the 3h window, confidence)
  maps.py                   # Folium maps: pollutant maps + the city-coverage map
  who.py                    # WHO exceedance summary + good/moderate/poor bands + EU-2030 context
  coverage.py               # per-city coverage / blind-spots / latency
  ircel.py                  # IRCEL-CELINE cross-check
```

Generated artifacts (`*.html`, `*.csv`, `raw_measurements.jsonl`, `belgium_provinces.geojson`)
are git-ignored — regenerate them with the commands below.

## How to run

```powershell
# 1. environment
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. configure
copy .env.example .env        # then edit DATABASE_URL (Postgres must be running, db "airquality")
# AWS creds: ~/.aws, a project-local .aws/ folder, or AWS_* in .env

# 3. pipeline
.venv\Scripts\python.exe main.py aggregate         # create table + view (adds sent_timestamp col)
.venv\Scripts\python.exe main.py ingest            # drain SQS -> Postgres (few minutes for ~56k)
.venv\Scripts\python.exe main.py backfill-latency  # set sent_timestamp on existing rows from the JSONL
.venv\Scripts\python.exe main.py maps              # build the pollutant maps
.venv\Scripts\python.exe main.py who               # WHO exceedance summary + CSV
.venv\Scripts\python.exe main.py coverage          # per-city coverage/blind-spots + belgium_coverage.html
.venv\Scripts\python.exe main.py ircel             # cross-check vs IRCEL (needs internet)
```

`ingest` populates `sent_timestamp` on insert going forward; `backfill-latency` fills it on rows
that predate the column, straight from the JSONL backup (no re-ingest). Both are idempotent.

Open the `*.html` files in any browser. (Map data is embedded; basemap tiles, Leaflet, d3 and
the grouped-layer plugin load from CDNs, so the live render needs internet.)

## Key findings

- **It's a static, simulated batch.** A full drain returns ~55,696 records spanning a rolling
  ~4-day window ending "now"; the queue then sits empty. Values are uniform-random up to round
  per-pollutant caps (pm25≈25, pm10≈50, o3≈180, no2≈200, so2≈350, co≈10 — co in mg/m³, rest µg/m³).
- **~1 reading per city·pollutant in the window**, so the "3-hour average" is effectively the
  *latest* reading for most stations. The radius aggregation on the major-cities map deliberately
  pools nearby sensors to get n>1 (n≈2–40 per city·pollutant).
- **A near-flat choropleth is the simulator's signature.** Averaging dozens of random sensors per
  province collapses to the mean, so province averages span only a narrow band (e.g. pm25
  10.7–13.8 µg/m³). The province map is rescaled to the province-average range so the small real
  spread is at least visible.
- **WHO exceedances (indicative).** Of 15 major cities: **15/15 exceed NO₂ and SO₂**, 13/15 CO,
  3/15 O₃, **2/15 PM2.5**, 0/15 PM10 — a direct consequence of uniform values sitting near each
  pollutant's mid-range, above the stricter guidelines.
- **OpenAQ vs IRCEL divergence confirms the above.** Against IRCEL-CELINE's live RIO grid, our
  simulated values run far high: NO₂ ~95–117 vs real ~11–21 µg/m³; SO₂ ~150–180 vs single digits
  in reality. Only O₃ is the same order of magnitude. (Caveats below.)

### Trust / feasibility insights (every reading is graded)

- **Confidence-graded readings.** Each (city, pollutant) carries its measurement count, distinct
  sensor count, and a tier (`high` n≥10 & ≥3 sensors / `medium` n≥3 / `low`). The 11 larger cities
  are high-confidence; 4 small ones (Ostend, Bruges, Antwerp, Genk) are sparse — flagged amber on
  the coverage map and dashed/hollow on the pollutant maps.
- **Coverage / blind spots.** In the current snapshot all 15 target cities report all 6 pollutants,
  so there are **no blind spots** — coverage is 11 green / 4 amber / 0 red. `coverage_summary.csv`
  records present-vs-expected pollutants, totals, sensors, staleness and latency per city.
- **WHO bands + EU regulatory context.** Each reading gets an indicative `good/moderate/poor` band
  vs the WHO short-term guideline, and PM2.5/NO₂/PM10 carry **EU annual-limit context** — current
  (25/40/40) and **2030** (10/20/20) from revised Directive (EU) 2024/2881. Shown as context only:
  a 3-hour average is not comparable to an annual mean limit.
- **Per-city latency (simulator publish-lag).** `sent_timestamp − date_utc` is **negative** (median
  ≈ −24 to −33 min): the simulator stamps measurement times ~28 min *ahead* of when it publishes to
  SQS. That is a simulator artifact — a real feed's latency would be positive and must be measured
  against the genuine OpenAQ API/stream, not this stand-in.

## Cost

Sized to the **actual** volume (~56k rows, a one-shot daily batch), not to defaults.

| Option | Monthly | Notes |
|---|---|---|
| **RDS `db.t4g.micro`, 20 GB gp3** (chosen target) | **~$14/mo** | **~$0 in year one** under the RDS Free Tier (750 t4g.micro hrs + 20 GB) |
| Default `db.m5.large` | ~$140/mo | ~10× over-provisioned for this data — rejected |

SQS itself is effectively free at this volume (well within the 1M-request free tier). Compute is
a tiny scheduled consumer (see below), not an always-on server.

## Productionization (out of scope here)

Deliberately omitted per the brief — the seams are marked with `# productionization:` comments:

- **Tests** — unit tests for the SNS-unwrap/parse and the SQL window; an integration test against
  a throwaway Postgres. Hook: `ingest._to_row`, `aggregate.window`.
- **Resilience** — boto3 retry/backoff config + an SQS **dead-letter queue** for poison messages
  (currently unparseable messages are logged and left on the queue). Hook: `ingest._receive` / the
  per-message try/except.
- **Scheduling** — run `ingest` on a timer via **EventBridge → Lambda** using the existing
  **`lambda-execution-role`** (the brief forbids creating roles). Hook: wrap `ingest.drain` as the
  Lambda handler.
- **Monitoring** — a **CloudWatch alarm on data staleness** (`now() - max(date_utc)`) and on SQS
  `ApproximateAgeOfOldestMessage`. Hook: after `aggregate.report`.
- **Infrastructure as code** — Terraform/CDK for the RDS instance, queue, Lambda, schedule and
  alarms instead of console/CLI setup.
- **Real OpenAQ feed** — replace the SQS simulator with the genuine OpenAQ API/stream; only then are
  positive latency, time-aligned IRCEL validation, and real geographic variation meaningful. The
  latency seam is marked `# productionization:` in `coverage.py`. Hook: `ingest`.
- **Serving layer** — a live web server / auto-refresh dashboard instead of static HTML files
  (out of scope here). Hook: serve `current_air_quality_3h` + `coverage_summary` from an API.
- **Decision modelling** — health-impact or filtration-ROI modelling for the AirMax pitch
  (deliberately not built; would layer on top of the per-city snapshot).

## Caveats on the IRCEL cross-check

This is a **method demonstration**, not a validation: (a) our OpenAQ values are *simulated*; and
(b) they are a historical June 24–28 batch while IRCEL is *live now*, so the windows are not
aligned. A rigorous cross-validation against the **real** OpenAQ feed would need time-aligned
windows, nearest-station (not city-centroid) matching, matched averaging periods, and unit
normalisation.

## Sources

- **EU air-quality limit values** — revised Ambient Air Quality Directive **(EU) 2024/2881**,
  Annex I (2030 annual limits: PM2.5 10, NO₂ 20, PM10 20 µg/m³) and Directive 2008/50/EC (current
  25/40/40). [EUR-Lex](https://eur-lex.europa.eu/eli/dir/2024/2881/oj) ·
  [EC air quality standards](https://environment.ec.europa.eu/topics/air/air-quality/eu-air-quality-standards_en)
- **WHO 2021 global air quality guidelines** — short-term guideline values used for the indicative
  bands and exceedance flags.
