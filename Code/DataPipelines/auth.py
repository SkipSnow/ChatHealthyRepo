"""
Token-based authentication for Azure Functions.
Validates Bearer tokens and returns user identity.
"""
import json
import os
from typing import Optional


def validate_bearer_token(auth_header: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    Validate the Authorization Bearer token and return user ID if valid.

    Expects: Authorization: Bearer <token>
    Tokens are configured in API_TOKEN_MAP (JSON: {"token": "user_id", ...})

    Returns:
        (is_valid, user_id) - user_id is None if invalid
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return False, None

    token = auth_header[7:].strip()  # Remove "Bearer "
    if not token:
        return False, None

    token_map_json = os.environ.get("API_TOKEN_MAP", "{}")
    try:
        token_map = json.loads(token_map_json)
    except json.JSONDecodeError:
        return False, None

    user_id = token_map.get(token)
    if user_id:
        return True, user_id

    return False, None


def require_auth(req) -> tuple[Optional[str], Optional[tuple[int, str]]]:
    """
    Check request for valid Bearer token (header or ?token= query param).

    Returns:
        (user_id, None) if valid
        (None, (status_code, error_message)) if invalid
    """
    auth_header = req.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif req.params.get("token"):
        token = req.params.get("token")

    is_valid, user_id = validate_bearer_token(
        f"Bearer {token}" if token else None
    )

    if not is_valid:
        return None, (401, "Missing or invalid token. Use: Authorization: Bearer <token> or ?token=<token>")

    return user_id, None
