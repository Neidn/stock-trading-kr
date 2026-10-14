-- 003_equity_snapshots.sql — 일별 계좌 자산 (낙폭 정지·구조 규칙 점검용)
--
-- 증권사 잔고 조회의 총평가금액(tot_evlu_amt)을 장 마감 후 하루 1행 기록한다.
-- 입출금이 섞인다. 입금이 있으면 고점이 올라가고, 출금은 낙폭처럼 보인다 — note 에 적는다.

CREATE TABLE IF NOT EXISTS equity_snapshots (
    trade_date   DATE PRIMARY KEY,
    total_eval   BIGINT NOT NULL,                  -- 총평가금액 (증권사)
    deposit      BIGINT NOT NULL,                  -- 예수금총금액 (증권사)
    note         TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
