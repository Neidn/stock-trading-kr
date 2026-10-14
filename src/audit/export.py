"""Export bot fills and equity for ai-skills ``trade-history-analysis``.

구조 규칙(종목당 상한·추가매수 금지·지정가 진입)이 코드에 있다고 지켜지는 것은 아니다.
봇 자신의 체결로 확인한다 (stock-trading-book-audit 3절 "구조 규칙 준수").

    python -m src.audit.export --days 90 --out out/
    python <ai-skills>/skills/trade-history-analysis/scripts/reconstruct_trades.py \
        out/fills.csv --format generic --equity out/equity.csv --out out/analysis/

출력 (generic 형식):
  fills.csv   time, symbol, side, qty, price, notional, fee, order_id, order_type, purpose
              fee = 수수료 + 거래세 (cost_source 가 estimated 면 추정치다)
  equity.csv  date, equity   (equity_snapshots — 장 마감 후 총평가금액)

봇이 내지 않은 주문(purpose='external')의 체결은 기본으로 뺀다. 봇의 행동을 보려는 것이다.
"""

from __future__ import annotations

import argparse
import csv
from datetime import timedelta
from pathlib import Path

from src.db.connection import db_conn
from src.utils.time_utils import KST, today_kst

FILL_COLUMNS = ("time", "symbol", "side", "qty", "price", "notional", "fee", "order_id",
                "order_type", "purpose")


def export(out_dir: Path, days: int, *, include_external: bool = False) -> dict[str, int]:
    start = today_kst() - timedelta(days=days)
    out_dir.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.executed_at, f.code, f.side, f.qty, f.price, f.amount, f.fee, f.tax,
                      f.order_no, COALESCE(o.order_type, '') AS order_type,
                      COALESCE(o.purpose, '') AS purpose
               FROM fills f LEFT JOIN orders o ON o.order_no = f.order_no
               WHERE (f.executed_at AT TIME ZONE 'Asia/Seoul')::date >= %s
                 AND (%s OR COALESCE(o.purpose, '') <> 'external')
               ORDER BY f.executed_at, f.id""",
            (start, include_external),
        )
        fills = cur.fetchall()
        cur.execute("SELECT trade_date, total_eval FROM equity_snapshots "
                    "WHERE trade_date >= %s ORDER BY trade_date", (start,))
        equity = cur.fetchall()

    with open(out_dir / "fills.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FILL_COLUMNS)
        for r in fills:
            w.writerow([
                r["executed_at"].astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
                r["code"], r["side"], r["qty"], r["price"], r["amount"],
                r["fee"] + r["tax"], r["order_no"], r["order_type"], r["purpose"],
            ])
    with open(out_dir / "equity.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("date", "equity"))
        for r in equity:
            w.writerow((r["trade_date"].isoformat(), r["total_eval"]))
    return {"fills": len(fills), "equity_days": len(equity)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", default="out")
    p.add_argument("--include-external", action="store_true",
                   help="봇이 내지 않은 주문(HTS·앱)의 체결도 포함")
    args = p.parse_args()
    n = export(Path(args.out), args.days, include_external=args.include_external)
    print(f"fills {n['fills']}건, equity {n['equity_days']}일 → {args.out}/")
    if not n["equity_days"]:
        print("equity.csv 가 비어 있다 — 노출 배수·%자산 지표는 장 마감 스냅샷이 쌓인 뒤에 나온다")


if __name__ == "__main__":
    main()
