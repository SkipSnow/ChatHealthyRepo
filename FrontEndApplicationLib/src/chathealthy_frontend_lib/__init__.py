"""chathealthy-frontend-lib — shared Python code for ChatHealthy.ai front-end services.

EPIC-003 — owned by the front-end-library team.
"""

__version__ = "0.1.4"

from .runtime_governance import (
    ChatHealthyFatalError,
    register_fatal_handler,
)
from .session_token import (
    SessionToken,
    SessionTokenVerification,
    TokenInfraError,
    verify_session_token,
)

__all__ = [
    "ChatHealthyFatalError",
    "register_fatal_handler",
    "SessionToken",
    "SessionTokenVerification",
    "TokenInfraError",
    "verify_session_token",
]
