# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).

"""
OTP Manager — one-time password key exchange for Brain API.

Boss generates an OTP and gives it to an agent (e.g. GPT) verbally.
The agent calls GET /api/ExchangeOTP?code=<otp> to receive their
permanent Bearer key. The OTP is consumed on first use and expires
after 30 minutes regardless.

Storage: {ENV_PREFIX}_MachineBrain.otp_tokens  (ChatHealthyFrontEndCluster)
"""

import os
import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Optional
from pymongo import MongoClient
from pymongo.errors import PyMongoError

_ENV_PREFIX    = os.getenv("ENV_PREFIX", "dev")
_DB_NAME       = f"{_ENV_PREFIX}_MachineBrain"
_COLLECTION    = "otp_tokens"
_TTL_MINUTES   = 30

_client: Optional[MongoClient] = None


def _get_col():
    global _client
    if _client is None:
        conn = os.getenv("MONGO_FRONTEND_connectionString") or os.getenv("MONGODB_CONNECTION_STRING")
        if not conn:
            raise EnvironmentError("MONGO_FRONTEND_connectionString is not set.")
        _client = MongoClient(conn)
    return _client[_DB_NAME][_COLLECTION]


# DEAD CODE (v4-031) -- unreferenced function 'generate_otp', marked for deletion
# def generate_otp(agent: str, bearer_key: str) -> str:
#     """
#     Generate and store a one-time password that exchanges for a Bearer key.
#
#     Args:
#         agent:      Agent name this OTP is for (e.g. "GPT")
#         bearer_key: The permanent Bearer key to return on successful exchange
#
#     Returns:
#         OTP string in format XXXX-XXXX  (give this to the agent verbally)
#     """
#     chars = string.ascii_uppercase + string.digits
#     otp = (
#         ''.join(secrets.choice(chars) for _ in range(4)) + '-' +
#         ''.join(secrets.choice(chars) for _ in range(4))
#     )
#     record = {
#         "otp":        otp,
#         "agent":      agent,
#         "bearer_key": bearer_key,
#         "created_at": datetime.now(timezone.utc),
#         "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_TTL_MINUTES),
#         "used":       False,
#     }
#     _get_col().insert_one(record)
#     return otp
# END DEAD CODE


def exchange_otp(otp: str) -> tuple[bool, str, Optional[str]]:
    """
    Exchange a one-time password for a Bearer key.

    Args:
        otp: The OTP string provided to the agent

    Returns:
        (success, agent_name, bearer_key)
        On failure: (False, "", None) with reason in agent_name field
    """
    col = _get_col()
    try:
        record = col.find_one({"otp": otp})
    except PyMongoError as e:
        return False, f"Database error: {e}", None

    if not record:
        return False, "Invalid OTP", None

    if record["used"]:
        return False, "OTP already used", None

    if datetime.now(timezone.utc) > record["expires_at"]:
        return False, "OTP expired", None

    # Consume the OTP — one-time use
    col.update_one(
        {"otp": otp},
        {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}}
    )

    return True, record["agent"], record["bearer_key"]
