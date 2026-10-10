# stock-trading-kr

한국투자증권 OpenAPI로 국내주식(KRX 정규장, 롱 온리)을 자동매매하는 봇입니다.
k3s에 올리고, PostgreSQL은 같은 네임스페이스에 파드로 띄우며, 알림은 텔레그램으로 받습니다.

설계 기준은 `neidn/ai-skills`의 두 스킬입니다.

- `stock-trading-execution` — 주문·체결·손절·대조·킬 스위치
- `stock-trading-book-audit` — 손익 원장, 비용 차감 net, 표본·판정 규칙

전략 인터페이스와 지표는 `binance_futures_autotrading`(commit f9b5621)에서 가져왔습니다.

> **현재 단계: 모의투자 배선 검증.** 아래 "실전 전환 게이트"를 통과하기 전에는 `KIS_ENV=real`로 바꾸지 마세요.
> 실전으로 바꾸려면 `KIS_REAL_CONFIRM` 시크릿도 넣어야 하고, 이 값이 없으면 엔진이 기동을 거부합니다.

## 구조

```
trader 파드 (replicas 1, Recreate)
  ├─ StopMonitor   5초마다 잔고 조회 1회 → 봇 포지션 손절·익절·킬스위치 청산
  ├─ OrderManager  10초마다 당일 주문체결 조회 → 상태 전이·체결 반영 (UNKNOWN 해소, 외부 주문 감지)
  ├─ EntryPlanner  하루 1회(SIGNAL_TIME) 완성된 일봉으로 신호 → 지정가 진입
  ├─ Reconciler    5분마다 + 기동 시 + 장 마감 후: 체결·미체결·잔고 대조
  └─ daily_prep / end_of_day  휴장일·종목상태 갱신 / 마감 요약
postgres 파드 (PVC)       orders · fills · positions · trades · signals · 시장규칙 테이블 · 토큰 캐시
CronJob performance-report  평일 16:20 book-audit 형식 리포트 → 텔레그램
CronJob db-backup           매일 18:00 pg_dump
```

손절은 거래소에 걸려 있지 않습니다. trader 프로세스가 살아 있을 때만 동작합니다.
그래서 매도 주문을 내는 주체를 하나로 두었습니다(replicas 1, Recreate).
재시작하면 **잔고 조회 → 손절 점검을 가장 먼저** 하고, 대조와 진입은 그 뒤에 합니다.

```
src/
  broker/      KIS HTTP 클라이언트(토큰 DB 캐시·호출 제한·오류 분류), KisBroker, FakeBroker
  market/      호가단위·세율·세션·휴장일 — DB 테이블에서 읽음 (상수 없음)
  execution/   주문 상태 머신, OrderManager, Store(Postgres), 비용 추정, 대조
  risk/        사이징(1주 미만 진입 안 함), 손절 감시, 3단계 킬 스위치
  signal/      BaseStrategy·지표·전략 4개 (ema_crossover, rsi_macd, bb_rsi_chartart, momentum)
  engine/      메인 루프, 진입
  audit/       성과 리포트
  cli.py       운영 명령 (status / kill / unlock / adopt)
k8s/           namespace, priorityclass, postgres, trader, cronjobs, configmap, secrets 예시
```

## 스킬 규칙과 구현 위치

| 규칙 | 구현 |
|---|---|
| 포지션은 체결로만 바뀐다 | `Store.apply_fill` 한 트랜잭션: fills → orders → positions → trades |
| 타임아웃이면 재주문하지 않고 UNKNOWN으로 둔 뒤 조회로 확정 | `OrderManager.submit` / `_resolve_unknown`. UNKNOWN 상태에서는 같은 종목·방향 주문 차단 |
| 체결 중복 제거 | `fills UNIQUE(order_no, fill_seq)`. KIS는 주문별 누적 체결만 주므로 누적 증가분을 체결 1건으로 처리 |
| 거부는 재시도하지 않음 | `BrokerRejected` → `REJECTED`, 사유 기록 |
| 취소는 요청이 아니라 응답으로 확정 | `CANCELLING` 중간 상태 |
| 장 운영 규칙은 데이터 | `tick_sizes`, `tax_rates(시행일, 조회일, verified)`, `market_sessions`(날짜별 예외 가능), `trading_calendar` |
| 매수 가격은 내림, 매도 가격은 올림, 경계는 재검증 | `MarketRules.round_to_tick` |
| 주문가능금액은 증권사 조회값 사용 | `buyable_cash` = 미수 없는 매수금액(`nrcvb_buy_amt`) |
| 1주 미만이면 진입하지 않음 | `size_entry` |
| 손절 재주문을 쌓지 않음 (동시호가·VI) | 같은 종목에 열린 매도(UNKNOWN 포함)가 있으면 새 매도를 내지 않음 |
| 토큰 공유 캐시, 갱신 실패는 치명 | `broker_tokens` 테이블 + advisory lock. `BrokerAuthError`가 나면 알림 후 종료(exit 2) |
| 대조에서 증권사가 진실, 정수배 차이는 기업행위로 의심 | 종목 잠금 + 킬 스위치 1단계. 정수배 차이일 때는 손절도 보류하고 사람에게 넘김 |
| 킬 스위치 3단계 | 자동 트리거는 1단계까지만 올림. 2·3단계는 CLI로 사람이 설정 |
| 수수료·세금 차감 net, 비용 추정 여부 표시 | `fills.cost_source='estimated'`, 리포트 전제 절에 표시 |
| 매매당 net + N + 진입일 수, 표본이 부족하면 보류 | `src/audit/report.py` |

