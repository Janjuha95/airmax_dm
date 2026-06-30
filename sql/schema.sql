-- schema.sql — measurements table + 3-hour "current air quality" view.

CREATE TABLE IF NOT EXISTS measurements (
    location_id INTEGER          NOT NULL,
    location    TEXT,
    city        TEXT,
    country     TEXT,
    parameter   TEXT             NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    unit        TEXT,
    date_utc    TIMESTAMPTZ      NOT NULL,
    date_local  TEXT,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    is_mobile   BOOLEAN,
    is_analysis BOOLEAN,
    sensor_type TEXT,
    PRIMARY KEY (location_id, parameter, date_utc)
);

-- SQS publish time (epoch from the SentTimestamp attribute), for per-city latency analysis.
-- Idempotent so re-running the schema on an existing table is safe.
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sent_timestamp TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_measurements_date_utc       ON measurements (date_utc);
CREATE INDEX IF NOT EXISTS idx_measurements_city_parameter ON measurements (city, parameter);

-- "Current air quality" = per (city, parameter) average + counts over the last 3 hours,
-- where "now" is the most recent measurement (the data is a historical batch, so anchoring
-- on wall-clock time would yield nothing).
CREATE OR REPLACE VIEW current_air_quality_3h AS
WITH anchor AS (
    SELECT max(date_utc) AS now FROM measurements
)
SELECT
    m.city,
    m.parameter,
    m.unit,
    round(avg(m.value)::numeric, 3)        AS avg_value,
    count(*)                               AS measurement_count,
    count(DISTINCT m.location_id)          AS location_count,
    avg(m.latitude)                        AS latitude,
    avg(m.longitude)                       AS longitude,
    (a.now - interval '3 hours')           AS window_start,
    a.now                                  AS window_end
FROM measurements m
CROSS JOIN anchor a
WHERE m.date_utc >  a.now - interval '3 hours'
  AND m.date_utc <= a.now
GROUP BY m.city, m.parameter, m.unit, a.now;
