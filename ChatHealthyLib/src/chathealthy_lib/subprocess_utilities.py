# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical subprocess spawn helpers for ChatHealthy.ai runtime code.

Every long-lived child process the platform spawns MUST be fully detached
from the parent: no shared stdio pipes, no process-group tie, no PID
handle held by the parent. Communication is exclusively via Mongo (WI
heartbeat_at, work_items status). This module is the ONLY sanctioned
spawn surface — do not call subprocess.Popen(...) with stdout=PIPE or
stderr=PIPE for any long-lived worker anywhere in the codebase.

Why: subprocess.Popen with stdout=PIPE / stderr=PIPE creates a coupled
relationship. Parent holds the kernel pipe FD, child writes into it. If
parent never drains, the kernel buffer (typically 64KB on Linux) fills
and the child blocks on the next write. Small workers escape by exiting
before hitting the cap. Big workers (that log a lot on startup —
pymongo debug, azure IMDS, urllib3 retries) deadlock forever, upstream
of any diagnostic point. This module exists so we never write that
pattern again.
"""
from __future__ import annotations

import os

from .exceptions import ChatHealthyException


def _require_posix_for_fork() -> None:
    """Raise-bearing pre-check. Rule-005 keeps raise separate from log."""
    if not hasattr(os, "fork"):
        raise ChatHealthyException(
            mode="platform_unsupported",
            message=(
                "spawn_detached_worker requires POSIX os.fork(). This is "
                "expected inside Docker containers on Linux; not supported "
                "on Windows. Use an in-process runner for local Windows "
                "dev instead."
            ),
            component="chathealthy_lib.subprocess_utilities",
        )


def spawn_detached_worker(argv: list[str], env: dict) -> int:
    """Fully detach a child process from the caller.

    Uses classic double-fork + setsid so the grandchild is reparented to
    init (PID 1). Caller keeps NO handle on the grandchild:
        - no stdio pipes (all three go to /dev/null)
        - no process-group tie (grandchild is a new session leader)
        - no waitable PID at the caller level (init reaps)

    Returns the grandchild's PID for informational/log purposes only.
    Callers MUST NOT use it for wait(), poll(), kill(), or any status
    check. Monitor the child exclusively via Mongo (heartbeat_at on the
    work_items record, or equivalent per-workload signal).

    Args:
        argv: exec-able command line. argv[0] is the executable; the
            os.execvpe() resolves it against PATH.
        env: environment dict passed verbatim to the child. Include
            CH_LOG_DESTINATION and MONGO_FRONTEND_connectionString if
            the child uses ChatHealthyLoggingService to write to Mongo.

    Raises:
        ChatHealthyException(mode="platform_unsupported") on Windows
            (os.fork() is POSIX-only).
    """
    _require_posix_for_fork()
    r, w = os.pipe()
    first_pid = os.fork()
    if first_pid > 0:
        # Original parent: wait for intermediate to exit fast (only forks
        # and reports), then read the grandchild PID.
        os.close(w)
        os.waitpid(first_pid, 0)
        try:
            gpid_bytes = os.read(r, 32)
        finally:
            os.close(r)
        return int(gpid_bytes.decode().strip() or "0")
    # Intermediate child: new session, fork the grandchild, report PID,
    # exit so grandchild reparents to init.
    try:
        os.close(r)
        os.setsid()
        grand_pid = os.fork()
        if grand_pid > 0:
            os.write(w, str(grand_pid).encode())
            os.close(w)
            os._exit(0)
        # Grandchild: swap stdio to /dev/null, exec.
        os.close(w)
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull_fd, 0)
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        if devnull_fd > 2:
            os.close(devnull_fd)
        os.execvpe(argv[0], argv, env)
    except Exception:  # noqa: BLE001 — anything reaching here means the
        os._exit(255)     # grandchild never became the child target.
    return 0  # unreachable; satisfies type checker