**봇이 사지 않은 보유 종목은 팔지 않습니다.** 대조 시 "unmanaged" 알림만 보냅니다.
봇 전용 계좌를 권장합니다. 기존 보유분을 봇이 관리하게 하려면 `cli adopt`로 명시적으로 편입하세요.

## 배포 (k3s)

```bash
# 1. 네임스페이스·우선순위
kubectl apply -f k8s/namespace.yaml -f k8s/priorityclass.yaml

# 2. 시크릿 — 파일을 남기지 않도록 kubectl로 직접 만든다 (k8s/secrets/*.yaml.example 참고)
kubectl -n stock-trading create secret generic kis-api-secret \
  --from-literal=KIS_APP_KEY=... --from-literal=KIS_APP_SECRET=... --from-literal=KIS_ACCOUNT_NO=12345678
kubectl -n stock-trading create secret generic telegram-secret \
  --from-literal=TELEGRAM_BOT_TOKEN=... --from-literal=TELEGRAM_CHAT_ID=...
PW=$(openssl rand -hex 24)
kubectl -n stock-trading create secret generic postgres-secret \
  --from-literal=POSTGRES_USER=trading --from-literal=POSTGRES_PASSWORD=$PW --from-literal=POSTGRES_DB=trading \
  --from-literal=DATABASE_URL=postgresql://trading:$PW@postgres:5432/trading
kubectl -n stock-trading create secret docker-registry dockerhub-credentials ...   # private 이미지일 때

# 3. 설정 — FEE_RATE 를 계좌의 실제 수수료율로 채운다 (비어 있으면 엔진이 기동하지 않음)
$EDITOR k8s/configmaps/trading-config.yaml

# 4. DB → 엔진 → 크론잡
kubectl apply -f k8s/pvcs/ -f k8s/services/ -f k8s/deployments/postgres.yaml
kubectl -n stock-trading rollout status deploy/postgres
kubectl apply -f k8s/configmaps/ -f k8s/deployments/trader.yaml -f k8s/cronjobs/

# 5. 연결 확인 (조회만, 주문 없음)
kubectl -n stock-trading exec deploy/trader -- python -m scripts.kis_smoke 005930
```

- 이미지: `neidn/stock-trading-kr:latest`. GitHub Actions에서 테스트를 통과하면 빌드합니다.
  저장소에 `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` 시크릿이 있어야 푸시됩니다.
- 마이그레이션은 엔진이 기동할 때 자동 적용됩니다(`schema_migrations` 기록, advisory lock).
- **롤백:** `kubectl -n stock-trading rollout undo deploy/trader`로 이전 이미지로 되돌립니다.
  엔진을 멈추려면 `kubectl -n stock-trading scale deploy/trader --replicas=0`입니다.
  이렇게 멈추면 손절 감시도 같이 멈춥니다. 멈추기 전에 킬 스위치 2단계를 걸어 미체결을 정리하는 것을 권장합니다.
  DB는 db-backup PVC의 `trading_YYYY-MM-DD.sql.gz`로 복원합니다.

## 운영

```bash
kubectl -n stock-trading exec deploy/trader -- python -m src.cli status
kubectl -n stock-trading exec deploy/trader -- python -m src.cli kill 1 --reason "점검"   # 0 = 해제
kubectl -n stock-trading exec deploy/trader -- python -m src.cli unlock 005930           # 원인 확인 후
kubectl -n stock-trading exec deploy/trader -- python -m src.audit.report --days 30
```

