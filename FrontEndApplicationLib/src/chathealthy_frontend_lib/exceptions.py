# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ChatHealthyException — the single deliberate-exception type for ChatHealthy.ai
authored code.

Realizes EPIC-003-F-003-S-001-REQ-B-001. Inherits from Exception (not
BaseException) so standard `except Exception:` blocks catch it.
"""
from __future__ import annotations

import traceback as _traceback


class ChatHealthyException(Exception):
    def __init__(self, mode: str, message: str, **context):
        super().__init__(message)
        self.mode: str = mode
        self.message: str = message
        # First-class network/architecture identity of the THROW site.
        # The server is the deployed process (e.g. 'shared_services',
        # 'find_care'); the component is the logical owner inside that
        # process (e.g. 'UM', 'SpecialtyFilter', 'UR'). Catching code can
        # always read these without parsing the context dict.
        self.server: str | None = context.pop("server", None)
        self.component: str | None = context.pop("component", None)
        self.exception: Exception | None = context.pop("exception", None)
        self.context: dict = dict(context)
        # Caller's stack captured at construction time, excluding this
        # __init__ frame. Used by ChatHealthyLoggingService to render the
        # destination's traceback when the exception is logged-only (never
        # raised — so Python has not attached its own __traceback__).
        self.construction_stack: str = "".join(_traceback.format_stack()[:-1])

    def __repr__(self) -> str:
        head = (f"ChatHealthyException(mode={self.mode!r}, "
                f"message={self.message!r}")
        if self.server is not None:
            head += f", server={self.server!r}"
        if self.component is not None:
            head += f", component={self.component!r}"
        ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        if ctx:
            head += ", " + ctx
        if self.exception is not None:
            head += f", exception={self.exception!r}"
        return head + ")"
