# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Conversation Log Producer Sidecar (PID 1)
# T002: Persistent FastAPI with warm Kafka producer connection.
# Accepts HTTP POST from hook, enqueues raw payload, returns 202.
# No processing beyond enqueue.
#
# Per Rule-063 + BUG-001:
#   - Maintains an in-memory static (build/version/framework) seeded from
#     admin.Versions on startup.
#   - Exposes POST /version_bump for the bumper (post-commit worker or human
#     routine) to push a new {build, version, framework} into the static.
#   - Stamps every outgoing /produce message with _build/_version/_framework
#     headers so the consumer can copy them into MongoDB without its own
#     read.

import json
import logging
import os
import sys
import threading
import time

from fastapi import FastAPI, Request, Response
from confluent_kafka import Producer
from pymongo import MongoClient

# v2.2 Part B 7.3 — Event Log surface for sidecar startup failures.
# Source `ChatHealthy Conversation Log Sidecar` is registered once at
# migration time. EventID 1002 distinguishes sidecar startup failures
# from consumer batch-write failures (EventID 1001 under a different
# source name).
_EVENTLOG_SOURCE = "ChatHealthy Conversation Log Sidecar"
_EVENTLOG_EVENTID = 1002
try:
    import win32evtlog  # type: ignore
    import win32evtlogutil  # type: ignore
    _HAS_WIN32EVT = True
except ImportError:
    _HAS_WIN32EVT = False


def _emit_eventlog_critical(message: str) -> None:
    """ERROR-level Application Event Log entry. On non-Windows or when
    pywin32 is unavailable, fall back to a CRITICAL log only."""
    if _HAS_WIN32EVT:
        try:
            win32evtlogutil.ReportEvent(
                _EVENTLOG_SOURCE,
                eventID=_EVENTLOG_EVENTID,
                eventType=win32evtlog.EVENTLOG_ERROR_TYPE,
                strings=[message[:30000]],
            )
            return
        except Exception as e:  # noqa: BLE001
            logging.getLogger("conversation_log_producer").warning(
                "Event Log write failed: %s", e
            )
    logging.getLogger("conversation_log_producer").critical(message)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("conversation_log_producer")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "conversation-log")
MONGO_CONN = os.environ.get("MONGO_FRONTEND_connectionString")

# T002: One global producer for the life of the process — warm connection
_producer_config = {
    "bootstrap.servers": KAFKA_BOOTSTRAP,
    "acks": "all",
    "enable.idempotence": True,
    "retries": 10,
    "retry.backoff.ms": 100,
    "linger.ms": 0,  # Send immediately — latency over throughput
    "queue.buffering.max.messages": 10000,
}

_producer: Producer = None

# Rule-063 in-memory static — updated by /version_bump POST.
_version_info = {"build": 0, "version": "?", "framework": "?"}
_version_lock = threading.Lock()


def _get_producer() -> Producer:
    global _producer
    if _producer is None:
        _log.info("Creating Kafka producer: %s", KAFKA_BOOTSTRAP)
        _producer = Producer(_producer_config)
        _log.info("Kafka producer created")
    return _producer


def _delivery_report(err, msg):
    """Called by Kafka when delivery succeeds or fails."""
    if err is not None:
        _log.error("Kafka delivery failed: %s", err)
    else:
        _log.debug("Delivered to %s [%d] @ %d", msg.topic(), msg.partition(), msg.offset())


