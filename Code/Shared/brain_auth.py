"""
Brain API Authentication — key registry and scope enforcement.

Resolves Bearer token → agent identity + permitted scopes.
Used by FastAPI middleware on all /brain/* endpoints.

Keys are stored in environment variables (Azure Key Vault in prod).
  BRAIN_KEY_GPT    — GPT's key (ChatGPT Custom Action)
  BRAIN_KEY_CLAUDE — Claude's key (internal service calls)
  BRAIN_KEY_SKIP   — Skip's key (admin, reporting, override)

Scopes:
  read:assignments   — GET /brain/assignments
  read:reviews       — GET /brain/reviews/*
  write:assurance    — POST /brain/assurance
  write:usage        — POST /brain/usage
  write:assignments  — POST /brain/assignments (Boss/Claude only)
  admin              — budget limits, reset, full report (Skip only)

ADR: ADR-0007, framework_02
"""

import os
from typing import Optional

# Scope definitions per agent
_AGENT_SCOPES = {
    "GPT": [
        "read:assignments",
        "read:reviews",
        "write:assurance",
        "write:usage",
    ],
    "Claude": [
        "read:assignments",
        "read:reviews",
        "write:assignments",
        "write:usage",
        "write:assurance",
    ],
    "Skip": [
        "read:assignments",
        "read:reviews",
        "write:assignments",
        "write:assurance",
        "write:usage",
        "admin",
    ],
}


def _key_map() -> dict[str, str]:
    """Build key → agent map from environment variables."""
    keys = {}
    for agent in ("GPT", "Claude", "Skip"):
        key = os.getenv(f"BRAIN_KEY_{agent.upper()}")
        if key:
            keys[key] = agent
    return keys


def resolve_agent(bearer_token: str) -> Optional[str]:
    """
    Resolve a Bearer token to an agent name.
    Returns None if the token is not recognised.
    """
    return _key_map().get(bearer_token)


def has_scope(agent: str, scope: str) -> bool:
    """Return True if the agent has the requested scope."""
    return scope in _AGENT_SCOPES.get(agent, [])


def authenticate(bearer_token: str, required_scope: str) -> tuple[bool, str]:
    """
    Validate a Bearer token and check scope.

    Returns:
        (True, agent_name)  if valid and authorised
        (False, reason)     if invalid or unauthorised
    """
    agent = resolve_agent(bearer_token)
    if agent is None:
        return False, "Invalid or missing API key"
    if not has_scope(agent, required_scope):
        return False, f"Agent '{agent}' does not have scope '{required_scope}'"
    return True, agent


def get_gpt_key() -> str:
    """Return GPT's key — used when generating the OpenAPI spec for the Custom Action."""
    key = os.getenv("BRAIN_KEY_GPT")
    if not key:
        raise EnvironmentError("BRAIN_KEY_GPT is not set")
    return key
