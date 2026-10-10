"""Operator CLI (kubectl exec 로 실행).

    python -m src.cli status                         # 킬 스위치·잠금·포지션·open 주문
    python -m src.cli kill 2 --reason "점검"          # 킬 스위치 단계 설정 (0 = 해제)
    python -m src.cli unlock 005930                  # 종목 잠금 해제 (원인 확인 후)
    python -m src.cli adopt 005930 --qty 10 --avg 70000 --stop 65000

주문을 직접 내는 명령은 없다. 킬 스위치 3단계(전량 청산)는 엔진이 실행한다.
"""

from __future__ import annotations

import argparse
import getpass

from src.execution.store import Store
from src.risk.kill_switch import DESCRIPTIONS, KillSwitch


def main() -> None:
    p = argparse.ArgumentParser(prog="src.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    k = sub.add_parser("kill")
    k.add_argument("level", type=int, choices=sorted(DESCRIPTIONS))
    k.add_argument("--reason", required=True)
    u = sub.add_parser("unlock")
    u.add_argument("code")
    a = sub.add_parser("adopt", help="증권사 잔고를 봇 포지션으로 편입 (손절 감시 대상이 된다)")
    a.add_argument("code")
    a.add_argument("--qty", type=int, required=True)
    a.add_argument("--avg", type=float, required=True)
    a.add_argument("--stop", type=int, required=True)
    args = p.parse_args()

    store = Store()
    if args.cmd == "status":
        level, reason = store.kill_level()
        print(f"kill switch: {level} — {DESCRIPTIONS[level]} ({reason})")
        for code, why in store.locked_symbols().items():
            print(f"locked {code}: {why}")
        for code, pos in store.positions().items():
            print(f"position {code}: qty={pos['qty']} avg={pos['avg_price']} stop={pos['stop_price']} "
                  f"tp={pos['take_profit']} strategy={pos['strategy_name']}")
        for o in store.open_orders():
            print(f"open {o['state']} {o['code']} {o['side']} {o['qty']}@{o['price']} "
                  f"filled={o['filled_qty']} no={o['order_no']} purpose={o['purpose']}")
    elif args.cmd == "kill":
        KillSwitch(store).set(args.level, args.reason, f"cli:{getpass.getuser()}")
        print(f"kill switch → {args.level}: {DESCRIPTIONS[args.level]}")
    elif args.cmd == "unlock":
        store.unlock_symbol(args.code)
        print(f"unlocked {args.code}")
    elif args.cmd == "adopt":
        store.adopt_position(args.code, args.qty, args.avg, args.stop)
        print(f"adopted {args.code} {args.qty}주 avg={args.avg} stop={args.stop}")


if __name__ == "__main__":
    main()
