"""explore_queue.py — non-destructive probe of the OpenAQ SQS queue.

Step 1 of the AirMax assignment: find out what is ACTUALLY in the queue before we
choose an architecture. This script:
  1. Verifies AWS access (sts.get_caller_identity).
  2. Finds the SQS queue with prefix "openaq" in eu-west-1 (prints URL + ARN).
  3. Reads queue depth WITHOUT consuming (get_queue_attributes).
  4. Peeks exactly ONE message (long poll, short visibility timeout) and does NOT
     delete it — so it returns to the queue for the real pipeline.
  5. Summarises: live measurement vs S3-object event, pollutant fields, latency.

It never reads or prints credential file contents; boto3 loads those itself.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

REGION = "eu-west-1"        # operate in Ireland only
QUEUE_PREFIX = "openaq"
QUEUE_NAME_HINT = "sarthak"  # prefer my dedicated per-interviewee queue (openaq-sarthak)
VISIBILITY_TIMEOUT = 5      # seconds hidden after our peek; short, and we never delete it
WAIT_TIME_SECONDS = 20      # SQS long polling (max) — cheap, avoids busy empty receives


def resolve_credentials():
    """Find AWS credentials and return (source_label, creds_path | None).

    The assignment says credentials live in ~/.aws; on this machine they are in a
    project-local .aws/ (or aws/) folder. A populated ~/.aws wins (boto3 default);
    otherwise we point boto3 at the project-local file. We only set file *paths* as
    env vars — boto3 reads the files itself; we never open or print their contents.
    """
    home_creds = Path.home() / ".aws" / "credentials"
    if home_creds.exists() and home_creds.stat().st_size > 0:
        return "default (~/.aws)", home_creds

    project = Path(__file__).resolve().parent
    for folder in (".aws", "aws"):
        creds = project / folder / "credentials"
        if creds.exists():
            os.environ.setdefault("AWS_SHARED_CREDENTIALS_FILE", str(creds))
            cfg = project / folder / "config"
            if cfg.exists():
                os.environ.setdefault("AWS_CONFIG_FILE", str(cfg))
            return f"project-local ({creds.parent})", creds
    return "default chain (env vars / instance role)", None


def get_clients():
    session = boto3.session.Session(region_name=REGION)
    return session.client("sts"), session.client("sqs")


def verify_identity(sts) -> None:
    ident = sts.get_caller_identity()
    print("AWS identity OK")
    print(f"  Account: {ident['Account']}")
    print(f"  ARN:     {ident['Arn']}")
    print(f"  UserId:  {ident['UserId']}")


def find_queue(sqs):
    urls = sqs.list_queues(QueueNamePrefix=QUEUE_PREFIX).get("QueueUrls", [])
    if not urls:
        print(f"No SQS queues found with prefix '{QUEUE_PREFIX}' in {REGION}.")
        return None

    name_of = lambda u: u.rsplit("/", 1)[-1]
    print(f"Found {len(urls)} queue(s) with prefix '{QUEUE_PREFIX}'. Depth survey (approx):")
    for u in sorted(urls, key=name_of):
        a = sqs.get_queue_attributes(
            QueueUrl=u,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        print(f"  {name_of(u):26} visible={a['ApproximateNumberOfMessages']:>6}  "
              f"in-flight={a['ApproximateNumberOfMessagesNotVisible']:>6}")

    # Prefer my dedicated queue (contains my name); else the exact 'openaq'; else first.
    chosen = next((u for u in urls if QUEUE_NAME_HINT in name_of(u)), None)
    if chosen is None:
        chosen = next((u for u in urls if name_of(u) == QUEUE_PREFIX), urls[0])
    arn = sqs.get_queue_attributes(QueueUrl=chosen, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    print(f"\nSelected queue (hint '{QUEUE_NAME_HINT}'):\n  URL: {chosen}\n  ARN: {arn}")
    return chosen


def queue_depth(sqs, url) -> None:
    attrs = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
            "CreatedTimestamp",
        ],
    )["Attributes"]
    print("Queue depth (approximate, non-consuming):")
    print(f"  Visible (available):     {attrs.get('ApproximateNumberOfMessages')}")
    print(f"  Not visible (in-flight): {attrs.get('ApproximateNumberOfMessagesNotVisible')}")
    print(f"  Delayed:                 {attrs.get('ApproximateNumberOfMessagesDelayed')}")
    created = attrs.get("CreatedTimestamp")
    if created:
        print(f"  Queue created (UTC):     {datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()}")


def receive_one(sqs, url):
    """Receive a single message, requesting ALL system + message attributes.

    Does NOT delete it. Newer SQS uses MessageSystemAttributeNames; we fall back to
    the older AttributeNames for portability.
    """
    common = dict(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        VisibilityTimeout=VISIBILITY_TIMEOUT,
        WaitTimeSeconds=WAIT_TIME_SECONDS,
        MessageAttributeNames=["All"],
    )
    try:
        resp = sqs.receive_message(MessageSystemAttributeNames=["All"], **common)
    except (ParamValidationError, ClientError):
        resp = sqs.receive_message(AttributeNames=["All"], **common)
    msgs = resp.get("Messages", [])
    return msgs[0] if msgs else None


def parse_dt(val):
    """Best-effort parse of an ISO-8601 string or epoch number to aware UTC datetime."""
    if isinstance(val, (int, float)):
        v = float(val)
        if v > 1e12:  # epoch milliseconds
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def find_measurement_time(inner):
    """Locate a measurement timestamp across known OpenAQ payload shapes."""
    if not isinstance(inner, dict):
        return None, None
    candidates = []
    date = inner.get("date")
    if isinstance(date, dict):
        candidates += [("date.utc", date.get("utc")), ("date.local", date.get("local"))]
    elif isinstance(date, str):
        candidates.append(("date", date))
    for key in ("datetime", "date_utc", "timestamp", "time", "lastUpdated"):
        if key in inner:
            candidates.append((key, inner[key]))
    for label, val in candidates:
        if val and (dt := parse_dt(val)):
            return label, dt
    return None, None


def analyze(msg):
    print("\n=== RAW SQS MESSAGE ===")
    print(f"MessageId: {msg.get('MessageId')}")
    print("(message NOT deleted — returns to queue after visibility timeout)")

    sys_attrs = msg.get("Attributes", {})
    print("\n--- System attributes ---")
    for k, v in sys_attrs.items():
        print(f"  {k}: {v}")
    sent_dt = None
    if (sent_ts := sys_attrs.get("SentTimestamp")):
        sent_dt = datetime.fromtimestamp(int(sent_ts) / 1000, tz=timezone.utc)
        print(f"  -> SentTimestamp (UTC): {sent_dt.isoformat()}")

    print("\n--- SQS-level message attributes ---")
    msg_attrs = msg.get("MessageAttributes", {})
    if msg_attrs:
        for k, v in msg_attrs.items():
            print(f"  {k}: {v.get('StringValue', v.get('BinaryValue', v))}")
    else:
        print("  (none at SQS level)")

    raw_body = msg.get("Body", "")
    print("\n--- RAW BODY ---")
    print(raw_body)

    try:
        body_obj = json.loads(raw_body)
    except json.JSONDecodeError:
        print("\n(Body is not JSON.)")
        return sent_dt, None

    is_sns = isinstance(body_obj, dict) and body_obj.get("Type") == "Notification" and "Message" in body_obj
    if is_sns:
        print("\n--- SNS ENVELOPE DETECTED ---")
        print(f"  TopicArn:      {body_obj.get('TopicArn')}")
        print(f"  SNS Timestamp: {body_obj.get('Timestamp')}")
        sns_attrs = body_obj.get("MessageAttributes", {})
        if sns_attrs:
            print("  SNS MessageAttributes (filter attributes live here when not raw-delivery):")
            for k, v in sns_attrs.items():
                print(f"    {k}: {v.get('Value', v)}")
        inner_raw = body_obj.get("Message")
        print("\n--- SNS INNER Message (raw) ---")
        print(inner_raw)
        try:
            inner = json.loads(inner_raw)
        except (json.JSONDecodeError, TypeError):
            inner = inner_raw
    else:
        print("\n(No SNS envelope; body is the payload directly.)")
        inner = body_obj

    print("\n--- PARSED PAYLOAD (pretty) ---")
    print(json.dumps(inner, indent=2, default=str))
    return sent_dt, inner


def summarize(sent_dt, inner) -> None:
    print("\n=== SUMMARY ===")
    if inner is None:
        print("Could not parse a payload from the body.")
        return

    # S3 object-created event?
    if isinstance(inner, dict) and isinstance(inner.get("Records"), list) and inner["Records"]:
        rec0 = inner["Records"][0]
        if rec0.get("eventSource") == "aws:s3" or "s3" in rec0:
            s3 = rec0.get("s3", {})
            print("Body type: S3 OBJECT-CREATED EVENT (not a live measurement).")
            print(f"  bucket: {s3.get('bucket', {}).get('name')}")
            print(f"  key:    {s3.get('object', {}).get('key')}")
            print("  => This is the daily-archive pipeline (batch, ~72h late), not a firehose.")
            return

    print("Body type: looks like a LIVE MEASUREMENT record.")
    if isinstance(inner, dict):
        for f in ("parameter", "value", "unit", "location", "locationId", "city",
                  "country", "sensorId", "coordinates"):
            if f in inner:
                print(f"  {f}: {inner[f]}")
    label, meas_dt = find_measurement_time(inner)
    if meas_dt:
        print(f"  measurement time ({label}): {meas_dt.isoformat()}")
    if sent_dt and meas_dt:
        print(f"  SQS SentTimestamp:        {sent_dt.isoformat()}")
        print(f"  Apparent latency (SentTimestamp - measurement time): {sent_dt - meas_dt}")


def main():
    src, creds_path = resolve_credentials()
    print(f"Credentials source: {src}")
    if creds_path is not None and creds_path.stat().st_size == 0:
        print(f"  WARNING: {creds_path} is EMPTY (0 bytes) — boto3 has nothing to load.")
    print(f"Region: {REGION}\n")
    sts, sqs = get_clients()

    try:
        verify_identity(sts)
    except (BotoCoreError, ClientError) as e:
        print("\nCREDENTIAL / ACCESS ERROR — stopping.")
        print(f"  {type(e).__name__}: {e}")
        sys.exit(1)

    print()
    url = find_queue(sqs)
    if not url:
        sys.exit(1)

    print()
    queue_depth(sqs, url)

    msg = receive_one(sqs, url)
    if not msg:
        print("\nNo message received within the long-poll window (queue may be empty right now).")
        return
    sent_dt, inner = analyze(msg)
    summarize(sent_dt, inner)
    print("\nDone. No messages were deleted.")


if __name__ == "__main__":
    main()
