# Ported from binance_futures_autotrading (commit f9b5621). 로직은 원본 그대로이고
# 타임프레임만 1d 로 바꿨다. 파라미터 기본값은 코인 1h 기준이라 KRX 일봉에 맞게
# 검증되지 않았다. 숏 신호는 엔진이 롱 온리로 차단한다 (signals.block_reason).
"""Momentum strategy — N-bar return breakout with trend filter.

Entry: price return over lookback exceeds threshold + EMA50 trend filter.
  Long  — N-bar return > threshold AND close > EMA50 AND volume above avg
  Short — N-bar return < -threshold AND close < EMA50 AND volume above avg

Best in strong trending or high-momentum markets (ADX 35–55).
SL/TP: ATR-based.
"""

from __future__ import annotations

import numpy as np

from src.signal.base_strategy import BaseStrategy, SignalResult
from src.signal.indicators import calc_atr, calc_ema, calc_volume_ratio


class MomentumStrategy(BaseStrategy):
    """N-bar price return momentum with EMA trend filter and volume confirmation.

    Parameters:
        lookback (int):        Bars for return calculation. Default 20.
        mom_threshold (float): % return threshold to trigger. Default 3.0.
        ema_period (int):      EMA period for trend filter. Default 50.
        vol_period (int):      Volume ratio SMA period. Default 20.
        vol_threshold (float): Volume ratio floor. Default 1.2.
        atr_period (int):      ATR period. Default 14.
        sl_atr_mult (float):   SL ATR multiplier. Default 1.5.
        tp1_atr_mult (float):  TP1 ATR multiplier. Default 2.5.
        tp2_atr_mult (float):  TP2 ATR multiplier. Default 4.0.
    """

    DEFAULTS: dict = {
        "lookback":       20,
        "mom_threshold":  3.0,
        "ema_period":     50,
        "vol_period":     20,
        "vol_threshold":  1.2,
        "atr_period":     14,
        "sl_atr_mult":    1.5,
        "tp1_atr_mult":   2.5,
        "tp2_atr_mult":   4.0,
    }

    def get_name(self) -> str:
        return "momentum"

    @classmethod
    def primary_regimes(cls) -> frozenset[str]:
        return frozenset({"trending", "volatile"})

    @classmethod
    def suitability_score(cls, indicators: dict) -> float:
        """Peaks at ADX ~45 + high ATR% — strong directional momentum markets."""
        adx        = float(indicators.get("adx", 25))
        atr_pct    = float(indicators.get("atr_pct", 1.0))
        adx_change = float(indicators.get("adx_change", 0.0))

        adx_score = min(adx / 45.0, 1.0) * max(0.0, 1.0 - (adx - 45.0) / 45.0)
        vol_score = min(atr_pct / 5.0, 1.0) * max(0.0, 1.0 - (atr_pct - 5.0) / 25.0)
        base = adx_score * 0.6 + vol_score * 0.4
        momentum_bonus = min(adx_change / 5.0, 0.10) if adx_change > 0 else 0.0
        return round(min(base + momentum_bonus, 1.0), 4)

    def get_min_candles(self) -> int:
        p = {**self.DEFAULTS, **self.params}
        return int(p["ema_period"]) + int(p["lookback"]) + 10

    def get_timeframe(self) -> str:
        return "1d"  # KRX 일봉 (원본: 1h)

    def generate_signal(self, df, symbol: str) -> SignalResult:
        p = {**self.DEFAULTS, **self.params}
        lookback      = int(p["lookback"])
        mom_threshold = float(p["mom_threshold"])
        ema_period    = int(p["ema_period"])
        vol_period    = int(p["vol_period"])
        vol_threshold = float(p["vol_threshold"])
        atr_period    = int(p["atr_period"])
        sl_atr_mult   = float(p["sl_atr_mult"])
        tp1_atr_mult  = float(p["tp1_atr_mult"])
        tp2_atr_mult  = float(p["tp2_atr_mult"])

        high   = df["high"].to_numpy(dtype=float)
        low    = df["low"].to_numpy(dtype=float)
        close  = df["close"].to_numpy(dtype=float)
        volume = df["volume"].to_numpy(dtype=float)

        ema   = calc_ema(close, ema_period)
        atr   = calc_atr(high, low, close, atr_period)
        vol_r = calc_volume_ratio(volume, vol_period)

        cur_close = float(close[-1])
        cur_ema   = float(ema[-1])
        cur_atr   = float(atr[-1])
        cur_vol_r = float(vol_r[-1])

        if np.isnan(cur_ema) or np.isnan(cur_atr):
            return SignalResult(signal_type="none", reason="지표 계산값 NaN")

        if len(close) < lookback + 1:
            return SignalResult(
                signal_type="none",
                reason=f"데이터 부족: {len(close)} < {lookback + 1}",
            )

        past_close = float(close[-(lookback + 1)])
        if past_close <= 0:
            return SignalResult(signal_type="none", reason="과거 종가 0 이하")

        momentum_pct = (cur_close - past_close) / past_close * 100
        vol_ok = not np.isnan(cur_vol_r) and cur_vol_r >= vol_threshold

        indicators_snapshot = {
            "momentum_pct": round(momentum_pct, 4),
            "ema":          round(cur_ema, 6),
            "atr":          round(cur_atr, 6),
            "vol_ratio":    round(cur_vol_r, 4) if not np.isnan(cur_vol_r) else None,
        }

        if momentum_pct > mom_threshold and cur_close > cur_ema and vol_ok:
            entry = cur_close
            sl = entry - cur_atr * sl_atr_mult
            if sl <= 0:
                return SignalResult(
                    signal_type="none", indicators=indicators_snapshot,
                    reason=f"SL 계산 오류: sl={sl:.6f}",
                )
            return SignalResult(
                signal_type="long",
                strength_score=3,
                entry_price=entry,
                sl=sl,
                tp1=entry + cur_atr * tp1_atr_mult,
                tp2=entry + cur_atr * tp2_atr_mult,
                indicators=indicators_snapshot,
                reason=(
                    f"롱: 모멘텀={momentum_pct:.2f}%>{mom_threshold}% "
                    f"+ EMA위 + 거래량{cur_vol_r:.2f}x"
                ),
            )

        if momentum_pct < -mom_threshold and cur_close < cur_ema and vol_ok:
            entry = cur_close
            sl = entry + cur_atr * sl_atr_mult
            return SignalResult(
                signal_type="short",
                strength_score=3,
                entry_price=entry,
                sl=sl,
                tp1=entry - cur_atr * tp1_atr_mult,
                tp2=entry - cur_atr * tp2_atr_mult,
                indicators=indicators_snapshot,
                reason=(
                    f"숏: 모멘텀={momentum_pct:.2f}%<-{mom_threshold}% "
                    f"+ EMA아래 + 거래량{cur_vol_r:.2f}x"
                ),
            )

        above_ema = "EMA위" if cur_close > cur_ema else "EMA아래"
        return SignalResult(
            signal_type="none",
            indicators=indicators_snapshot,
            reason=(
                f"모멘텀 미충족: {momentum_pct:.2f}% (임계값±{mom_threshold}%) "
                f"| {above_ema} | 거래량{cur_vol_r:.2f}x"
            ),
        )
