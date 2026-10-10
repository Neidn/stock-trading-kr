-- 002_seed_market_rules.sql — 시장 규칙 초기값
--
-- 출처: ai-skills/stock-trading-execution/references/krx-market-rules.md (기준일 2026-10)
-- 이 값들은 1차 출처(KRX·국세청)로 직접 확인하지 않았다. 그래서 verified = FALSE 이고
-- note 에 "미확인"이라고 적는다. 확인하면 UPDATE 로 verified/checked_on 을 바꾼다.

-- 정규장 기본 세션 (KRX)
INSERT INTO market_sessions
    (venue, trade_date, regular_open, closing_auction_start, regular_close,
     effective_from, checked_on, note)
VALUES
    ('KRX', NULL, '09:00', '15:20', '15:30', '2000-01-01', '2026-10-09',
     '미확인: 스킬 레퍼런스 값. KRX 시장운영 안내로 확인 필요')
ON CONFLICT DO NOTHING;

-- 호가단위 (KOSPI·KOSDAQ 통합, 2023-01 개편 이후). ETF·ETN 은 별도 단위.
INSERT INTO tick_sizes (effective_from, price_from, tick, checked_on) VALUES
    ('2023-01-25',       0,    1, '2026-10-09'),
    ('2023-01-25',    2000,    5, '2026-10-09'),
    ('2023-01-25',    5000,   10, '2026-10-09'),
    ('2023-01-25',   20000,   50, '2026-10-09'),
    ('2023-01-25',   50000,  100, '2026-10-09'),
    ('2023-01-25',  200000,  500, '2026-10-09'),
    ('2023-01-25',  500000, 1000, '2026-10-09')
ON CONFLICT DO NOTHING;

-- 매도 제세금 (거래세 + 농특세), 2026-01-01 양도분부터
INSERT INTO tax_rates (market, effective_from, sell_tax_rate, checked_on, source, verified) VALUES
    ('KOSPI',  '2026-01-01', 0.002000, '2026-10-09', 'skill reference (거래세 0.05% + 농특세 0.15%) — 국세청 확인 필요', FALSE),
    ('KOSDAQ', '2026-01-01', 0.002000, '2026-10-09', 'skill reference (거래세 0.20%) — 국세청 확인 필요', FALSE),
    ('KONEX',  '2026-01-01', 0.001000, '2026-10-09', 'skill reference (거래세 0.10%) — 국세청 확인 필요', FALSE)
ON CONFLICT DO NOTHING;
