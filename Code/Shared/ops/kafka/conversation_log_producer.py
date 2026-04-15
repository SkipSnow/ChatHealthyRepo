# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Conversation Log Producer Sidecar (PID 1)
# T002: Persistent FastAPI with warm Kafka producer connection.
# Accepts HTTP POST from hook, enqueues raw payload, returns 202.
# No processing beyond enqueue.

import json
import logging
import os
import sys
import time

from fastapi import FastAPI, Request, Response
from confluent_kafka import Producer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("conversation_log_producer")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "conversation-log")

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


app = FastAPI(title="ChatHealthy Conversation Log Producer", docs_url=None)


@app.on_event("startup")
async def startup():
    """Warm the Kafka connection on startup so first request is fast."""
    _get_producer()
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
    We add a timestamp header but do NOT parse or transform the body."""
    try:
        body = await request.body()
        if not body:
            return Response(status_code=400, content="Empty payload")

        producer = _get_producer()
        producer.produce(
            topic=KAFKA_TOPIC,
            value=body,
            headers={
                "source": "claudecode",
                "occurred_at": str(time.time()),
                "hook_event": request.headers.get("X-Hook-Event", "unknown"),
            },
            callback=_delivery_report,
        )
        # Poll to trigger delivery reports (non-blocking)
        producer.poll(0)

        return Response(status_code=202, content="Enqueued")

    except Exception as e:
        _log.error("Failed to enqueue: %s", e)
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
