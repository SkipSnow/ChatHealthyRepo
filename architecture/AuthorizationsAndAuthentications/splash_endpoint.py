# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""SplashTool — pydantic-contracted tool for the SharedServices splash.

Renders the User Object identity + the 3-actor / 4-thread conversation
history into HTML, and emits a ux_event capturing this splash invocation.
"""

import html as _html
import json as _json
import logging
from typing import ClassVar, Type

from authentication.session_conversation_history import ux_event_append
from authentication.tool_framework import (
    Tool,
    ToolRequest,
    ToolResponse,
)


class SplashRequest(ToolRequest):
    """Splash takes no payload."""


class SplashResponse(ToolResponse):
    pass


class SplashTool(Tool):
    tool_name: ClassVar[str] = "splash"
    request_model: ClassVar[Type[ToolRequest]] = SplashRequest
    response_model: ClassVar[Type[ToolResponse]] = SplashResponse

    _PEDANTIC_NOTE = "SharedServices took ownership of the page and rendered the User Object."

    def __init__(self):
        self.log = logging.getLogger("shared_services.splash")

    def execute(self, session_token, user_object, request):
        self.log.info("CONTROL TRANSFER: SharedServices has taken ownership of the page")
        # Compose the identity dict for rendering from the UserObject's
        # current_session_token + a few computed/injected display fields.
        # Order matters for the render: user_type first, GUID second, etc.
        cst = user_object.current_session_token
        identity = {
            "user_type": "Guest",
            "guid": cst.get_auth_token(),
            "origin": cst.origin,
            "server_env": cst.server_env,
            "created_at": cst.created_at,
            "expires_at": user_object.expires_at,
        }
        sch = user_object.session_conversation_history.model_dump()
        return SplashResponse(
            result={"html": self._render(identity, sch)},
            history_append=ux_event_append(
                "splash_displayed",
                value=None,
                pedantic_response={"text": self._PEDANTIC_NOTE},
            ),
        )

    @staticmethod
    def _missing_user_object_html() -> str:
        return (
            '<div style="text-align:center;padding:20px;">'
            '<div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div>'
            '<div style="font-size:16px;font-weight:600;color:#b91c1c;margin-top:8px;">'
            'No User Object resolved by the gate.'
            '</div></div>'
        )

    @classmethod
    def _render(cls, identity: dict, sch: dict | None = None) -> str:
        if not isinstance(sch, dict):
            sch = {}
        display = identity
        return (
            '<div style="padding:20px;max-width:820px;margin:0 auto;text-align:left;'
            'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">'
            '<h2 style="margin:0 0 8px 0;color:#0b7a75;font-size:22px;">'
            'Shared Services &mdash; User Object'
            '</h2>'
            '<p style="color:#6b7280;margin:0 0 12px 0;font-size:13px;">'
            'Live evidence that the entrance code completed. The cookie carries only the GUID; '
            'the user object lives in <code>admin.Sessions</code>.'
            '</p>'
            + cls._render_identity(display)
            + cls._render_history(sch)
            + '</div>'
        )

    @classmethod
    def _render_identity(cls, display: dict) -> str:
        rows: list[str] = []
        for key in ("user_type", "guid", "origin", "server_env", "created_at", "expires_at"):
            if key in display:
                rows.append(cls._kv_row(key, display[key]))
        # Anything else (excluding the raw token/signature bytes) goes
        # at the end. Defensive — `identity` dict is clean by construction.
        for key, value in display.items():
            if key in ("user_type", "guid", "origin", "server_env", "created_at",
                       "expires_at", "token", "signature"):
                continue
            rows.append(cls._kv_row(key, value))
        return (
            '<h3 style="margin:16px 0 6px 0;color:#0b7a75;font-size:15px;">Identity</h3>'
            '<dl style="margin:0;display:grid;grid-template-columns:160px 1fr;gap:4px 12px;">'
            + ''.join(rows) +
            '</dl>'
        )

    @staticmethod
    def _kv_row(key: str, value) -> str:
        if isinstance(value, (dict, list)):
            v = _json.dumps(value, indent=2, ensure_ascii=False, default=str)
        else:
            v = str(value)
        return (
            f'<dt style="font-weight:600;color:#374151;">{_html.escape(str(key))}</dt>'
            f'<dd style="margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;'
            f'font-size:12px;color:#1f2937;white-space:pre-wrap;word-break:break-all;">'
            f'{_html.escape(v)}'
            '</dd>'
        )

    @classmethod
    def _render_history(cls, sch: dict) -> str:
        """Render the conversation history as four parallel scroll-bar
        threads, categorized by actor (Skip-agreed design 2026-05-14):

          Thread 1 — Person          (UX clicks + typed utterances)
          Thread 2 — Machine         (pedantic responses + tool results)
          Thread 3 — LLM → Person    (clarifications)
          Thread 4 — LLM → Machine   (tool invocations)

        Three actors (Person / Machine / LLM); the LLM splits into two
        threads because it has two distinct outbound surfaces.

        Each thread is its own fixed-height scrollable div; small font.
        """
        ux_events = sch.get("ux_events") if isinstance(sch.get("ux_events"), list) else []
        utterances = sch.get("utterances") if isinstance(sch.get("utterances"), list) else []
        empty = (not ux_events) and (not utterances)
        intro = (
            '<h3 style="margin:14px 0 4px 0;color:#0b7a75;font-size:12px;">'
            'Session Conversation History'
            '</h3>'
        )
        if empty:
            return intro + (
                '<p style="margin:0;color:#9ca3af;font-size:10px;font-style:italic;">'
                'No UX events or utterances yet. Click around or type a prompt to see this grow.'
                '</p>'
            )

        person_items = cls._collect_person(ux_events, utterances)
        machine_items = cls._collect_machine(ux_events, utterances)
        llm_to_person = cls._collect_llm_to_person(utterances)
        llm_to_machine = cls._collect_llm_to_machine(utterances)

        threads_grid = (
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">'
            + cls._render_thread("Person", "#0b7a75", "#f0fffe", person_items)
            + cls._render_thread("Machine", "#0b7a75", "#e6fffd", machine_items)
            + cls._render_thread("LLM → Person", "#d97706", "#fff7ed", llm_to_person)
            + cls._render_thread("LLM → Machine", "#d97706", "#fef3c7", llm_to_machine)
            + '</div>'
        )
        return intro + threads_grid

    @staticmethod
    def _collect_person(ux_events: list, utterances: list) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for e in ux_events:
            if not isinstance(e, dict):
                continue
            at = str(e.get("at", ""))
            event_type = str(e.get("event_type", "?"))
            value = e.get("value")
            tail = ""
            if value is not None and value != "":
                v = _json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
                tail = f" · {v[:90]}"
            out.append((at, f"<strong>{_html.escape(event_type)}</strong>{_html.escape(tail)}"))
        for u in utterances:
            if not isinstance(u, dict):
                continue
            at = str(u.get("at", ""))
            text = str(u.get("text", ""))
            out.append((at, f'"{_html.escape(text)}"'))
        out.sort(key=lambda r: r[0])
        return out

    @staticmethod
    def _collect_machine(ux_events: list, utterances: list) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for e in ux_events:
            if not isinstance(e, dict):
                continue
            ped = e.get("pedantic_response")
            if not ped:
                continue
            at = str(e.get("at", ""))
            text = ped.get("text") if isinstance(ped, dict) else str(ped)
            out.append((at, _html.escape(str(text or "(empty pedantic response)"))))
        for u in utterances:
            if not isinstance(u, dict):
                continue
            br = u.get("bridge_response") or {}
            if isinstance(br, dict) and br.get("kind") == "tool_invocation":
                at = str(u.get("at", ""))
                tool_name = str(br.get("tool_name", "?"))
                tool_result = br.get("tool_result")
                tail = ""
                if tool_result is not None:
                    tail = " → " + _json.dumps(tool_result, default=str)[:120]
                out.append((at, f"tool_result from <strong>{_html.escape(tool_name)}</strong>{_html.escape(tail)}"))
        out.sort(key=lambda r: r[0])
        return out

    @staticmethod
    def _collect_llm_to_person(utterances: list) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for u in utterances:
            if not isinstance(u, dict):
                continue
            br = u.get("bridge_response") or {}
            if isinstance(br, dict) and br.get("kind") == "llm_clarification":
                at = str(u.get("at", ""))
                llm_text = str(br.get("llm_response", ""))
                info = br.get("info_sought")
                tail = ""
                if isinstance(info, list) and info:
                    tail = " · seeking: " + ", ".join(map(str, info))
                out.append((at, _html.escape(llm_text + tail)))
        out.sort(key=lambda r: r[0])
        return out

    @staticmethod
    def _collect_llm_to_machine(utterances: list) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for u in utterances:
            if not isinstance(u, dict):
                continue
            br = u.get("bridge_response") or {}
            if isinstance(br, dict) and br.get("kind") == "tool_invocation":
                at = str(u.get("at", ""))
                tool_name = str(br.get("tool_name", "?"))
                tool_args = br.get("tool_args")
                tail = ""
                if tool_args is not None:
                    tail = "(" + _json.dumps(tool_args, default=str)[:120] + ")"
                out.append((at, f"invoke <strong>{_html.escape(tool_name)}</strong>{_html.escape(tail)}"))
        out.sort(key=lambda r: r[0])
        return out

    @staticmethod
    def _render_thread(title: str, color: str, bg: str, items: list[tuple[str, str]]) -> str:
        if not items:
            body = (
                '<div style="padding:4px 0;color:#9ca3af;font-size:9px;font-style:italic;">'
                '(no entries yet)</div>'
            )
        else:
            rows = []
            for i, (at, body_html) in enumerate(items):
                rows.append(
                    f'<div style="padding:3px 0;border-top:{"1px solid #d1fae5" if i else "none"};">'
                    f'<div style="font-size:9px;color:#6b7280;line-height:1.1;">{_html.escape(at)}</div>'
                    f'<div style="font-size:10px;color:#1f2937;line-height:1.2;">{body_html}</div>'
                    '</div>'
                )
            body = ''.join(rows)
        return (
            f'<div style="border-left:2px solid {color};background:{bg};padding:5px 6px;">'
            f'<div style="font-weight:600;color:{color};font-size:10px;margin-bottom:3px;text-transform:uppercase;letter-spacing:.4px;">'
            f'{title}'
            '</div>'
            f'<div style="max-height:140px;overflow-y:auto;padding-right:3px;">'
            f'{body}'
            '</div>'
            '</div>'
        )
