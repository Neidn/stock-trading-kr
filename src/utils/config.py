"""Runtime configuration from environment variables.

자격증명(앱키·시크릿·계좌번호)은 환경변수(k8s Secret)로만 받는다. 파일·로그·커밋에
남기지 않는다. 실전/모의는 KIS_ENV 하나로 바뀐다 — 그래서 실전은 별도 확인 변수가
없으면 기동을 거부한다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# KIS 도메인. 출처: koreainvestment/open-trading-api kis_devlp.yaml (조회일 2026-10-09)
KIS_URLS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",
}

# 초당 호출 한도 기본값. 공식 샘플 kis_auth.py 의 호출 간 sleep(실전 0.05s, 모의 0.5s)
# 에서 역산한 추정치다 (조회일 2026-10-09, 공식 한도 문서로는 미확인).
# 실전은 여유를 두고 15로 잡는다.
DEFAULT_RPS = {"real": 15.0, "demo": 2.0}

REAL_CONFIRM_VALUE = "I-UNDERSTAND-THIS-IS-REAL-MONEY"


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"환경변수 {name} 가 필요합니다")
    return val


def _float(name: str, default: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"환경변수 {name} 가 필요합니다 (기본값 없음)")
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class KisConfig:
    env: str                     # 'demo' | 'real'
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)   # 계좌 앞 8자리
    account_product: str = "01"           # 계좌 뒤 2자리
    base_url: str = ""
    rps: float = 2.0
    timeout_sec: float = 5.0

    @property
    def is_real(self) -> bool:
        return self.env == "real"


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_krw: int          # 건당 리스크 금액 (진입가-손절가) × 수량 상한
    max_position_krw: int            # 종목당 최대 매수금액
    max_positions: int               # 동시 보유 종목 수
    daily_loss_limit_krw: int        # 일손실 한도 (양수). 넘으면 킬 스위치 1단계
    max_consecutive_errors: int      # 연속 API 오류 → 킬 스위치 1단계
    fee_rate: float                  # 증권사 매매수수료율 (매수·매도 각각). 기본값 없음
    gap_scenario_pct: float          # 오버나잇 갭 시나리오 (예: 0.10 = 시가 -10%)
    # 구조 규칙 (ai-skills stock-trading-execution 3절, trade-history-analysis aoa 사례)
    max_position_pct: float          # 종목당 매수금액 상한 (순자산 대비)
    min_cash_pct: float              # 진입 후에도 남겨둘 현금 하한 (순자산 대비)
    drawdown_halt_pct: float         # 자산 고점 대비 낙폭 → 킬 스위치 1단계


@dataclass(frozen=True)
class EngineConfig:
    universe: tuple[str, ...]
    strategies: tuple[str, ...]
    strategy_params: dict
    signal_time: str                 # "HH:MM" KST, 하루 1회 신호 평가
    stop_poll_sec: int
    order_poll_sec: int
    reconcile_sec: int
    quote_stale_sec: int
    entry_order_timeout_sec: int     # 진입 지정가 미체결 시 취소까지 대기
    health_port: int


@dataclass(frozen=True)
class Settings:
    kis: KisConfig
    risk: RiskConfig
    engine: EngineConfig
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)


def load_kis() -> KisConfig:
    env = os.environ.get("KIS_ENV", "demo").strip().lower()
    if env not in KIS_URLS:
        raise ConfigError(f"KIS_ENV 는 demo 또는 real 이어야 합니다: {env!r}")
    if env == "real" and os.environ.get("KIS_REAL_CONFIRM") != REAL_CONFIRM_VALUE:
        raise ConfigError(
            "KIS_ENV=real 인데 KIS_REAL_CONFIRM 이 설정되지 않았습니다. "
            "실전 전환 게이트(README) 통과 후에만 설정하세요."
        )
    base_url = os.environ.get("KIS_BASE_URL", "").strip() or KIS_URLS[env]
    if env == "demo" and base_url.rstrip("/") == KIS_URLS["real"]:
        # 인자 하나 실수로 실전 주문이 나가는 경로를 막는다 (kis-paper.md 1절).
        raise ConfigError("KIS_ENV=demo 인데 KIS_BASE_URL 이 실전 도메인입니다")
    account = _req("KIS_ACCOUNT_NO")
    if not (account.isdigit() and len(account) == 8):
        raise ConfigError("KIS_ACCOUNT_NO 는 계좌번호 앞 8자리 숫자여야 합니다")
    return KisConfig(
        env=env,
        app_key=_req("KIS_APP_KEY"),
        app_secret=_req("KIS_APP_SECRET"),
        account_no=account,
        account_product=os.environ.get("KIS_ACCOUNT_PRODUCT", "01").strip() or "01",
        base_url=base_url,
        rps=_float("KIS_RPS", DEFAULT_RPS[env]),
        timeout_sec=_float("KIS_TIMEOUT_SEC", 5.0),
    )


def load_risk() -> RiskConfig:
    cfg = RiskConfig(
        risk_per_trade_krw=_int("RISK_PER_TRADE_KRW", 50_000),
        max_position_krw=_int("MAX_POSITION_KRW", 2_000_000),
        max_positions=_int("MAX_POSITIONS", 5),
        daily_loss_limit_krw=_int("DAILY_LOSS_LIMIT_KRW", 200_000),
        max_consecutive_errors=_int("MAX_CONSECUTIVE_ERRORS", 5),
        # 수수료율은 증권사·계좌·이벤트마다 다르다. 기억으로 채우지 않도록 기본값을 두지 않는다.
        fee_rate=_float("FEE_RATE"),
        gap_scenario_pct=_float("GAP_SCENARIO_PCT", 0.10),
        # 모의 단계용 보수적 기본값. 실전 전에 사용자가 다시 정한다.
        max_position_pct=_float("MAX_POSITION_PCT", 0.20),
        min_cash_pct=_float("MIN_CASH_PCT", 0.20),
        drawdown_halt_pct=_float("DRAWDOWN_HALT_PCT", 0.10),
    )
    for name, v in (("MAX_POSITION_PCT", cfg.max_position_pct),
                    ("DRAWDOWN_HALT_PCT", cfg.drawdown_halt_pct)):
        if not 0 < v <= 1:
            raise ConfigError(f"{name} 는 0 초과 1 이하여야 합니다: {v}")
    if not 0 <= cfg.min_cash_pct < 1:
        raise ConfigError(f"MIN_CASH_PCT 는 0 이상 1 미만이어야 합니다: {cfg.min_cash_pct}")
    if cfg.daily_loss_limit_krw <= 0 or cfg.risk_per_trade_krw <= 0:
        raise ConfigError("DAILY_LOSS_LIMIT_KRW, RISK_PER_TRADE_KRW 는 양수여야 합니다")
    if not 0 <= cfg.fee_rate < 0.01:
        raise ConfigError(f"FEE_RATE 값이 이상합니다: {cfg.fee_rate}")
    return cfg


def _codes(raw: str) -> tuple[str, ...]:
    codes = tuple(c.strip() for c in raw.split(",") if c.strip())
    for c in codes:
        if not (len(c) == 6 and c.isalnum()):
            raise ConfigError(f"종목코드는 6자리 문자열이어야 합니다: {c!r}")
    return codes


def load_engine() -> EngineConfig:
    params_raw = os.environ.get("STRATEGY_PARAMS", "").strip()
    return EngineConfig(
        universe=_codes(os.environ.get("UNIVERSE", "")),
        strategies=tuple(
            s.strip() for s in os.environ.get("STRATEGIES", "ema_crossover").split(",") if s.strip()
        ),
        strategy_params=json.loads(params_raw) if params_raw else {},
        signal_time=os.environ.get("SIGNAL_TIME", "09:10").strip(),
        stop_poll_sec=_int("STOP_POLL_SEC", 5),
        order_poll_sec=_int("ORDER_POLL_SEC", 10),
        reconcile_sec=_int("RECONCILE_SEC", 300),
        quote_stale_sec=_int("QUOTE_STALE_SEC", 30),
        entry_order_timeout_sec=_int("ENTRY_ORDER_TIMEOUT_SEC", 120),
        health_port=_int("HEALTH_PORT", 8080),
    )


def load_settings() -> Settings:
    return Settings(
        kis=load_kis(),
        risk=load_risk(),
        engine=load_engine(),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )
