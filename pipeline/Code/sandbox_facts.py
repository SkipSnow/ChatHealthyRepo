"""What the Automation sandbox is, in its own words.

Three rounds of wheels were declared for this account on the strength of what
Azure's job errors said, and the errors contradicted themselves: a wheel
carrying manylinux2014_x86_64 -- a tag Azure named as supported -- was refused
as "not a supported wheel on this platform". Inference from error text has run
out.

This writes what the sandbox is: the interpreter, the platform, and the wheel
tags its own pip will accept. It reads nothing and holds no credential, and it
writes to stdout rather than through the logging service -- that service needs
Mongo, and Mongo needs the very packages this exists to explain.

It is declared in deployment_architecture.json so it reaches Azure through the
chain, and it comes out the same way once it has answered.
"""
from __future__ import annotations

import platform
import sys
import sysconfig


_NL = chr(10)


def _say(line: str) -> None:
    """Automation keeps the Output stream on a job that succeeds and discards
    stderr; it keeps stderr only inside an exception. Writing to stdout is
    what a completed job comes back with.

    This imports nothing of ours, and must not name the shared library even
    in a comment: the build inlines that library into any runbook whose text
    contains its name, and what it inlines needs the very packages this
    exists to explain. The probe carries no import that could fail before it
    answers, and no word that would give it one.
    """
    sys.stdout.write(line + _NL)
    sys.stdout.flush()


def main() -> int:
    _say(f"python          : {sys.version.replace(_NL, ' ')}")
    _say(f"executable      : {sys.executable}")
    _say(f"machine         : {platform.machine()}")
    _say(f"platform        : {platform.platform()}")
    _say(f"sysconfig       : {sysconfig.get_platform()}")
    _say(f"64-bit          : {sys.maxsize > 2 ** 32}")

    try:
        import pip
        _say(f"pip version     : {pip.__version__}")
    except Exception as exc:  # noqa: BLE001
        _say(f"pip version     : unavailable: {exc}")

    try:
        from pip._internal.utils.compatibility_tags import get_supported
        tags = [str(t) for t in get_supported()]
        _say(f"supported tags  : {len(tags)}")
        for tag in tags:
            if "manylinux" in tag or "win" in tag or "none-any" in tag:
                _say(f"    {tag}")
    except Exception as exc:  # noqa: BLE001
        _say(f"supported tags  : unavailable: {exc}")

    _say("already importable:")
    for name in ("pymongo", "cffi", "cryptography", "httpx", "requests",
                 "dns", "certifi"):
        try:
            module = __import__(name)
            _say(f"    {name:14s} {getattr(module, '__version__', '?')}")
        except Exception as exc:  # noqa: BLE001
            _say(f"    {name:14s} not importable: {type(exc).__name__}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
