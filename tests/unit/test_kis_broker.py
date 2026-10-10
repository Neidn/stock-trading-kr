"""KisBroker response parsing with canned payloads (field names from the official samples)."""

from __future__ import annotations

from datetime import date

from src.broker.kis_broker import KisBroker


class StubClient:
    def __init__(self, settings, pages):
        self.cfg = settings.kis
        self.pages = list(pages)
        self.requests = []

    def tr(self, real):
        return "V" + real[1:] if real[0] in "TJC" else real

    def get(self, path, tr_id, params, tr_cont="", retries=2):
        self.requests.append((tr_id, tr_cont, params.get("CTX_AREA_NK100")))
        return self.pages.pop(0)

    def post(self, path, tr_id, body):
        self.requests.append((tr_id, body))
        return {"rt_cd": "0", "output": {"ODNO": "0000012345", "KRX_FWDG_ORD_ORGNO": "00950",
                                         "ORD_TMD": "091000"}}, {}


def test_daily_orders_paging_and_fields(settings):
    row = {"ord_dt": "20261012", "ord_gno_brno": "00950", "odno": "0000012345", "orgn_odno": "",
           "sll_buy_dvsn_cd": "02", "pdno": "005930", "ord_qty": "10", "ord_unpr": "70000",
           "ord_tmd": "091000", "tot_ccld_qty": "4", "tot_ccld_amt": "280000", "rmn_qty": "6",
           "cnc_cfrm_qty": "0", "rjct_qty": "0", "cncl_yn": "N"}
    row2 = {**row, "odno": "0000012346", "sll_buy_dvsn_cd": "01", "tot_ccld_qty": "0",
            "tot_ccld_amt": "0", "rmn_qty": "10"}
    c = StubClient(settings, [
        ({"rt_cd": "0", "output1": [row], "ctx_area_nk100": "NEXT"}, {"tr_cont": "M"}),
        ({"rt_cd": "0", "output1": [row2]}, {"tr_cont": "D"}),
    ])
    out = KisBroker(c).daily_orders(date(2026, 10, 12))
    assert [o.order_no for o in out] == ["0000012345", "0000012346"]
    assert out[0].side == "buy" and out[1].side == "sell"
    assert (out[0].filled_qty, out[0].filled_amount, out[0].remaining_qty) == (4, 280000, 6)
    assert out[0].ordered_at.hour == 9 and out[0].ordered_at.minute == 10
    assert c.requests[0][0] == "VTTC0081R"
    assert c.requests[1][1:] == ("N", "NEXT")          # 연속조회


def test_holdings_and_summary(settings):
    c = StubClient(settings, [({
        "rt_cd": "0",
        "output1": [
            {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10", "ord_psbl_qty": "10",
             "pchs_avg_pric": "70000.0000", "prpr": "71000"},
            {"pdno": "000660", "hldg_qty": "0", "ord_psbl_qty": "0", "pchs_avg_pric": "0", "prpr": "0"},
        ],
        "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1710000", "asst_icdc_amt": "-5000",
                     "bfdy_tot_asst_evlu_amt": "1715000"}],
    }, {})])
    hs, summary = KisBroker(c).holdings()
    assert [h.code for h in hs] == ["005930"]               # 보유 0 행은 제외
    assert hs[0].avg_price == 70000.0 and hs[0].current_price == 71000
    assert summary.asset_change == -5000


def test_quote_block_reasons(settings):
    c = StubClient(settings, [({"rt_cd": "0", "output": {
        "stck_prpr": "70000", "stck_mxpr": "91000", "stck_llam": "49000", "stck_sdpr": "70000",
        "rprs_mrkt_kor_name": "KOSPI200", "temp_stop_yn": "N", "mang_issu_cls_code": "N",
        "mrkt_warn_cls_code": "02", "short_over_yn": "N", "sltr_yn": "N", "vi_cls_code": "N",
        "iscd_stat_cls_code": "55"}}, {})])
    q = KisBroker(c).quote("005930")
    assert q.market == "KOSPI"
    assert q.block_reasons == ("시장경고(02)",) and not q.vi_active


def test_place_order_body(settings):
    c = StubClient(settings, [])
    ack = KisBroker(c).place_order("005930", "buy", 10, "limit", 70000)
    tr_id, body = c.requests[0]
    assert tr_id == "VTTC0012U"
    assert body["ORD_DVSN"] == "00" and body["ORD_QTY"] == "10" and body["ORD_UNPR"] == "70000"
    assert body["EXCG_ID_DVSN_CD"] == "KRX"
    assert (ack.order_no, ack.org_no) == ("0000012345", "00950")
