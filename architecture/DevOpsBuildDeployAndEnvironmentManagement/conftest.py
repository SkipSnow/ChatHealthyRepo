# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Failures are written out as they happen. The hook lives here because
# pytest only calls runtest hooks from a conftest.

import pytest

from find_care_windows_uat_test import VIEWPORT, _triage


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when in ("setup", "call") and report.failed:
        _triage(item.name, VIEWPORT,
                report.longreprtext or str(report.longrepr))
