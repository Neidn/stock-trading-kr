"""Read-only KIS connectivity check. 주문을 내지 않는다.

모의투자 앱키로 토큰 발급 → 휴장일 → 현재가 → 잔고 → 매수가능금액 → 당일 주문 조회를
한 번씩 호출하고 결과 요약만 출력한다 (계좌번호·토큰은 출력하지 않는다).

    DATABASE_URL=... KIS_APP_KEY=... KIS_APP_SECRET=... KIS_ACCOUNT_NO=... FEE_RATE=... \
        python -m scripts.kis_smoke 005930
"""

from __future__ import annotations

import sys

from src.broker.kis_broker import KisBroker
from src.broker.kis_client import KisClient
from src.broker.token_store import PgTokenStore
from src.db.migrate import migrate
from src.utils.config import load_kis
from src.utils.logger import setup_logging
from src.utils.time_utils import today_kst


def main() -> None:
    setup_logging()
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    cfg = load_kis()
    migrate()
    broker = KisBroker(KisClient(cfg, PgTokenStore()))
    print(f"env={cfg.env} base_url={cfg.base_url} rps={cfg.rps}")

    steps = [
        ("휴장일", lambda: broker.is_market_open_day(today_kst())),
        ("현재가", lambda: broker.quote(code)),
        ("잔고", lambda: (len(broker.holdings()[0]), "종목")),
        ("매수가능", lambda: broker.buyable_cash(code, broker.quote(code).price)),
        ("당일주문", lambda: (len(broker.daily_orders(today_kst())), "건")),
    ]
    failed = 0
    for name, fn in steps:
        try:
            print(f"[OK]   {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 — 진단용 스크립트
            failed += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
