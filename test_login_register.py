# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Playwright E2E test for EPIC-002-F-003-S-004 - Create and login
Users With Google OAuth.

STUB: the real test will be authored jointly with the operator after
the deployed flow is verified end-to-end against dev. Every REQ in
S-004 (B-001, B-002, T-001..T-012) points at this file.
"""
import pytest
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


@pytest.mark.skip(reason="Stub - real Playwright test deferred to joint authoring with the operator")
def test_login_register_sequence_stub() -> None:
    raise ChatHealthyException(
        mode="not_implemented",
        component="test_login_register",
        message="Real Playwright E2E for EPIC-002-F-003-S-004 not yet authored.")
