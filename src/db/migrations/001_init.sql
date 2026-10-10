-- 001_init.sql — 국내주식 자동매매 기본 스키마 (PostgreSQL 16)
--
-- 원칙
--   * 금액은 정수 원(BIGINT). 비율·평균가만 NUMERIC.
--   * 종목코드는 TEXT. 정수로 저장하면 005930 → 5930 이 되어 조인이 조용히 0행이 된다.
--   * 시각은 TIMESTAMPTZ. 집계는 (ts AT TIME ZONE 'Asia/Seoul')::date 로 한다.
--   * 포지션은 fills 로만 바뀐다. orders 는 주문 레코드일 뿐이다.

-- ── 브로커 토큰 공유 캐시 ────────────────────────────────────────────────────
-- 프로세스마다 토큰을 새로 발급하면 발급 빈도 제한에 걸린다. 재시작 루프가 제한에
-- 걸리면 그동안 손절이 없다. 그래서 DB에 두고 모든 프로세스가 공유한다.
CREATE TABLE IF NOT EXISTS broker_tokens (
    env           TEXT PRIMARY KEY,               -- 'demo' | 'real'
    access_token  TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 시장 규칙 (상수가 아니라 데이터) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date    DATE PRIMARY KEY,
    is_open       BOOLEAN NOT NULL,               -- KIS chk-holiday opnd_yn
    is_business   BOOLEAN,                        -- bzdy_yn
    is_settlement BOOLEAN,                        -- sttl_day_yn
    source        TEXT    NOT NULL DEFAULT 'kis', -- 'kis' | 'manual'
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 세션 시간. trade_date IS NULL 행이 기본값, 날짜 행이 그날만의 예외(수능일 등).
CREATE TABLE IF NOT EXISTS market_sessions (
    id                    SERIAL PRIMARY KEY,
    venue                 TEXT NOT NULL DEFAULT 'KRX',
    trade_date            DATE,                    -- NULL = 기본
    regular_open          TIME NOT NULL,
    closing_auction_start TIME NOT NULL,
    regular_close         TIME NOT NULL,
    effective_from        DATE NOT NULL,
    checked_on            DATE NOT NULL,           -- 조회일
    note                  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_market_sessions_default
    ON market_sessions (venue, effective_from) WHERE trade_date IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_market_sessions_override
    ON market_sessions (venue, trade_date) WHERE trade_date IS NOT NULL;

-- 매도 시 제세금 (거래세 + 농특세). 체결일 기준으로 시행일이 가장 늦은 행을 쓴다.
CREATE TABLE IF NOT EXISTS tax_rates (
    market          TEXT    NOT NULL,              -- 'KOSPI' | 'KOSDAQ' | 'KONEX'
    effective_from  DATE    NOT NULL,
    sell_tax_rate   NUMERIC(8,6) NOT NULL,         -- 0.002000 = 0.20%
    checked_on      DATE    NOT NULL,
    source          TEXT    NOT NULL,
    verified        BOOLEAN NOT NULL DEFAULT FALSE,-- 1차 출처로 확인했는가
    PRIMARY KEY (market, effective_from)
);

-- 호가단위. 가격 하한 이상 & 다음 구간 미만이면 해당 tick.
CREATE TABLE IF NOT EXISTS tick_sizes (
    effective_from  DATE    NOT NULL,
    price_from      BIGINT  NOT NULL,
    tick            INTEGER NOT NULL,
    checked_on      DATE    NOT NULL,
    PRIMARY KEY (effective_from, price_from)
);

-- 종목 정보 (장 전 갱신)
CREATE TABLE IF NOT EXISTS instruments (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    market          TEXT,                          -- 'KOSPI' | 'KOSDAQ'
    tradable        BOOLEAN NOT NULL DEFAULT TRUE,
    block_reason    TEXT,                          -- 거래정지·관리·경고 등
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 주문 · 체결 · 포지션 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id              BIGSERIAL PRIMARY KEY,
    client_id       TEXT UNIQUE NOT NULL,          -- 로컬 생성 ID (멱등성 키)
    order_no        TEXT UNIQUE,                   -- 증권사 주문번호 (ODNO)
    org_no          TEXT,                          -- KRX_FWDG_ORD_ORGNO (정정·취소에 필요)
    parent_order_no TEXT,                          -- 정정 시 원주문번호
    code            TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
    order_type      TEXT NOT NULL,                 -- 'limit' | 'market'
    qty             INTEGER NOT NULL CHECK (qty > 0),
    price           BIGINT,                        -- 지정가. 시장가는 NULL
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    filled_amount   BIGINT  NOT NULL DEFAULT 0,    -- 누적 체결금액 (증권사 tot_ccld_amt)
    state           TEXT NOT NULL,
    venue           TEXT NOT NULL DEFAULT 'KRX',
    purpose         TEXT NOT NULL,                 -- 'entry' | 'stop' | 'take_profit' | 'exit' | 'manual'
    strategy_name   TEXT,
    reject_reason   TEXT,
    stop_price      BIGINT,                        -- 진입 주문: 체결 시 포지션에 옮길 손절가
    take_profit     BIGINT,
    initial_risk    NUMERIC(14,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    trade_date      DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_open ON orders (state)
    WHERE state IN ('NEW','UNKNOWN','ACCEPTED','PARTIAL','REPLACING','CANCELLING');
CREATE INDEX IF NOT EXISTS ix_orders_code ON orders (code, trade_date);

-- 체결. KIS 일별주문체결조회는 주문별 누적 체결만 준다. 그래서 체결 1건 =
-- "누적 체결수량이 늘어난 구간"이고, fill_seq 는 그 시점의 누적 수량이다.
-- (order_no, fill_seq) 로 중복 제거한다.
CREATE TABLE IF NOT EXISTS fills (
    id              BIGSERIAL PRIMARY KEY,
    order_no        TEXT    NOT NULL,
    fill_seq        INTEGER NOT NULL,              -- 이 체결 반영 후 누적 체결수량
    code            TEXT    NOT NULL,
    market          TEXT,
    side            TEXT    NOT NULL CHECK (side IN ('buy','sell')),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    amount          BIGINT  NOT NULL,              -- 이 구간 체결금액
    price           NUMERIC(14,2) NOT NULL,        -- amount / qty
    fee             BIGINT  NOT NULL,
    tax             BIGINT  NOT NULL,
    cost_source     TEXT    NOT NULL DEFAULT 'estimated', -- 'estimated' | 'broker'
    venue           TEXT    NOT NULL DEFAULT 'KRX',
    strategy_name   TEXT,
    source          TEXT    NOT NULL,              -- 'poll' | 'reconcile' | 'ws'
    executed_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (order_no, fill_seq)
);
CREATE INDEX IF NOT EXISTS ix_fills_date ON fills (executed_at);

-- 종목당 포지션 1개 (롱 온리). 전략 둘이 같은 종목을 들면 전략별 손익을 낼 수 없다.
CREATE TABLE IF NOT EXISTS positions (
    code            TEXT PRIMARY KEY,
    qty             INTEGER NOT NULL CHECK (qty >= 0),
    avg_price       NUMERIC(14,2) NOT NULL,        -- 이동평균 (증권사 기본 방식과 맞춤)
    strategy_name   TEXT,
    stop_price      BIGINT,
    take_profit     BIGINT,
    initial_risk    NUMERIC(14,2),                 -- 진입가 - 최초 손절가 (1R, 주당)
    opened_at       TIMESTAMPTZ,
    -- 현재 라운드트립 누적 (포지션이 0이 되면 trades 로 옮긴다)
    rt_buy_qty      INTEGER NOT NULL DEFAULT 0,
    rt_buy_amount   BIGINT  NOT NULL DEFAULT 0,
    rt_sell_amount  BIGINT  NOT NULL DEFAULT 0,
    rt_fee          BIGINT  NOT NULL DEFAULT 0,
    rt_tax          BIGINT  NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 라운드트립. 포지션이 0이 될 때 1행.
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL,
    market          TEXT,
    strategy_name   TEXT,
    entry_at        TIMESTAMPTZ NOT NULL,
    exit_at         TIMESTAMPTZ NOT NULL,
    qty             INTEGER NOT NULL,              -- 총 매수수량
    buy_amount      BIGINT NOT NULL,
    sell_amount     BIGINT NOT NULL,
    gross_pnl       BIGINT NOT NULL,
    fee             BIGINT NOT NULL,
    tax             BIGINT NOT NULL,
    net_pnl         BIGINT NOT NULL,
    realized_r      NUMERIC(8,3),
    close_reason    TEXT,
    cost_source     TEXT NOT NULL DEFAULT 'estimated'
);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades (exit_at);

-- 신호 기록 (차단된 신호 포함 → shadow 분석용)
CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL,
    strategy_name   TEXT NOT NULL,
    signal_type     TEXT NOT NULL,
    entry_price     NUMERIC(14,2),
    stop_price      NUMERIC(14,2),
    take_profit     NUMERIC(14,2),
    block_reason    TEXT,                          -- NULL = 주문까지 갔다
    reason          TEXT,
    indicators      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 리스크 상태 ─────────────────────────────────────────────────────────────
-- 킬 스위치. 단일 행. level 0 정상, 1 신규진입 중지, 2 미체결 전부 취소, 3 전량 청산.
CREATE TABLE IF NOT EXISTS kill_switch (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    level       INTEGER NOT NULL DEFAULT 0 CHECK (level BETWEEN 0 AND 3),
    reason      TEXT,
    set_by      TEXT,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO kill_switch (id, level, reason, set_by) VALUES (1, 0, 'init', 'migration')
    ON CONFLICT (id) DO NOTHING;

-- 종목 잠금 (대조 불일치·기업행위). 신규 진입만 막는다. 손절은 계속.
CREATE TABLE IF NOT EXISTS symbol_locks (
    code        TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    locked_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,                     -- 'alert' | 'kill' | 'reconcile' | ...
    message     TEXT NOT NULL,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
