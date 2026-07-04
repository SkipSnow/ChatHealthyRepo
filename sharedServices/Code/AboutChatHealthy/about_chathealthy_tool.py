# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""AboutChatHealthy tool — surfaces the About-popup payload.

POC consumer of the new client-side architecture. Pydantic In + Pydantic
Out. The tool returns STRUCTURED DATA only — no HTML, no display logic.
The React widget renders the HTML and hands it to ClientRouter.render.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

from chathealthy_frontend_lib.authentication.agent_deps import AgentDeps
from chathealthy_frontend_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


BUILD_INFO_PATH = "/app/build_info.json"

COMPANY_BLURB = (
    "Chat Healthy.ai LLC, is a Delaware corporation, that was founded "
    "on March 26th 2026. The current plan is to go live in production "
    "in December 2026."
)


class Request(BaseModel):
    pass


class BuildFacts(BaseModel):
    version: Optional[str] = None
    build: Optional[int] = None
    commit: Optional[str] = None
    built_at: Optional[str] = None
    env: Optional[str] = None


class SecurityPanelData(BaseModel):
    server: Optional[str] = None
    env: Optional[str] = None
    # 'signed_token' is rendered by the widget as "Authorization ID".
    signed_token: Optional[str] = None
    nonce: Optional[str] = None
    # GUID is the canonical session GUID off the SessionToken
    # (cst.get_auth_token()); the widget labels this "Session GUID".
    guid: Optional[str] = None
    verified: Optional[str] = None
    time: Optional[str] = None


class Response(BaseModel):
    company_text: str = ""
    build_facts: BuildFacts = Field(default_factory=BuildFacts)
    security_panel: SecurityPanelData = Field(default_factory=SecurityPanelData)


def _read_build_info() -> dict:
    p = Path(BUILD_INFO_PATH)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.info(
            "build_info read failed (about_chathealthy): %s", exc,
            exc=ChatHealthyException(
                mode="about_build_info_read_failed",
                message=f"build_info read failed: {exc}",
                component="AboutChatHealthyTool",
                exception=exc,
            ),
        )
        return {}


def _build_facts(env_prefix: str) -> BuildFacts:
    info = _read_build_info()
    build_raw = info.get("build")
    build = int(build_raw) if isinstance(build_raw, int) else None
    return BuildFacts(
        version=info.get("version"),
        build=build,
        commit=info.get("commit"),
        built_at=info.get("built_at"),
        env=env_prefix,
    )


def _security_panel(deps: AgentDeps) -> SecurityPanelData:
    user_obj = getattr(deps, "user_object", None)
    sess = getattr(user_obj, "current_session_token", None) if user_obj else None
    if not sess or not hasattr(sess, "token"):
        return SecurityPanelData(
            server="SharedServices",
            env=getattr(deps, "server_env", None),
            verified="pending",
        )
    # Use canonical getters from SessionToken instead of slicing the
    # Python repr (the previous code did `str(sess)` which returned the
    # Pydantic repr like "origin='SharedServices' token='CH...' ..." and
    # slicing that produced garbage like "igin='SharedServi"). The
    # SessionToken class exposes get_nonce()/get_auth_token() that read
    # the canonical positions on the underlying token string.
    token_str = getattr(sess, "token", "") or ""
    try:
        nonce_val = sess.get_nonce() if callable(getattr(sess, "get_nonce", None)) else None
    except Exception:
        nonce_val = None
    try:
        guid_val = sess.get_auth_token() if callable(getattr(sess, "get_auth_token", None)) else None
    except Exception:
        guid_val = None
    return SecurityPanelData(
        server="SharedServices",
        env=getattr(deps, "server_env", None),
        signed_token=(token_str[:60] + "...") if len(token_str) > 60 else token_str,
        nonce=nonce_val,
        guid=guid_val,
        verified="verified",
    )


class AboutChatHealthyTool(ChatHealthyTool):
    TOOL_NAME = "about_chathealthy"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        env_prefix = getattr(deps, "server_env", None) or os.getenv("ENV_PREFIX", "")
        resp = Response(
            company_text=COMPANY_BLURB,
            build_facts=_build_facts(env_prefix),
            security_panel=_security_panel(deps),
        )
        deps.stream({"kind": "about_chathealthy", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = AboutChatHealthyTool()
