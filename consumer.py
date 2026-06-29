"""consumer.py — drain the openaq-sarthak queue safely into Postgres.

Per batch of up to 10 messages:
  1. Unwrap SNS envelope -> inner measurement dict.
  2. SAFETY NET: append one line per record to raw_measurements.jsonl and flush
     (BEFORE any delete), so a crash in the DB/delete step never loses data.
  3. Upsert with execute_values + ON CONFLICT DO NOTHING, commit.
  4. ONLY after the raw append AND a successful insert: DeleteMessageBatch.
Stops when the queue is empty. Reruns are safe (ON CONFLICT dedupes; undeleted
messages simply reappear).
"""

import sys
import json
from datetime import datetime, timezone

import boto3
import psycopg2.extras
from botocore.exceptions import ClientError, ParamValidationError

from explore_queue import resolve_credentials, REGION
from db import connect

try:
    sys.stdout.reconfigure(encoding="utf-8")  # so µg/m³ prints correctly
except Exception:
    pass

QUEUE_NAME = "openaq-sarthak"
RAW_PATH = "raw_measurements.jsonl"
EMPTY_POLLS_BEFORE_STOP = 3   # confirm the queue is really empty before stopping

INSERT_SQL = """
INSERT INTO measurements (
    location_id, location, city, country, parameter, value, unit,
    date_utc, date_local, latitude, longitude, is_mobile, is_analysis, sensor_type
) VALUES %s
ON CONFLICT (location_id, parameter, date_utc) DO NOTHING
"""


def get_sqs():
    resolve_credentials()
    return boto3.session.Session(region_name=REGION).client("sqs")


def receive(sqs, url):
    common = dict(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1, VisibilityTimeout=60)
    try:
        return sqs.receive_message(MessageSystemAttributeNames=["SentTimestamp"], **common)
    except (ParamValidationError, ClientError):
        return sqs.receive_message(AttributeNames=["SentTimestamp"], **common)


def parse_utc(s):
    # "YYYY-MM-DD HH:MM:SS.ffffff" — naive, treat as UTC
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def to_row(inner):
    coords = inner.get("coordinates") or {}
    date = inner.get("date") or {}
    return (
        inner["locationId"],
        inner.get("location"),
        inner.get("city"),
        inner.get("country"),
        inner["parameter"],
        inner["value"],
        inner.get("unit"),
        parse_utc(date["utc"]),
        date.get("local"),
        coords.get("latitude"),
        coords.get("longitude"),
        inner.get("isMobile"),
        inner.get("isAnalysis"),
        inner.get("sensorType"),
    )


def main():
    sqs = get_sqs()
    url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    print(f"Draining {url}", flush=True)

    conn = connect()
    received = inserted = duplicates = errors = 0
    empties = 0
    next_progress = 5000

    with open(RAW_PATH, "w", encoding="utf-8") as raw:
        while True:
            msgs = receive(sqs, url).get("Messages", [])
            if not msgs:
                empties += 1
                if empties >= EMPTY_POLLS_BEFORE_STOP:
                    break
                continue
            empties = 0

            rows, raw_lines, deletes = [], [], []
            for i, m in enumerate(msgs):
                try:
                    inner = json.loads(json.loads(m["Body"])["Message"])
                    row = to_row(inner)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    errors += 1
                    print(f"  skip unparseable message {m.get('MessageId')}: {e}", flush=True)
                    continue
                rows.append(row)
                raw_lines.append(json.dumps({
                    "message_id": m["MessageId"],
                    "sent_timestamp": m.get("Attributes", {}).get("SentTimestamp"),
                    "body": inner,
                }, ensure_ascii=False))
                deletes.append({"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]})

            if not rows:
                continue

            # 1) safety net: persist raw BEFORE deleting anything
            raw.write("\n".join(raw_lines) + "\n")
            raw.flush()

            # 2) upsert this batch and commit
            with conn, conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, INSERT_SQL, rows)
                batch_inserted = cur.rowcount
            inserted += batch_inserted
            duplicates += len(rows) - batch_inserted
            received += len(rows)

            # 3) delete only after raw append + successful insert
            failed = sqs.delete_message_batch(QueueUrl=url, Entries=deletes).get("Failed", [])
            if failed:
                print(f"  WARNING: {len(failed)} deletes failed (will reappear; dedup handles it)", flush=True)

            if received >= next_progress:
                print(f"  progress: received={received} inserted={inserted} duplicates={duplicates}", flush=True)
                next_progress += 5000

    conn.close()
    print("\nDrain complete.", flush=True)
    print(f"  received            : {received}")
    print(f"  inserted            : {inserted}")
    print(f"  skipped-as-duplicate: {duplicates}")
    if errors:
        print(f"  unparseable (left in queue): {errors}")


if __name__ == "__main__":
    main()
