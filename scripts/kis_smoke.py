"""Read-only KIS connectivity check. 주문을 내지 않는다.

모의투자 앱키로 토큰 → 현재가 → 일봉 → 잔고 → 매수가능금액 → 당일 주문 → 휴장일을
한 번씩 호출하고 결과 요약만 출력한다 (계좌번호·토큰은 출력하지 않는다).
README "미확인" 목록 중 모의에서 무엇이 되는지를 이 출력으로 확인한다.

  * 휴장일 실패는 모의에서 예상된 결과다 (공식 샘플에 모의 TR 없음). 엔진은 장 시작 후
    일봉에 오늘 봉이 생기는지로 개장을 확인한다 → "일봉" 줄의 today_bar 를 본다.
    장중에 실행해야 today_bar 가 의미 있다.

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
from src.utils.time_utils import today_kst, ymd


def main() -> None:
    setup_logging()
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    cfg = load_kis()
    migrate()
    broker = KisBroker(KisClient(cfg, PgTokenStore()))
    print(f"env={cfg.env} base_url={cfg.base_url} rps={cfg.rps}")

    client = broker.c

    def candles():
        df = broker.daily_candles(code, 5)
        last = str(df["date"].iloc[-1]) if not df.empty else "-"
        return f"{len(df)}봉, 마지막 {last}, today_bar={last == ymd(today_kst())}"

    def balance():
        hs, s = broker.holdings()
        return f"{len(hs)}종목, 총평가 {s.total_eval:,}, 예수금 {s.deposit:,}"

    def holiday():
        v = broker.is_market_open_day(today_kst())
        if v is None:
            raise RuntimeError("응답 없음/거부 — 모의 미지원으로 추정 (엔진은 일봉으로 대체 확인)")
        return f"개장일={v}"

    # (이름, 호출, 엔진 운영에 필수인가)
    steps = [
        ("토큰", lambda: f"만료 {client._token_exp:%Y-%m-%d %H:%M}" if client.token() else "-", True),
        ("현재가", lambda: broker.quote(code), True),
        ("일봉", candles, True),
        ("잔고", balance, True),
        ("매수가능", lambda: f"{broker.buyable_cash(code, broker.quote(code).price):,}원", True),
        ("당일주문", lambda: f"{len(broker.daily_orders(today_kst()))}건", True),
        ("휴장일", holiday, cfg.is_real),
    ]
    failed = 0
    for name, fn, required in steps:
        try:
            print(f"[OK]   {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 — 진단용 스크립트
            failed += required
            tag = "FAIL" if required else "WARN"
            print(f"[{tag}] {name}: {type(exc).__name__}: {exc}")
            if name == "토큰":
                # 나머지 단계가 토큰 발급을 반복하지 않게 멈춘다 (발급마다 알림톡이 간다).
                print("토큰 발급 실패 — 앱키·시크릿이 모의투자용인지, KIS_ENV=demo 인지 확인")
                break
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
