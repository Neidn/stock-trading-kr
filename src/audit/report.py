"""Performance report following stock-trading-book-audit.

순서: 원장 확인 → 필드 신뢰성 → 그다음에야 성과. 이 리포트는 판정 전에 다음을 먼저 적는다.
  * 모의투자 여부 (모의 체결은 실전 근거가 아니다)
  * 비용 출처 (estimated = 수수료율·세율 테이블로 추정)
  * 세율 테이블 검증 여부
  * 체결 경로 비율 (reconcile 비율이 높으면 실시간 경로가 망가졌다)
그리고 성과는 **매매당 net**, 거래 수, **진입일 수**를 같이 쓴다. 표본이 부족하면 "판단 보류".

벤치마크(지수) 차감은 아직 구현하지 않았다 — 리포트에 그 사실을 적는다.

Usage:
    python -m src.audit.report --days 90
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from src.db.connection import db_conn
from src.utils.time_utils import today_kst

MIN_EDGE_N = 50       # 방향성 있는 엣지 판단
MIN_STRATEGY_N = 30   # 전략 켜기/끄기


def _rows(sql: str, args: tuple) -> list[dict]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def build_report(days: int, env: str) -> str:
    start = today_kst() - timedelta(days=days)
    out: list[str] = [f"# 성과 리포트 (최근 {days}일, {start} ~ {today_kst()})", ""]

    # ── 0. 전제 ───────────────────────────────────────────────────────────
    out.append("## 전제")
    if env == "demo":
        out.append("- **모의투자 데이터다. 실전 성과 근거로 쓰지 않는다 (배선 검증용).**")
    taxes = _rows("SELECT market, effective_from, sell_tax_rate, verified, checked_on "
                  "FROM tax_rates ORDER BY market, effective_from", ())
    for t in taxes:
        mark = "확인됨" if t["verified"] else "**미확인**"
        out.append(f"- 세율 {t['market']} {t['effective_from']}~: {float(t['sell_tax_rate']):.4%} "
                   f"({mark}, 조회일 {t['checked_on']})")
    srcs = _rows("SELECT source, COUNT(*) AS n FROM fills WHERE "
                 "(executed_at AT TIME ZONE 'Asia/Seoul')::date >= %s GROUP BY source", (start,))
    total_fills = sum(r["n"] for r in srcs)
    if total_fills:
        rec = sum(r["n"] for r in srcs if r["source"] == "reconcile")
        out.append(f"- 체결 {total_fills}건 중 대조 복구 {rec}건 ({rec / total_fills:.0%})")
    est = _rows("SELECT COUNT(*) AS n FROM trades WHERE exit_at >= %s AND cost_source = 'estimated'",
                (start,))[0]["n"]
    if est:
        out.append(f"- 거래 {est}건의 수수료·세금은 **추정치**(cost_source=estimated). "
                   "증권사 기간별매매손익과 대조 전에는 확정 손익이 아니다.")
    out.append("- 벤치마크(KOSPI/KOSDAQ) 차감: **미구현** — 롱 온리 절대수익은 대부분 베타일 수 있다.")
    out.append("")

    # ── 1. 전략별 매매당 net ──────────────────────────────────────────────
    out.append("## 전략별")
    rows = _rows(
        """SELECT COALESCE(strategy_name, '(none)') AS strategy,
                  COUNT(*) AS n,
                  COUNT(DISTINCT (entry_at AT TIME ZONE 'Asia/Seoul')::date) AS entry_days,
                  SUM(gross_pnl) AS gross, SUM(fee + tax) AS cost, SUM(net_pnl) AS net,
                  ROUND(AVG(net_pnl)) AS per_trade_net,
                  SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN gross_pnl > 0 THEN gross_pnl ELSE 0 END) AS gross_wins,
                  ROUND(AVG(realized_r), 2) AS avg_r,
                  SUM(CASE WHEN realized_r < -1.0 THEN 1 ELSE 0 END) AS beyond_1r
           FROM trades WHERE exit_at >= %s
           GROUP BY 1 ORDER BY per_trade_net DESC""",
        (start,),
    )
    if not rows:
        out.append("- 청산된 거래 없음")
    for r in rows:
        n = r["n"]
        cost_pct = (f"{r['cost'] / r['gross_wins']:.0%}" if r["gross_wins"] else "n/a")
        verdict = ("판단 보류 (N < %d)" % MIN_STRATEGY_N) if n < MIN_STRATEGY_N else (
            "엣지 판단 보류 (N < %d)" % MIN_EDGE_N if n < MIN_EDGE_N else "표본 충분 — 레짐·벤치마크 확인 후 판정")
        out.append(
            f"- **{r['strategy']}**: N={n}, 진입일 {r['entry_days']}일, 매매당 net {int(r['per_trade_net'] or 0):,}원, "
            f"총 net {int(r['net']):,} (gross {int(r['gross']):,} − 비용 {int(r['cost']):,}), "
            f"승률 {r['wins'] / n:.0%}, 평균 R {r['avg_r']}, −1R 초과 손실 {r['beyond_1r']}건, "
            f"비용/이익거래 gross {cost_pct} → {verdict}"
        )
    out.append("")

    # ── 2. 시장별 · 청산사유별 (상쇄되어 안 보이는 것 분리) ────────────────
    out.append("## 시장별 · 청산사유별")
    for col in ("market", "close_reason"):
        for r in _rows(f"SELECT COALESCE({col}, '?') AS k, COUNT(*) AS n, ROUND(AVG(net_pnl)) AS avg "
                       f"FROM trades WHERE exit_at >= %s GROUP BY 1 ORDER BY 1", (start,)):
            out.append(f"- {col}={r['k']}: N={r['n']}, 매매당 net {int(r['avg'] or 0):,}원")
    out.append("")

    # ── 3. 갭 꼬리 ────────────────────────────────────────────────────────
    tail = _rows("SELECT code, exit_at, realized_r, close_reason FROM trades "
                 "WHERE exit_at >= %s AND realized_r < -1.0 ORDER BY realized_r LIMIT 10", (start,))
    if tail:
        out.append("## −1R 보다 큰 손실 (손절이 갭·VI·하한가·프로세스 다운을 못 막은 신호)")
        for r in tail:
            out.append(f"- {r['code']} {r['exit_at']:%Y-%m-%d} R={r['realized_r']} ({r['close_reason']})")
        out.append("")

    out.append("_이 리포트는 판정이 아니라 판정 재료다. 추정치와 실측치를 구분해서 읽는다._")
    return "\n".join(out)


def main() -> None:
    import os

    from src.monitoring.telegram import Notifier
    from src.utils.logger import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    env = os.environ.get("KIS_ENV", "demo")
    text = build_report(args.days, env)
    print(text)
    if args.telegram:
        Notifier(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""),
                 prefix="[모의] " if env == "demo" else "[실전] ").send(text)


if __name__ == "__main__":
    main()
