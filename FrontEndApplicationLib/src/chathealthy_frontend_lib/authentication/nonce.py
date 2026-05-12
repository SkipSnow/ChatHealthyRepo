# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from datetime import datetime, timezone


class Nonce:
    STAMP_SIZE = 17
    SEPARATOR = 'X'
    SIZE = STAMP_SIZE + 1 + STAMP_SIZE
    _STRFTIME = '%Y%m%d%H%M%S'

    @classmethod
    def fresh(cls) -> str:
        s = cls._now_stamp()
        return f"{s}{cls.SEPARATOR}{s}"

    @classmethod
    def restamp(cls, field: str) -> str:
        cls._validate(field)
        original = field[:cls.STAMP_SIZE]
        return f"{original}{cls.SEPARATOR}{cls._now_stamp()}"

    @classmethod
    def latest_stamp(cls, field: str) -> str:
        cls._validate(field)
        return field[cls.STAMP_SIZE + 1:]

    @classmethod
    def original_stamp(cls, field: str) -> str:
        cls._validate(field)
        return field[:cls.STAMP_SIZE]

    @classmethod
    def _now_stamp(cls) -> str:
        now = datetime.now(timezone.utc)
        ms = str(now.microsecond)[:3].zfill(3)
        return now.strftime(cls._STRFTIME) + ms

    @classmethod
    def _validate(cls, field: str) -> None:
        if len(field) != cls.SIZE:
            raise ValueError(f"nonce length {len(field)} != {cls.SIZE}")
        if field[cls.STAMP_SIZE] != cls.SEPARATOR:
            raise ValueError(f"missing '{cls.SEPARATOR}' separator at byte {cls.STAMP_SIZE}")
