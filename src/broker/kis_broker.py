"""한국투자증권 국내주식 Broker 구현 (KRX 정규장).

TR ID·경로·필드명 출처: koreainvestment/open-trading-api
  examples_user/domestic_stock/domestic_stock_functions.py
  examples_llm/domestic_stock/*/chk_*.py (응답 필드 한글명)
조회일 2026-10-09. 필드의 **코드값 의미**(시장경고코드 등)는 공식 문서로 확인하지
못한 것이 있어 해당 줄에 '미확인'으로 적었다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from src.broker.base import (
    AccountSummary,
    BrokerRejected,
    Holding,
    OrderAck,
    OrderStatus,
    Quote,
)
from src.broker.kis_client import KisClient
from src.utils.time_utils import combine_kst, now_kst, ymd

log = logging.getLogger(__name__)

# 주문구분 ORD_DVSN
ORD_DVSN = {"limit": "00", "market": "01"}

PATH_ORDER = "/uapi/domestic-stock/v1/trading/order-cash"
PATH_RVSECNCL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
PATH_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
PATH_DAILY_CCLD = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
PATH_PSBL_ORDER = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
PATH_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
PATH_DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
PATH_HOLIDAY = "/uapi/domestic-stock/v1/quotations/chk-holiday"

MAX_PAGES = 20


def _i(v) -> int:
    try:
        return int(float(str(v).strip() or 0))
    except ValueError:
        return 0


def _f(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def _market(name: str) -> str:
    # rprs_mrkt_kor_name 예: 'KOSPI200', 'KOSDAQ150' … (값 목록 미확인 — 부분일치로 판정)
    n = (name or "").upper()
    if "KOSDAQ" in n:
        return "KOSDAQ"
    if "KONEX" in n:
        return "KONEX"
    if "KOSPI" in n or "유가" in n:
        return "KOSPI"
    return ""


class KisBroker:
    def __init__(self, client: KisClient):
        self.c = client
        self.env = client.cfg.env
        self._acct = {"CANO": client.cfg.account_no, "ACNT_PRDT_CD": client.cfg.account_product}

    # ── 주문 ────────────────────────────────────────────────────────────────
    def place_order(self, code: str, side: str, qty: int, order_type: str,
                    price: int | None) -> OrderAck:
        if side not in ("buy", "sell"):
            raise ValueError(side)
        if qty <= 0:
            raise ValueError("qty must be positive")
        if order_type == "limit" and not price:
            raise ValueError("limit order needs a price")
        tr_id = self.c.tr("TTTC0012U" if side == "buy" else "TTTC0011U")
        body = {
            **self._acct,
            "PDNO": code,
            "ORD_DVSN": ORD_DVSN[order_type],
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price if order_type == "limit" else 0),
            "EXCG_ID_DVSN_CD": "KRX",      # 정규장 KRX 고정. NXT/SOR 은 추후
            "SLL_TYPE": "01" if side == "sell" else "",
            "CNDT_PRIC": "",
        }
        data, _ = self.c.post(PATH_ORDER, tr_id, body)
        out = data.get("output") or {}
        return OrderAck(
            order_no=str(out.get("ODNO", "")).strip(),
            org_no=str(out.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            ordered_at=now_kst(),
        )

    def cancel_order(self, order_no: str, org_no: str, qty: int | None = None) -> OrderAck:
        tr_id = self.c.tr("TTTC0013U")
        body = {
            **self._acct,
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",          # 02 = 취소
            "ORD_QTY": str(qty or 0),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "N" if qty else "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        data, _ = self.c.post(PATH_RVSECNCL, tr_id, body)
        out = data.get("output") or {}
        return OrderAck(
            order_no=str(out.get("ODNO", "")).strip(),
            org_no=str(out.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            ordered_at=now_kst(),
        )

    # ── 조회 ────────────────────────────────────────────────────────────────
    def _paged(self, path: str, tr_id: str, params: dict) -> tuple[list[dict], list[dict]]:
        """Follow tr_cont/CTX_AREA paging. Returns (output1 rows, output2 rows)."""
        rows1: list[dict] = []
        rows2: list[dict] = []
        tr_cont = ""
        fk = nk = ""
        for _ in range(MAX_PAGES):
            p = {**params, "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk}
            data, headers = self.c.get(path, tr_id, p, tr_cont=tr_cont)
            o1 = data.get("output1") or []
            o2 = data.get("output2") or []
            rows1.extend(o1 if isinstance(o1, list) else [o1])
            rows2.extend(o2 if isinstance(o2, list) else [o2])
            if headers.get("tr_cont") not in ("M", "F"):
                return rows1, rows2
            tr_cont = "N"
            fk = data.get("ctx_area_fk100", "")
            nk = data.get("ctx_area_nk100", "")
        log.warning("%s: paging stopped at %d pages", tr_id, MAX_PAGES)
        return rows1, rows2

    def holdings(self) -> tuple[list[Holding], AccountSummary]:
        rows, summary = self._paged(PATH_BALANCE, self.c.tr("TTTC8434R"), {
            **self._acct,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",           # 종목별
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
        })
        holdings = [
            Holding(
                code=str(r["pdno"]).strip(),
                name=str(r.get("prdt_name", "")).strip(),
                qty=_i(r.get("hldg_qty")),
                sellable_qty=_i(r.get("ord_psbl_qty")),
                avg_price=_f(r.get("pchs_avg_pric")),
                current_price=_i(r.get("prpr")),
            )
            for r in rows
            # 당일 전량매도 종목은 보유 0으로 남는다 (공식 문서). 대조에서는 0으로 본다.
            if r.get("pdno") and _i(r.get("hldg_qty")) > 0
        ]
        s = summary[0] if summary else {}
        return holdings, AccountSummary(
            deposit=_i(s.get("dnca_tot_amt")),
            total_eval=_i(s.get("tot_evlu_amt")),
            asset_change=_i(s.get("asst_icdc_amt")),
            prev_total_asset=_i(s.get("bfdy_tot_asst_evlu_amt")),
        )

    def daily_orders(self, trade_date: date) -> list[OrderStatus]:
        d = ymd(trade_date)
        rows, _ = self._paged(PATH_DAILY_CCLD, self.c.tr("TTTC0081R"), {
            **self._acct,
            "INQR_STRT_DT": d,
            "INQR_END_DT": d,
            "SLL_BUY_DVSN_CD": "00",
            "PDNO": "",
            "CCLD_DVSN": "00",           # 체결·미체결 전체
            "INQR_DVSN": "01",           # 정순
            "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_1": "",
            "EXCG_ID_DVSN_CD": "KRX",
        })
        out: list[OrderStatus] = []
        for r in rows:
            if not r.get("odno"):
                continue
            ord_dt = r.get("ord_dt") or d
            ord_tmd = (str(r.get("ord_tmd") or "000000") + "000000")[:6]
            od = date(int(ord_dt[:4]), int(ord_dt[4:6]), int(ord_dt[6:8]))
            orig = str(r.get("orgn_odno") or "").strip().lstrip("0")
            # 취소 주문 레코드 판정: 원주문번호가 있고 취소여부 Y (필드 의미 일부 미확인)
            is_cancel = bool(orig) and str(r.get("cncl_yn", "")).strip().upper() == "Y"
            out.append(OrderStatus(
                order_no=str(r["odno"]).strip(),
                # 정정·취소에 쓰는 KRX_FWDG_ORD_ORGNO 에 해당한다고 보고 있다 (미확인).
                # 로컬 주문은 접수 응답의 org_no 를 우선 쓴다.
                org_no=str(r.get("ord_gno_brno") or "").strip(),
                orig_order_no=str(r.get("orgn_odno") or "").strip() if orig else "",
                code=str(r.get("pdno", "")).strip(),
                side="sell" if str(r.get("sll_buy_dvsn_cd")) == "01" else "buy",
                qty=_i(r.get("ord_qty")),
                price=_i(r.get("ord_unpr")),
                filled_qty=_i(r.get("tot_ccld_qty")),
                filled_amount=_i(r.get("tot_ccld_amt")),
                remaining_qty=_i(r.get("rmn_qty")),
                cancelled_qty=_i(r.get("cnc_cfrm_qty")),
                rejected_qty=_i(r.get("rjct_qty")),
                is_cancel_order=is_cancel,
                ordered_at=combine_kst(od, ord_tmd),
            ))
        return out

    def buyable_cash(self, code: str, price: int) -> int:
        """미수 없이 살 수 있는 금액 (nrcvb_buy_amt). 예수금을 직접 계산하지 않는다."""
        data, _ = self.c.get(PATH_PSBL_ORDER, self.c.tr("TTTC8908R"), {
            **self._acct,
            "PDNO": code,
            "ORD_UNPR": str(price),
            # 시장가(01)로 조회해야 종목 증거금률이 반영된다 (공식 문서)
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        })
        return _i((data.get("output") or {}).get("nrcvb_buy_amt"))

    def quote(self, code: str) -> Quote:
        data, _ = self.c.get(PATH_PRICE, "FHKST01010100", {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        })
        o = data.get("output") or {}
        reasons: list[str] = []
        if str(o.get("temp_stop_yn", "")).upper() == "Y":
            reasons.append("거래정지")
        if str(o.get("mang_issu_cls_code", "")).upper() == "Y":
            reasons.append("관리종목")
        if str(o.get("sltr_yn", "")).upper() == "Y":
            reasons.append("정리매매")
        if str(o.get("short_over_yn", "")).upper() == "Y":
            reasons.append("단기과열")
        # 시장경고코드: 00 없음 / 01 투자주의 / 02 투자경고 / 03 투자위험 (값 의미 미확인)
        warn = str(o.get("mrkt_warn_cls_code", "") or "00").strip()
        if warn in ("02", "03"):
            reasons.append(f"시장경고({warn})")
        # 종목상태구분코드 58 = 거래정지 (값 의미 미확인)
        if str(o.get("iscd_stat_cls_code", "")).strip() == "58":
            reasons.append("거래정지(58)")
        vi = str(o.get("vi_cls_code", "") or "N").strip().upper()
        return Quote(
            code=code,
            price=_i(o.get("stck_prpr")),
            upper_limit=_i(o.get("stck_mxpr")),
            lower_limit=_i(o.get("stck_llam")),
            base_price=_i(o.get("stck_sdpr")),
            market=_market(str(o.get("rprs_mrkt_kor_name", ""))),
            name=str(o.get("bstp_kor_isnm", "")).strip(),
            block_reasons=tuple(reasons),
            vi_active=vi not in ("N", "", "0"),
            at=now_kst(),
        )

    def daily_candles(self, code: str, count: int) -> pd.DataFrame:
        """수정주가 일봉 (신호용). 오래된 것 → 최신 순. 1회 최대 100건이라 날짜를 거슬러 올라간다."""
        rows: dict[str, dict] = {}
        end = now_kst().date()
        for _ in range(max(1, count // 100 + 2)):
            start = end - timedelta(days=150)
            data, _ = self.c.get(PATH_DAILY_CHART, "FHKST03010100", {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": ymd(start),
                "FID_INPUT_DATE_2": ymd(end),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",        # 0 = 수정주가
            })
            batch = [r for r in (data.get("output2") or []) if r.get("stck_bsop_date")]
            if not batch:
                break
            for r in batch:
                rows[r["stck_bsop_date"]] = r
            if len(rows) >= count:
                break
            oldest = min(r["stck_bsop_date"] for r in batch)
            end = date(int(oldest[:4]), int(oldest[4:6]), int(oldest[6:8])) - timedelta(days=1)
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame([
            {
                "date": k,
                "open": _f(r.get("stck_oprc")),
                "high": _f(r.get("stck_hgpr")),
                "low": _f(r.get("stck_lwpr")),
                "close": _f(r.get("stck_clpr")),
                "volume": _f(r.get("acml_vol")),
            }
            for k, r in rows.items()
        ]).sort_values("date").reset_index(drop=True)
        return df.tail(count).reset_index(drop=True)

    def is_market_open_day(self, d: date) -> bool | None:
        """국내휴장일조회. 공식 문서: 원장 부하 때문에 1일 1회 호출 권장.

        모의투자에서 지원되는지 미확인 — 실패하면 None (판단 불가)을 돌려준다.
        """
        try:
            data, _ = self.c.get(PATH_HOLIDAY, self.c.tr("CTCA0903R"), {
                "BASS_DT": ymd(d), "CTX_AREA_FK": "", "CTX_AREA_NK": "",
            }, retries=0)
        except BrokerRejected as exc:
            log.warning("holiday lookup rejected: %s", exc)
            return None
        rows = data.get("output") or []
        rows = rows if isinstance(rows, list) else [rows]
        for r in rows:
            if r.get("bass_dt") == ymd(d):
                return str(r.get("opnd_yn", "")).upper() == "Y"
        return None