| 킬 스위치 | 동작 | 누가 거나 |
|---|---|---|
| 1 | 신규 진입 중지 (손절·청산은 계속) | 일손실 한도, 연속 API 오류, 대조 불일치, 시세 갱신 실패 시 자동 |
| 2 | 1단계 + 미체결 전부 취소 (전환 시 1회) | 사람 |
| 3 | 2단계 + 봇 보유 전량 시장가 매도. 동시호가·VI·하한가 구간에서는 즉시 체결되지 않음 | 사람 |

## 테스트

```bash
pip install -r requirements.txt
TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_test pytest
```

DB 테스트는 실제 Postgres를 사용합니다(매 테스트마다 스키마를 초기화). `TEST_DATABASE_URL`이 없으면 건너뜁니다.
`references/order-lifecycle.md` 4절 목록을 FakeBroker로 재현합니다.
대상은 부분체결, 중복 체결, 취소 경합, UNKNOWN 해소, 장마감 소멸, 거부, 대조 복구, 분할, 재시작 순서, 킬 스위치입니다.

## 확인한 것 / 확인하지 못한 것

**공식 샘플 코드로 확인한 것** (`koreainvestment/open-trading-api`, 조회일 2026-10-09):
도메인(실전 9443 / 모의 29443), TR ID(주문 TTTC0012U·0011U, 정정취소 0013U, 잔고 8434R, 일별체결 0081R, 매수가능 8908R, 현재가 FHKST01010100, 일봉 FHKST03010100, 휴장일 CTCA0903R), 모의 TR은 첫 글자를 V로 바꿈, 응답 필드명.

**미확인 — 실제 모의 계좌로 확인이 필요한 것:**

- 초당 호출 한도. 기본값 demo 2 / real 15는 공식 샘플의 sleep 값에서 역산한 추정치입니다.
- 토큰 유효기간·재발급 제한. 최소 발급 간격 65초는 보수적으로 잡은 가정입니다.
- 오류 코드 `EGW00201`(호출 제한), `EGW00123`/`EGW00121`(토큰).
- 시장경고코드·종목상태코드의 값 의미(`mrkt_warn_cls_code` 02/03, `iscd_stat_cls_code` 58).
- 일별체결조회의 취소 주문 레코드 판정(`orgn_odno` + `cncl_yn`), `ord_gno_brno`를 정정·취소 조직번호로 쓸 수 있는지.
- 휴장일 조회가 모의투자에서 되는지. 안 되면 신규 진입은 중지되고 손절만 동작합니다.
- 장 세션 시간, 호가단위, 세율. 스킬 레퍼런스(2026-10 기준) 값을 넣었고 `tax_rates.verified = FALSE`입니다.
  KRX·국세청 자료로 확인한 뒤 `UPDATE tax_rates SET verified = TRUE, checked_on = ...`로 바꿉니다.
- 수수료율. 기본값을 두지 않았으므로 `FEE_RATE`에 직접 넣어야 합니다.
- 포팅한 전략 파라미터는 코인 1h 기준입니다. KRX 일봉에서 검증하지 않았습니다.

## 실전 전환 게이트 (stock-trading-execution 7절)

- [ ] 상태 머신 전이 테스트 — 자동 테스트 있음. 정정(REPLACE)은 사용하지 않음
- [ ] 재시작 시 손절이 먼저 복구되는지 **모의 계좌에서 파드를 직접 죽여서** 확인
- [ ] 대조 불일치 0이 5거래일 연속
- [ ] 위 "미확인" 항목 확인 (특히 세율·수수료율·호출 한도)
- [ ] 킬 스위치 1·2·3단계를 모의 계좌에서 실제로 눌러봄
- [ ] 성과 판단은 `stock-trading-book-audit` 5절 게이트를 따름 (모의 성과는 근거가 아님)

## 다음 단계

- NXT·KRX 애프터마켓 (세션 테이블 `venue` 확장, 주문 `EXCG_ID_DVSN_CD`)
- 실시간 체결통보 웹소켓(H0STCNI0) — 현재는 REST 폴링과 대조로 처리
- 벤치마크(KOSPI/KOSDAQ) 차감 리포트
- 텔레그램 명령(킬 스위치·상태 조회)
- Binance 저장소의 나머지 전략 포팅과 KRX 일봉 기준 검증
