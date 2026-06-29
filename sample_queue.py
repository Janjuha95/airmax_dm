"""sample_queue.py — read-only characterisation of the openaq-sarthak batch.

Step 2: sample ~200 DISTINCT messages WITHOUT deleting, then describe the data
(time span, pollutants, cities) and decide static vs live.

Reuses credential + timestamp helpers from explore_queue.py. Nothing is deleted:
sampled messages get a short visibility timeout and return to the queue. To make the
start/end depth comparison valid (sampled messages are briefly in-flight), we report
visible + not-visible at both points AND wait for the visibility window to expire
before the final reading.
"""

import sys
import time
import json
from collections import Counter
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError, ParamValidationError

from explore_queue import resolve_credentials, REGION, parse_dt

# UTF-8 stdout so µg/m³ renders correctly on Windows consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

QUEUE_NAME = "openaq-sarthak"
SAMPLE_TARGET = 200
VISIBILITY_TIMEOUT = 30      # seconds; sampled messages return after this (we never delete)
WAIT_TIME_SECONDS = 5        # SQS long poll per receive (returns fast when messages exist)
MAX_RECEIVE_CALLS = 100      # safety cap so we never loop forever


def get_sqs():
    resolve_credentials()
    return boto3.session.Session(region_name=REGION).client("sqs")


def queue_url(sqs):
    return sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]


def depth(sqs, url):
    a = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    visible = int(a["ApproximateNumberOfMessages"])
    not_visible = int(a["ApproximateNumberOfMessagesNotVisible"])
    return visible, not_visible


def receive_batch(sqs, url):
    common = dict(
        QueueUrl=url,
        MaxNumberOfMessages=10,
        VisibilityTimeout=VISIBILITY_TIMEOUT,
        WaitTimeSeconds=WAIT_TIME_SECONDS,
        MessageAttributeNames=["All"],
    )
    try:
        return sqs.receive_message(MessageSystemAttributeNames=["All"], **common)
    except (ParamValidationError, ClientError):
        return sqs.receive_message(AttributeNames=["All"], **common)


def parse_message(m):
    """Return (sent_dt, inner_record) for one SQS message; inner may be None."""
    sent_dt = None
    if (st := m.get("Attributes", {}).get("SentTimestamp")):
        sent_dt = datetime.fromtimestamp(int(st) / 1000, tz=timezone.utc)
    inner = None
    try:
        body = json.loads(m["Body"])
        inner = json.loads(body["Message"]) if isinstance(body, dict) and "Message" in body else body
    except (json.JSONDecodeError, KeyError, TypeError):
        inner = None
    return sent_dt, inner


def sample(sqs, url):
    """Collect up to SAMPLE_TARGET distinct messages (dedupe by MessageId)."""
    seen = {}
    empties = 0
    calls = 0
    for _ in range(MAX_RECEIVE_CALLS):
        if len(seen) >= SAMPLE_TARGET:
            break
        calls += 1
        msgs = receive_batch(sqs, url).get("Messages", [])
        if not msgs:
            empties += 1
            if empties >= 3:
                break
            continue
        empties = 0
        for m in msgs:
            seen.setdefault(m["MessageId"], parse_message(m))
    print(f"  collected {len(seen)} distinct messages in {calls} receive call(s)")
    return seen


def analyse(seen):
    meas_times, sent_times = [], []
    params, city_param = Counter(), Counter()
    cities, location_ids = set(), set()
    unit_for = {}

    for sent_dt, inner in seen.values():
        if inner is None:
            continue
        if sent_dt:
            sent_times.append(sent_dt)
        date = inner.get("date")
        if isinstance(date, dict) and (mt := parse_dt(date.get("utc"))):
            meas_times.append(mt)
        param = inner.get("parameter")
        city = inner.get("city")
        if param:
            params[param] += 1
            unit_for.setdefault(param, inner.get("unit"))
        if city:
            cities.add(city)
        if (lid := inner.get("locationId")) is not None:
            location_ids.add(lid)
        if city and param:
            city_param[(city, param)] += 1

    return dict(meas_times=meas_times, sent_times=sent_times, params=params,
                city_param=city_param, cities=cities, location_ids=location_ids,
                unit_for=unit_for)


def span(label, times):
    if not times:
        print(f"  {label}: (none parsed)")
        return None, None
    lo, hi = min(times), max(times)
    print(f"  {label}: {lo.isoformat()}  ->  {hi.isoformat()}   (span {hi - lo})")
    return lo, hi


def report(stats):
    print("\n--- Timestamps ---")
    span("measurement date.utc", stats["meas_times"])
    span("SentTimestamp       ", stats["sent_times"])

    print("\n--- Pollutants (parameter counts in sample) ---")
    for p, c in stats["params"].most_common():
        print(f"  {p:8} {c:5}   unit={stats['unit_for'].get(p)}")

    print(f"\n--- Cardinality ---")
    print(f"  distinct cities:     {len(stats['cities'])}")
    print(f"  distinct locationId: {len(stats['location_ids'])}")

    print("\n--- Top 10 (city, parameter) by sample count ---")
    for (city, param), c in stats["city_param"].most_common(10):
        print(f"  {c:4}  {city:24} {param}")


def main():
    sqs = get_sqs()
    url = queue_url(sqs)
    print(f"Queue: {url}")

    v0, nv0 = depth(sqs, url)
    print(f"\nSTART depth: visible={v0}  not-visible={nv0}  total={v0 + nv0}")

    print(f"\nSampling up to {SAMPLE_TARGET} distinct messages "
          f"(visibility={VISIBILITY_TIMEOUT}s, no deletes)...")
    t0 = time.monotonic()
    seen = sample(sqs, url)

    vi, nvi = depth(sqs, url)
    print(f"\nEND (immediate) depth: visible={vi}  not-visible={nvi}  total={vi + nvi}")
    print(f"  (~{len(seen)} of ours are temporarily in-flight here)")

    wait = VISIBILITY_TIMEOUT + 2 - (time.monotonic() - t0)
    if wait > 0:
        print(f"\nWaiting {wait:.0f}s for the visibility window to expire so samples return...")
        time.sleep(wait)

    v1, nv1 = depth(sqs, url)
    print(f"\nEND (settled) depth: visible={v1}  not-visible={nv1}  total={v1 + nv1}")

    stats = analyse(seen)
    report(stats)

    print("\n=== VERDICT ===")
    grew_total = (v1 + nv1) - (v0 + nv0)
    print(f"  total messages start->settled: {v0 + nv0} -> {v1 + nv1}  (delta {grew_total:+d})")
    state = "LIVE (still publishing)" if grew_total > 5 else "STATIC (one-time batch)"
    print(f"  queue appears: {state}")
    if stats["meas_times"]:
        hi = max(stats["meas_times"])
        lo3 = hi.replace(microsecond=0)
        from datetime import timedelta
        window_start = hi - timedelta(hours=3)
        in_window = sum(1 for t in stats["meas_times"] if window_start <= t <= hi)
        print(f"  max(date.utc) = {hi.isoformat()}")
        print(f"  natural 3h window = [{window_start.isoformat()}, {hi.isoformat()}]")
        print(f"  sampled measurements falling in that window: {in_window}/{len(stats['meas_times'])}")

    print("\nNo messages were deleted (delete_message was never called).")


if __name__ == "__main__":
    main()
