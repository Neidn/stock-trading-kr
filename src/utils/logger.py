"""Logging setup with secret/account masking.

로그에 계좌번호·앱키·토큰이 섞여 들어가지 않게 필터로 가린다. 필터는 마지막
방어선이다 — 애초에 로그 메시지에 넣지 않는 것이 원칙이다.
"""

from __future__ import annotations

import logging
import os
import re

_SECRET_ENV = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "TELEGRAM_BOT_TOKEN")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+")


def mask_account(account_no: str) -> str:
    """'12345678' → '12****78'."""
    if len(account_no) <= 4:
        return "*" * len(account_no)
    return account_no[:2] + "*" * (len(account_no) - 4) + account_no[-2:]


def scrub(text: str) -> str:
    text = _BEARER.sub(r"\1***", text)
    for name in _SECRET_ENV:
        val = os.environ.get(name, "")
        if len(val) >= 6:
            replacement = mask_account(val) if name == "KIS_ACCOUNT_NO" else "***"
            text = text.replace(val, replacement)
    return text


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(MaskingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    # urllib3 는 DEBUG 에서 헤더를 찍을 수 있다.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
