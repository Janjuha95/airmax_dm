"""SQS drain into Postgres.

Per batch: unwrap the SNS envelope, append every record to a JSONL safety net (BEFORE any
delete), upsert with ON CONFLICT DO NOTHING (idempotent), then DeleteMessageBatch. The JSONL
backup + idempotent upsert make the drain safe to crash and rerun.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import boto3
import psycopg2.extras
from botocore.exceptions import ClientError, ParamValidationError

from . import config
from .db import connection

log = logging.getLogger(__name__)

INSERT_SQL = """
INSERT INTO measurements (
    location_id, location, city, country, parameter, value, unit,
    date_utc, date_local, latitude, longitude, is_mobile, is_analysis, sensor_type
) VALUES %s
ON CONFLICT (location_id, parameter, date_utc) DO NOTHING
"""


def _sqs_client():
    config.resolve_aws_credentials()
    return boto3.session.Session(region_name=config.AWS_REGION).client("sqs")


def _receive(sqs, url) -> dict:
    common = dict(QueueUrl=url, MaxNumberOfMessages=config.RECEIVE_BATCH,
                  WaitTimeSeconds=config.WAIT_TIME_SECONDS, VisibilityTimeout=config.VISIBILITY_TIMEOUT)
    try:
        return sqs.receive_message(MessageSystemAttributeNames=["SentTimestamp"], **common)
    except (ParamValidationError, ClientError):
        return sqs.receive_message(AttributeNames=["SentTimestamp"], **common)


def _parse_utc(s: str) -> datetime:
    # "YYYY-MM-DD HH:MM:SS.ffffff" — naive, treat as UTC
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _to_row(inner: dict) -> tuple:
    coords = inner.get("coordinates") or {}
    date = inner.get("date") or {}
    return (
        inner["locationId"], inner.get("location"), inner.get("city"), inner.get("country"),
        inner["parameter"], inner["value"], inner.get("unit"),
        _parse_utc(date["utc"]), date.get("local"),
        coords.get("latitude"), coords.get("longitude"),
        inner.get("isMobile"), inner.get("isAnalysis"), inner.get("sensorType"),
    )


def drain() -> dict:
    """Drain the configured SQS queue into Postgres. Returns counts."""
    sqs = _sqs_client()
    url = sqs.get_queue_url(QueueName=config.SQS_QUEUE_NAME)["QueueUrl"]
    log.info("Draining %s", url)

    received = inserted = duplicates = errors = 0
    empties = 0
    next_progress = config.PROGRESS_EVERY

    with connection() as conn, open(config.RAW_BACKUP_PATH, "w", encoding="utf-8") as raw:
        while True:
            msgs = _receive(sqs, url).get("Messages", [])
            if not msgs:
                empties += 1
                if empties >= config.EMPTY_POLLS_BEFORE_STOP:
                    break
                continue
            empties = 0

            rows, raw_lines, deletes = [], [], []
            for i, m in enumerate(msgs):
                try:
                    inner = json.loads(json.loads(m["Body"])["Message"])
                    row = _to_row(inner)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    errors += 1
                    log.warning("skip unparseable message %s: %s", m.get("MessageId"), e)
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

            # 2) idempotent upsert, commit per batch
            with conn, conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, INSERT_SQL, rows)
                batch_inserted = cur.rowcount
            inserted += batch_inserted
            duplicates += len(rows) - batch_inserted
            received += len(rows)

            # 3) delete only after raw append + successful insert
            failed = sqs.delete_message_batch(QueueUrl=url, Entries=deletes).get("Failed", [])
            if failed:
                log.warning("%d deletes failed (will reappear; dedup handles it)", len(failed))

            if received >= next_progress:
                log.info("progress: received=%d inserted=%d duplicates=%d", received, inserted, duplicates)
                next_progress += config.PROGRESS_EVERY

    log.info("Drain complete: received=%d inserted=%d duplicates=%d errors=%d",
             received, inserted, duplicates, errors)
    return {"received": received, "inserted": inserted, "duplicates": duplicates, "errors": errors}
