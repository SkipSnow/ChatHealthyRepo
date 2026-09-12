from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""pipeline_health.py — MongoDB health check and admin notifications.

send_admin_notification(subject, text): sends email via SparkPost.
check_mongo_health(config):             pings MongoDB, emails admin and raises if unreachable.

Environment variables:
    SPARKMAIL_API_KEY          SparkPost API key
    NOTIFICATION_FROM_EMAIL    Verified sender (Skip.Snow@mail.chatHealthy.ai)
    NOTIFICATION_TO_EMAIL      Admin recipient
"""


import os

import requests
from pymongo import MongoClient
from sparkpost import SparkPost


def send_admin_notification(subject: str, text: str) -> None:
    """Send an email to the pipeline admin via SparkPost.

    Logs a warning and returns silently if credentials are not configured,
    so missing env vars never mask the underlying error.
    """
    api_key    = os.environ.get("SPARKMAIL_API_KEY")
    from_email = os.environ.get("NOTIFICATION_FROM_EMAIL")
    to_email   = os.environ.get("NOTIFICATION_TO_EMAIL")

    if not (api_key and from_email and to_email):
        ChatHealthyLoggingService().warning("Admin notification skipped — SparkPost credentials not configured.")
        return

    try:
        sp = SparkPost(api_key)
        sp.transmissions.send(
            recipients=[to_email],
            from_email=from_email,
            subject=subject,
            text=text,
        )
        ChatHealthyLoggingService().info("Admin notification sent to %s: %s", to_email, subject)
    except Exception as exc:
        ChatHealthyLoggingService().error("SparkPost send failed: %s", exc)


def send_pushover(title: str, message: str) -> None:
    """Send a push notification via Pushover.

    Logs a warning and returns silently if credentials are not configured.

    Environment variables:
        PUSHOVER_TOKEN      Application API token
        PUSHOVER_USER_KEY   Recipient user key
    """
    token = os.environ.get("PUSHOVER_TOKEN")
    user_key = os.environ.get("PUSHOVER_USER_KEY")

    if not (token and user_key):
        ChatHealthyLoggingService().warning("Pushover notification skipped — credentials not configured.")
        return

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": token, "user": user_key, "title": title, "message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            ChatHealthyLoggingService().info("Pushover sent: %s", title)
        else:
            ChatHealthyLoggingService().error("Pushover failed (%d): %s", resp.status_code, resp.text)
    except Exception as exc:
        ChatHealthyLoggingService().error("Pushover send failed: %s", exc)


def check_mongo_health(config: dict = None) -> dict:
    """Ping MongoDB. Email admin and raise RuntimeError if unreachable.

    Called at the start of every pipeline run so the orchestration fails
    fast with a clear message rather than 32 workers all crashing with
    cryptic ReplicaSetNoPrimary errors.

    Returns {"status": "ok"} if the cluster is reachable and has a primary.
    """
    try:
        from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
        client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyDataPipelines")
        client.admin.command("ping")
        client.close()
        return {"status": "ok"}
    except Exception as exc:
        msg = str(exc)
        send_admin_notification(
            subject="Pipeline BLOCKED — MongoDB unreachable",
            text=(
                "The ChatHealthy data pipeline could not connect to MongoDB "
                "and has been stopped.\n\n"
                f"Error: {msg}\n\n"
                "Please check the Atlas cluster status and restart the pipeline "
                "once MongoDB is available."
            ),
        )
        raise ChatHealthyException(mode="runtime_error", message=f"MongoDB health check failed: {msg}",
            exception=exc) from exc
