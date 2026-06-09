"""chathealthy-frontend-lib — shared Python code for ChatHealthy.ai front-end services.

EPIC-003 — owned by the front-end-library team.
"""

__version__ = "0.1.7"

from .authentication import (
    SessionToken,
    SessionTokenVerification,
    TokenInfraError,
    TokenWidgetData,
)

__all__ = [
    "SessionToken",
    "SessionTokenVerification",
    "TokenInfraError",
    "TokenWidgetData",
]
