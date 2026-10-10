"""KST time helpers. 거래일·세션 판단은 항상 KST 기준이다 (UTC 로 자르면 09:00 이 날짜 경계가 된다)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> date:
    return now_kst().date()


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def combine_kst(d: date, hhmmss: str) -> datetime:
    """'20261009' 날짜 + 'HHMMSS' 시각 → aware KST datetime."""
    return datetime(d.year, d.month, d.day,
                    int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=KST)