def _load_initial_version_from_mongo():
    """Seed the in-memory static from admin.Versions on startup."""
    if not MONGO_CONN:
        _log.warning("MONGO_FRONTEND_connectionString not set; version stamps "
                     "will be unknown until first bump")
        return
    try:
        client = MongoClient(MONGO_CONN, serverSelectionTimeoutMS=5000)
        latest = client["admin"]["Versions"].find_one(sort=[("from", -1)])
        if latest:
            # Per-env builds[] array shape: extract this producer's env slot.
            # The producer runs co-located with backend containers; ENV_PREFIX
            # identifies its env (dev|qa|prod). Off-Space defaults to dev.
            _env = os.getenv("ENV_PREFIX") or "dev"
            _builds_arr = latest.get("builds", [])
            _build_for_env = 0
            if isinstance(_builds_arr, list):
                _build_for_env = next(
                    (int(b.get("build", 0)) for b in _builds_arr
                     if isinstance(b, dict) and b.get("env") == _env),
                    0,
                )
            with _version_lock:
                _version_info["build"] = _build_for_env
                _version_info["version"] = str(latest["version"])
                _version_info["framework"] = str(latest["framework"])
            _log.info("Initial version loaded: build=%s version=%s framework=%s",
                      _version_info["build"], _version_info["version"],
                      _version_info["framework"])
        else:
            _log.warning("admin.Versions has no records; version stamps "
                         "will be unknown until first bump")
    except Exception as e:
        # v2.2 Part B 7.3: prior behaviour logged at ERROR and kept the
        # process alive with build=0, version=?, framework=? — every
        # downstream Kafka message stamped "?" until the next
        # /version_bump push. That's exactly the silent BUG-012 path:
        # a rotated credential leaves the sidecar stamping garbage and
        # the operator never sees a failure. Emit an Event Log entry
        # so the operator's Windows monitoring sees the failure, then
        # exit so the C# supervisor restarts; persistent crash-looping
        # trips the Worker.cs circuit breaker.
        msg = (
            f"ChatHealthy conversation-log sidecar failed to load initial "
            f"version from MongoDB on startup: {e}. Most likely cause: "
            f"rotated MONGO_FRONTEND_connectionString not yet propagated "
            f"to the C# service's registry read "
            f"(HKLM\\SOFTWARE\\ChatHealthy\\Secrets)."
        )
        _emit_eventlog_critical(msg)
        _log.critical(msg)
        sys.exit(1)


def _version_headers() -> dict:
    """Snapshot the static for use as message headers."""
    with _version_lock:
        return {
            "_build": str(_version_info["build"]),
            "_version": str(_version_info["version"]),
            "_framework": str(_version_info["framework"]),
        }


app = FastAPI(title="ChatHealthy Conversation Log Producer", docs_url=None)


@app.on_event("startup")
async def startup():
    """Warm the Kafka connection and seed the version static."""
    _get_producer()
    _load_initial_version_from_mongo()
    _log.info("Sidecar ready on port 8100")


@app.on_event("shutdown")
async def shutdown():
    """Flush any pending messages before exit."""
    if _producer is not None:
        remaining = _producer.flush(timeout=10)
        _log.info("Shutdown: flushed producer, %d messages remaining", remaining)


@app.post("/produce")
async def produce(request: Request):
    """T002: Accept raw payload from hook, enqueue to Kafka, return 202.
    The payload is whatever Claude Code sent to the hook — raw bytes.
    We add timestamp and version-stamp headers but do NOT parse or transform
    the body."""
    try:
        body = await request.body()
        if not body:
            return Response(status_code=400, content="Empty payload")

        producer = _get_producer()
        headers = {
            "source": "claudecode",
            "occurred_at": str(time.time()),
            "hook_event": request.headers.get("X-Hook-Event", "unknown"),
        }
        headers.update(_version_headers())  # _build / _version / _framework
        producer.produce(
            topic=KAFKA_TOPIC,
            value=body,
            headers=headers,
            callback=_delivery_report,
        )
        # Poll to trigger delivery reports (non-blocking)
        producer.poll(0)

        return Response(status_code=202, content="Enqueued")

    except Exception as e:
        _log.error("Failed to enqueue: %s", e)
        return Response(status_code=500, content=str(e))


@app.post("/version_bump")
async def version_bump(request: Request):
    """Rule-063: receive a push of new {build, version, framework} values
    and update the in-memory static. Body is JSON: {build, version, framework}.
    Called by the build-bump enforcement worker (post-commit) and by the
    human routine that sets version/framework on prod deploy."""
    try:
        payload = await request.json()
        with _version_lock:
            _version_info["build"] = int(payload["build"])
            _version_info["version"] = str(payload["version"])
            _version_info["framework"] = str(payload["framework"])
        _log.info("Version updated via /version_bump: build=%s version=%s framework=%s",
                  _version_info["build"], _version_info["version"],
                  _version_info["framework"])
        return Response(status_code=200, content="Updated")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        _log.error("/version_bump bad payload: %s", e)
        return Response(status_code=400, content=f"Bad payload: {e}")
    except Exception as e:
        _log.error("/version_bump failed: %s", e)
        return Response(status_code=500, content=str(e))


@app.get("/health")
async def health():
    """Health check for the C# supervisor."""
    try:
        producer = _get_producer()
        # Poll triggers any pending callbacks — if producer is dead this will show
        producer.poll(0)
        return {"status": "healthy", "topic": KAFKA_TOPIC, "bootstrap": KAFKA_BOOTSTRAP}
    except Exception as e:
        return Response(status_code=503, content=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="info")
