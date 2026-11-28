# account1 미국 자동매수 — Kiwoom LIVE-Oriented SHADOW 상세 설계

- 작성: 2026-11-28 · 상태: **설계 초안 (승인 전)** · 코드 조사 기준: `origin/main` 6f196f5, PR #14 head 7da6a4d
- 이번 단계는 조사와 설계만 한다. 브랜치 생성·Push·PR #14 수정·S01 변경·Migration·SHADOW/LIVE 활성화·실주문은 하지 않는다.
- 표기: **[결정]** 사용자가 정한 것 · **[제안]** 설계자가 정한 것(바로잡을 수 있음) · **[미검증]** 실제 API 응답으로 확인하지 않은 것

---

## 0. 결정 기록

| # | 주제 | 결정 |
|---|---|---|
| D1 | 개발 방향 | PR #14 Full SHADOW 개발 중단·동결 보존. 실시간 키움 API 기반 LIVE-Oriented SHADOW 로 전환 **[결정]** |
| D2 | 운영 대상 | account1 미국 주식만. account2·account4 자동매수 제외 **[결정]** |
| D3 | 전략 구성 | B00·B00-N·B01 세 전략 병행. 같은 신호·같은 시세·같은 판단 코드, 전략별 독립 가상 자금. LIVE 후보는 운영 결과로 나중에 지정 **[결정]** |
| D4 | 평가 표본 | 전략별 **진입 40건(청산 + 미청산)** 이후 재평가. 미청산은 평가손익 포함, '청산만 R'과 '미청산 포함 R'을 따로 보고 **[결정]** |
| D5 | 실행 구조 | 기존 uvicorn 프로세스 안의 APScheduler tick 작업 하나 + 전용 SQLite 저널 (S01 과 같은 운영 형태) **[결정]** |
| D6 | 데이터 | 자동매수 SHADOW 는 키움만. Yahoo 신규 수집·자동 fallback 없음 **[결정]** |
| D7 | 키움 호출 한도 | 사용자 제공 공식 한도 (§9) **[결정: 입력값]** |

---

## 1. 의도와 이유

PR #14 는 과거 일봉 재현(Replay)과 46개 가상 계좌의 정합성을 지키는 데 대부분의 복잡도를 썼고, 리뷰마다 원장·Replay·다중 계좌·무결성 결함이 새로 나왔다. 그 노력은 "실제로 자동매수를 운영할 수 있는가"라는 질문에 답하지 못한다.

새 시스템의 목적은 **실제 LIVE 운영 준비**다. 실제 키움 시세로, 실제 운영 시간에, LIVE 와 같은 판단 코드를 돌려서 다음을 쌓는다.

1. 운영 안전성 증거 — 중복 주문 없음, 호출량, 시세 신선도, 장애 복구, S01 무영향
2. 체결·청산 관측 — 가상 체결률, 미체결·불명, 손절 판단
3. 전략 비교 — B00 / B00-N / B01 중 LIVE 후보 선택 근거

### 목표
- SHADOW 와 LIVE 가 매수 적격·수량·위험·주문 생성·손절 판단을 **같은 코드**로 한다. 차이는 Executor 뿐이다.
- 상태 변경은 단일 작업자가 직렬화하고, 하나의 거래 이벤트(체결 → 포지션 → 현금 → 손절 상태)는 한 트랜잭션에 저장한다.
- 관측하지 못한 것은 "불명"으로 남긴다. 미래 데이터로 과거를 채우지 않는다.

### 비목표
- 과거 백테스트·P7-OPS 결과 재현 (승인 조건에서 제외, 참고 자료로 보존)
- 과거 구간 Replay, run 세대 관리
- P7C·B02 무작위 비교군 (별도 연구용 일괄 분석으로 분리)
- LIVE 실주문 구현·활성화, account2·account4
- 범용 무결성 프레임워크, 자동 복구, UI 개선

### 반목표 (기술적으로 돌아가도 실패로 보는 것)
- SHADOW 때문에 S01 자동손절이나 Scanner·수동 매수 조회가 429·지연을 겪는 것
- 관측 공백을 정상 미체결·정상 손절로 기록하는 것
- 이미 기록된 체결·청산을 조용히 바꾸는 것
- 가상 포지션이 S01 운영 저널이나 실제 원장에 섞이는 것
- 기간·건수 충족을 LIVE 승인으로 해석하는 것

---

## 2. 제약

- 기존 판정 유지: NOT_RECOMMENDED, B01 = NEED_MORE_DATA.
- Scanner 매수 후보 기준·리스크 정책 변경 금지. `strict_filter_passed is True` 만 후보.
- 기존 S01 코드·설정·저널 변경 금지(필요하면 범위·위험을 따로 보고). 이번 설계는 **S01 을 import 로만 재사용**한다.
- 운영 DB(Neon)는 읽기만. 표 추가 없음.
- 저널 스키마는 전용 Migration 으로만 만든다. 런타임에서 DDL·파일 생성 없음 (AGENTS.md).
- 블로킹 증권사 호출은 이벤트 루프에서 하지 않는다 (APScheduler 스레드).
- 네트워크 호출을 DB 쓰기 트랜잭션 안에 넣지 않는다.
- 새 개발은 민감정보가 있는 이력을 가져오지 않는 깨끗한 `main` 기반 별도 브랜치에서 한다 (생성은 승인 후).

---

## 3. 접근 방식 (Approaches)

| 방안 | 내용 | 장점 | 대가 | 결론 |
|---|---|---|---|---|
| **A. 앱 내 tick + 전용 SQLite** | APScheduler 30초 tick 하나가 단일 작업자. S01 모듈 import 재사용, PR #14 순수 함수만 이식 | 같은 프로세스라 호출 예산·토큰 캐시 공유, S01 과 같은 운영 형태 | uvicorn 재시작 시 함께 멈춤 → tick 시작 복구 규칙 필요 | **채택 [결정]** |
| B. 별도 워커 프로세스 | systemd 서비스 | 앱 배포와 독립 | 같은 App Key 한도를 프로세스 간 조정해야 함, 토큰 캐시 미공유, 운영 단위 증가 | 제외 |
| S01 엔진 확장 | S01 저널·엔진에 매수 상태 추가 | 최대 재사용 | "S01 운영 저널에 가상 매수 등록 금지", S01 변경 금지 위반 | 제외 |
| 운영 DB 표 추가 | Neon 에 원장 | — | 지침 위반, 원격 DB 지연이 시세 루프에 묶임 | 제외 |
| PR #14 정리 재사용 | 엔진·저널 유지하고 일봉만 교체 | — | run·replay 전제가 거의 모든 함수에 있음. 정리 비용 > 신규 | 제외 |

---

## 4. 기존 키움 API 구현 현황 (조사 결과)

모든 미국주식 TR 은 `trading/kiwoom_readonly.py`(조회)와 `trading/kiwoom_orders.py`(주문, 조회와 분리된 모듈)에 있다. `origin/main` 과 PR #14 사이에 이 모듈들과 S01(`trading/auto_stop/`)의 차이는 없다.

| 기능 | TR | 구현 위치 | 검증 상태 | 새 SHADOW 사용 |
|---|---|---|---|---|
| 현재가 + 시세 시각 | `usa20101` `cur_prc`, `dt`+`bid_tm` (뉴욕 시각, **분 단위**) | `get_overseas_orderbook`, S01 `brokers._parse_orderbook` | S01·Scanner 매수가 사용. **시각 갱신 주기 미확인** (`USA20101_QUOTE_TIME`) | 시세·체결 근거의 유일한 출처 |
| 현재가(시각 없음) | `usa20100` | `get_overseas_quote` | `[1632]` 오류 시 Scanner 매수 보완용 | **체결·판단 근거로 쓰지 않음** (시각 없음) |
| 호가·잔량 | `usa20101` 10호가 | `orderbook_side_prices` (env `KIWOOM_US_ORDERBOOK_BID_FIELD`/`ASK_FIELD`) | **1호가 필드명 미확정** (`USA20101_BID`), 잔량 필드 미확인 | 검증 전에는 현재가 모델만 (§8) |
| 시가 | — | 없음 | **필드 존재 미확인** | 첫 유효 시세로 대체 (§7.3) |
| 장 상태 | — | 키움 TR 없음. 로컬 캘린더 `auto_stop/us_calendar.py` (휴장·조기폐장 + env 추가) | 거래정지·서킷 감지 불가 | 캘린더 + 시세 시각 정지 감지로 대체 |
| 보유 종목 | `ust21070` | `get_overseas_account_balance` (`complete` 검사) | S01 사용. `sell_alowq` 의미 미확인 | 일 1회 실계좌 스냅샷 (참고용) |
| 예수금·외화주문가능 | `ust21110` `fc_ord_alowa` | `get_overseas_deposit` | account2 실계좌 확인 | 일 1회 스냅샷 (참고용) |
| 통화별 자산·평가 | `ust21120`, `ust21121` | `get_overseas_currency_valuation`, `get_overseas_valuation` | 조회 사용 중 | 순자산 기록 (가상 자금 초기값 산정) |
| 종목별 주문가능 | `ust31490` (`ord_alowa`, `min_ord_alowa`, `min_ord_alowq`) | `ExecutionEvidence.buy_capacity` | account2 실계좌만 확인. **account1 미호출** | SHADOW 에서는 호출 안 함 (LIVE 전 확인 항목) |
| 매수 주문 | `ust20000` 지정가 `trde_tp=00` | `KiwoomOrderClient.buy_us_limit` | **account1 미전송** | 호출 안 함 (LIVE 전용, 미구현) |
| 취소 | `ust20003` | `cancel_us_order` | 모의 전량 미체결 취소만. 부분체결 후 취소 미확인 | 호출 안 함 |
| 주문·체결 내역 | `ust21150` (`ord_qty`,`cntr_qty`,`ord_remnq`,`cncl_qty`,`ord_stat_nm`) → OPEN/PARTIAL/FILLED/CANCELLED/REJECTED/MODIFIED/UNKNOWN | `ExecutionEvidence.history/normalize/lookup` | 정규화 있음, **account1 실응답 미대조**. 평균 체결가 필드 없음 | 호출 안 함 (LIVE 대조용) |
| 미체결 | `ust21050` | `ExecutionEvidence.cash_check` | 조회 사용 중 | 호출 안 함 |
| 부분 체결 | `ust21150` `cntr_qty`/`ord_remnq` | 정규화 PARTIAL | 실주문으로만 확인 가능 | SHADOW 는 관측 불가 → 모델링하지 않음 |
| WebSocket 실시간 | — | 없음 | — | 1단계 미사용 (§9.4) |

토큰: `web/kiwoom_holdings._get_token` 이 프로필·App Key 별 프로세스 캐시를 가진다. 같은 App Key 재발급은 같은 토큰을 돌려준다(실측 주석). 새 시스템은 이 캐시를 그대로 쓴다 — 토큰·인증 세션이 늘지 않는다.

---

## 5. 추가로 필요한 API·확인 항목과 보류 정책

새 TR 을 구현할 필요는 없다. 필요한 것은 **응답 계약 확인**이다. 확인 전에는 아래 보류 정책으로 운영한다.

| 항목 | 필요 이유 | 확인 방법 | 확인 전 정책 |
|---|---|---|---|
| `usa20101` 시세 시각 갱신 주기 | 시세 신선도·체결 근거 | 정규장 중 조회 전용 관찰 (S01 절차와 공유) | 120초 이내 시각만 유효. SHADOW 운영은 하되 신선도 지표로 관찰. LIVE 조건 |
| `usa20101` 시가 필드 | P7 '시가 손절·R' 검사 | 응답 필드 목록 확인 | 개장 후 첫 유효 시세로 대체, 모델 버전에 `OPEN=FIRST_QUOTE` 표기 |
| `usa20101` 매수·매도 1호가 필드 | 보수적 '매도1호가 ≤ 지정가' 체결 모델 | 앱 호가와 대조 | 현재가 모델(`CUR_V1`)만. 호가 모델은 검증 후 별도 버전 |
| TR별 개별 호출 한도 | 일부 TR 초당 1회 | 공식 TR 표 확인 | 사용하는 모든 TR 을 **TR당 초당 1회 이하**로 |
| 거래정지·서킷 | 정지 종목 체결 판단 | API 없음 | 시세 시각이 멈추면(120초 초과) 그 종목 판단 보류, 관측 공백 기록 |
| account1 `ust31490` | LIVE 매수 가능 수량 | 조회 전용 1회 (운영자) | SHADOW 는 일 1회 `ust21110` 기준 'LIVE 였다면 차단' 표시만 |
| account1 `ust21150` 실응답 | LIVE 체결 대조 | S01 `UST21150_STATUS` 절차와 공유 | LIVE 미구현 |
| account1 `ust20000`, 부분체결 후 `ust20003` | LIVE 주문 | **실주문 필요** — 별도 승인 | LIVE 미구현 |
| 종목 거래소(ND/NY/NA) | `usa20101` 필수 입력. 스캔 결과에 거래소 열 없음 | S01 `probe_exchange` (3거래소 중 정확히 1개 일치) | 확정 실패 종목은 그날 판단 제외(`EXCHANGE_UNRESOLVED`) |

---

## 6. 재사용 범위

### 6.1 S01 (`trading/auto_stop/`) — import 만, 수정 없음

| 재사용 대상 | 용도 |
|---|---|
| `quotes.Quote`, `quotes.validate`, `is_jump`, `consistent`, `limit_price`, `reference_price` | 시세 유효성(당일 정규장·개장 후·120초·양수), 급변 확인, 가상 손절 지정가 |
| `us_calendar.session_for`, `next_session`, `market_clock`, `ny_date` | 거래일·조기폐장·개장/마감 시각 |
| `brokers.LiveStopSellBroker(config, allow_orders=False)` 의 `quote`, `probe_exchange`, `holdings` | 조회 전용 어댑터. `allow_orders=False` 면 주문 객체를 만들지 않는다 |
| `settings.read_access_block_reason` | 조회 스위치가 꺼져 있으면 증권사 접근 안 함 |
| `web.kiwoom_holdings._get_token` | 토큰 캐시 |

주의: `LiveStopSellBroker` 는 넘겨받은 `requests.Session` 의 `post` 를 429 계수용으로 감싼다. 새 시스템은 **자기 전용 `KiwoomReadOnlyClient` 인스턴스(별도 Session)** 를 만들어 넘기므로 S01 의 세션·계수에 영향이 없다.

### 6.2 S01 에서 개념만 가져오는 것 (코드 복사가 아니라 같은 규칙을 새로 구현)
- lease + `BEGIN IMMEDIATE` 선점, tick 시작 시 미완료 작업 복구
- 연속 오류 보류, kill switch, heartbeat, 알림 outbox, 일일 운영 보고
- LIVE 주문 상태 이름(SENDING/SUBMITTED/PARTIAL_FILLED/FILLED/CANCEL_*/REJECTED/UNKNOWN/NOT_SENT) — 향후 LIVE Executor 가 같은 의미를 쓰도록
- 가상 손절 체결 규칙(`NEXT_QUOTE`): 주문에 쓴 시세보다 **증권사 시각이 새로운** 유효 시세가 지정가 이상일 때만 체결, 체결가는 지정가

### 6.3 S01 과 격리하는 것
- 가상 포지션은 S01 저널(`auto_stop_*`)에 등록하지 않는다. 별도 SQLite 파일.
- 가상 손절은 새 시스템 안에서만 판단·기록한다. S01 Engine·store 를 호출하지 않는다.
- 향후 LIVE: 실제 매수 체결 후 S01 등록은 `trading/auto_stop/service.register(..., mode="LIVE")` 경로가 후보다. 자동 호출을 허용하려면 S01 쪽 변경(호출 주체·승인 정책)이 필요할 수 있어 **LIVE 설계 시 별도 보고**한다.

### 6.4 S01 변경이 필요한 항목 (이번 범위 밖, 보고만)
| 항목 | 이유 | 위험 |
|---|---|---|
| account1 공통 호출 관리기(S01 우선) | 진짜 우선순위 보장 | S01 호출 경로 수정 → S01 회귀 위험. 1단계는 SHADOW 자기 제한으로 충분 (§9) |
| LIVE 매수 체결 → S01 자동 등록 | LIVE 연계 | 사람 확인 없는 등록 정책 결정 필요 |

---

## 7. PR #14 재사용·제거

### 7.1 이식 (순수 함수만, 새 패키지에 테스트와 함께 다시 작성)
| PR #14 | 새 위치 | 비고 |
|---|---|---|
| `eligibility.common_reasons`, `b01_reasons`, `evaluate`, `grade_rank`, `ordered`(GRADE 만) | `strategy.py` | B02 무작위 순위 제외 |
| `settings.STRATEGIES` 의 B00/B00N/B01 | `strategy.py` | B02 제외 |
| `settings.PROFILES["OPS"]` 위험 값 (0.75% / 4% / 15% / 섹터 35% / R 3~15%) | `settings.py` | P7C 제외 |
| `risk.account_state`, `size_order`, `heat_level`, `limit_price`(Decimal 내림) | `sizing.py` | run_id·예약 개념 제거 |
| `S5_CRITERIA.milestones`, 성과 비교 항목 | `report.py` | D4 반영(진입 40건) |
| 스캔 행 정규화 필드 목록 (`snapshot._row_dict`) | `signals.py` | 원본 JSON + 정규화 필드 |

### 7.2 가져오지 않음
run 상태(BUILDING/CURRENT/SUPERSEDED), Replay·replay lease·세대 fencing, `ab_day_inputs` 동결·SSV2·v1 해시, `integrity.py` 격리 프레임워크, `raw_contract.py`, 46 계좌·P7C·B02 seeds, Yahoo `market_data`·`ChartFetcher`, 일봉 가상 체결 `vfills`, `ab0001~ab0007` Migration, P7-OPS parity 하네스, `scan_engine` 스냅샷 hook.

### 7.3 보존
PR #14(Draft, 동결), `feature/auto-buy-shadow` 브랜치, `docs/auto_buy_shadow.md`, `backtest/entry_strategy` 보고서·테스트 — 삭제하지 않고 참고 자료로 둔다.

---

## 8. 신규 시스템 아키텍처

### 8.1 패키지 (깨끗한 `main` 기반 신규 브랜치, 이름 [제안])

```
trading/auto_buy_us/
  settings.py        모드 OFF/SHADOW (LIVE 값은 거부), 위험 값, 주기, 호출 예산
  signals.py         운영 DB 읽기 전용: 정규 스캔 완료 확인 → 신호 스냅샷
  strategy.py        B00/B00N/B01 적격·순위 (순수)
  sizing.py          수량·Entry Heat·Current Heat (순수)
  decision.py        공통 판단: 진입 계획, 개장 검사, 손절 판단 (순수)
  market.py          KiwoomMarketData: 조회 전용 어댑터 + 호출 예산 + tick 내 캐시
  limiter.py         SHADOW 호출 예산 (전체·TR별·피크·429 정지)
  fill_model.py      가상 체결 모델 CUR_V1 (+ 호가 검증 후 BOOK_V1)
  executor.py        Executor 프로토콜, ShadowExecutor, LiveExecutor(생성 즉시 거부)
  store.py           SQLite 원장, 트랜잭션 단위 함수
  engine.py          tick 오케스트레이션 (단일 작업자)
  report.py          일일 운영 보고·성과 (미완료를 정상으로 집계하지 않음)
  runner.py          스케줄러 진입점·조립
  journal_migrations/ + scripts/auto_buy_us_journal_migrate.py
```

`scheduler.py` 에 작업 하나(`auto_buy_us_tick`, 30초, `max_instances=1`, `coalesce=True`)를 추가한다. 기본 모드 OFF 면 tick 은 저널 확인 외에 아무것도 하지 않는다.

### 8.2 tick 흐름 (30초, 뉴욕 시각 기준)

```
lease 획득(BEGIN IMMEDIATE) ─실패→ 종료
kill switch / 조회 스위치 / 스키마 확인 ─차단→ heartbeat 만 기록
시점별 단계:
  [장 마감 후 ~ 다음 개장 전]
    1. 신호 수집: 당일 정규 스캔 완료 확인 → signals 기록 (§7.1)
    2. 진입 계획: 전략별 적격·순위·수량 → decisions + BUY 주문 PLANNED
    3. 신규 종목 거래소 확정 (probe, 피크 시간 회피)
    4. 일일 마감: 성과 스냅샷, 실계좌 참고 스냅샷, 운영 보고
  [정규장]
    5. 시세 조회 대상 = 열린 주문 종목 ∪ 보유 종목 ∪ 첫 시세 대기 PLANNED (종목 중복 제거)
    6. 우선순위 큐에서 예산만큼 조회 (§9) → quotes 기록 (짧은 트랜잭션)
    7. PLANNED: 첫 유효 시세로 개장 검사 → WORKING 또는 SKIPPED_OPEN
    8. WORKING: 체결 모델 → FILLED (체결·포지션·현금·손절 상태 한 트랜잭션)
    9. 보유: 손절 판단 → 가상 매도 → 체결 시 청산 (한 트랜잭션)
   10. 마감 시각: 미체결 BUY 만료 (관측 공백 여부로 구분)
heartbeat · 알림 outbox · lease 해제
```

키움 호출(5·6·3·4의 조회)은 모두 트랜잭션 밖에서 하고, 결과를 받은 뒤 짧은 트랜잭션에 쓴다. lease 는 호출 전후에 갱신하고, 갱신 실패 시 이후 쓰기를 멈춘다(S01 규칙).

### 8.3 신호·주문 시점 정책 (LIVE 기준 명시)

| 항목 | 정책 |
|---|---|
| 결정 스캔 | 거래일 D 의 `ScanLog(market='US', triggered_by='scheduler', status='DONE')` 중 **신호 마감(D 22:00 ET) [제안]** 전에 끝난 첫 스캔. 수동 스캔은 쓰지 않는다 |
| 신호 행 | 해당 스캔의 `started_at ≤ scan_time ≤ finished_at` 인 US 행, `strict_filter_passed is True` |
| 수집 실패 | 수집 전에 다른 US 스캔이 시작됐으면(행 덮어쓰기 가능) 그날 `MISSED`. 마감까지 결정 스캔이 없으면 `NO_SIGNAL_SCAN`. 둘 다 D+1 신규 매수 없음 |
| 신호 유효기간 | D+1 정규장 1회 |
| 판단가·지정가 | 판단가 = 스캔 `price`(D 종가). 지정가 = 판단가 × 1.01, 센트 내림 (P7 규칙) |
| 수량 | D 저녁 계획 시점에 전략 가상 자금으로 계산(판단가 기준). 체결 시 가상 현금이 모자라면 `BLOCKED_CASH` |
| 주문 생성 | D+1 개장 후 **첫 유효 시세**(시각 ≥ 개장, 120초 이내) 수신 시. 이 시세로 개장 검사: `price ≤ stop` 또는 `(price−stop)/price < 3%` 면 `SKIPPED_OPEN` |
| 개장 검사 시세 부재 | 개장 후 30분 [제안] 안에 유효 시세가 없으면 `NOT_STARTED`(관측 불명) |
| 체결 근거 | 시세 `as_of` 가 주문 생성 시각의 분보다 **엄격히 이후**이고 수신 시각도 이후인 것만. 주문 생성에 쓴 시세는 체결 근거가 아니다 |
| 만료 | D+1 정규장 마감(조기폐장 반영). §10 의 만료 상태 구분 |
| 손절가 | 체결 시 Scanner `stop_loss` 를 포지션 `initial_stop` 으로 고정. 이후 바꾸지 않는다 |
| 전략 간 중복 | 같은 종목을 여러 전략이 각자 가상 보유할 수 있다 (자금 독립). 같은 전략 안에서는 보유 중 종목 재진입 없음 (PR #14 규칙 이식) |
| 실계좌 보유 | SHADOW 는 실계좌 보유를 판단에 쓰지 않는다 (가상 자금 기준). LIVE 설계 때 실보유·미체결 대조를 추가 |
| 섹터 | 섹터 상한 35% 의 섹터 값은 스캔 `sector_name` (PR #14 `risk.sector_of` 와 같은 정규화). 알 수 없으면 PR #14 와 같이 **매수 계획 차단** (`SECTOR_UNKNOWN`) — 상한 우회 방지 |
| Current Heat | Σ max(마지막 유효 시세 − initial_stop, 0) × 수량 + 미체결 예정 위험, ÷ 가상 자산. 시세 없는 포지션이 있으면 값을 내지 않고(`None`) `UNPRICED` 로 표시한다. 표시·경고 전용이며 매수를 막지 않는다 |

---

## 9. API 호출량 관리와 우선순위

### 9.1 한도 (사용자 제공 공식 값, D7)
미국주식 REST: 조회 TR 초당 5회(피크 3회), 주문 TR 초당 10회(피크 3회), 미국 전체 초당 50회, 차트 초당 20회, `usa10099` 분당 5회. 피크 = **KST 09:00~10:00** (= 미국 정규장 마감 후, 뉴욕 19:00/20:00~). TR 별 개별 제한이 추가로 적용된다. WebSocket: 계좌·토큰당 세션 1개, 실시간 200종목(국내·미국 합산).

### 9.2 설계 가정
- 한도는 **App Key(계좌) 단위**로 본다 [제안]. account1 은 S01, Scanner 매수 시세·주문, 화면 잔고 조회, 새 SHADOW 가 같은 키로 **조회 초당 5회를 공유**한다. account2·account4 는 다른 키라서 계좌 간 우선순위는 필요 없다.
- 평균 부하: S01 최대 20종목 × 60초 + 주문상태·잔고 ≈ 초당 0.5회 이하. SHADOW 40종목 × 60초 ≈ 초당 0.67회.
- 위험은 **순간 몰림**이다: 기존 잔고·주문내역 페이지 조회가 0.2초 간격(=초당 5회), S01 거래소 확정 3연속 호출.

### 9.3 SHADOW 호출 예산 (`limiter.py`)
| 규칙 | 값 [제안] |
|---|---|
| SHADOW 전체 | 조회 **초당 1회 이하** (호출 간 최소 1초). 나머지 초당 4회는 S01·주문·체결 조회 몫 |
| TR별 | 사용하는 각 TR 초당 1회 이하 (개별 한도 확인 전 보수값) |
| tick 당 상한 | 20회 (30초 tick 안에 끝나도록) |
| 감시 종목 상한 | 40종목. 초과분은 우선순위 낮은 것부터 그 주기 건너뜀 → 관측 누락으로 기록 |
| 피크 시간 | KST 09:00~10:00 에는 SHADOW 조회 0 (정규장과 겹치지 않음. 마감 후 스냅샷·거래소 확정을 이 시간 밖으로) |
| 429 | 즉시 그 tick 의 조회 중단, 60초 쿨다운, `api_calls` 집계. 재시도 루프 없음 (S01 의 2/4/8/16초 재시도를 SHADOW 는 쓰지 않는다) |
| 기타 오류·timeout | timeout 은 S01 과 같은 `KIWOOM_TIMEOUT_SECONDS`. 연속 5회 오류 시 신규 판단 보류(오류 보류), 회복 조회만 |
| 캐시 | 같은 tick 안에서 종목별 시세 1회 조회, 결과를 모든 전략이 공유. 다음 tick 에는 새로 조회 (체결 근거는 새 시세만) |

SHADOW 내부 우선순위: ① 가상 매도 진행 중 종목 ② 보유 종목(손절 감시) ③ WORKING 매수 ④ 첫 시세 대기 PLANNED. 종목별 조회 간격 60초.

### 9.4 하지 않는 것
- **WebSocket 미사용 (1단계)**: 저장소에 구현이 없고 계좌·토큰당 세션 1개라 SHADOW 가 점유하면 향후 LIVE·S01 이 쓸 수 없다. 시세 신선도 지표가 부족하면 다시 검토.
- **S01 우선 공통 관리기**: S01 호출 경로 수정이 필요해 이번 범위 밖(§6.4). SHADOW 자기 제한만으로 평균·순간 부하를 S01 몫 아래로 둔다.
- 주문 TR 우선순위: SHADOW 는 주문 TR 을 호출하지 않는다. LIVE 설계 때 주문·체결 조회 > S01 > SHADOW 순서를 공통 관리기와 함께 정한다.

---

## 10. 주문 상태 머신

### 10.1 매수 (SHADOW)

```
PLANNED ──(첫 유효 시세, 개장 검사 실패)──────────> SKIPPED_OPEN
PLANNED ──(개장 후 30분 유효 시세 없음)───────────> NOT_STARTED        (관측 불명)
PLANNED ──(kill switch·오류 보류·가상 현금 부족)───> BLOCKED(사유)
PLANNED ──(첫 유효 시세, 개장 검사 통과)──────────> WORKING            (가상 주문 생성 시각 기록)
WORKING ──(체결 근거 시세: price ≤ limit)────────> FILLED             (전량, 체결가 = 지정가)
WORKING ──(마감, 관측 공백 없음)──────────────────> EXPIRED            (정상 미체결)
WORKING ──(마감, 관측 공백 있음)──────────────────> EXPIRED_UNOBSERVED (체결 불명)
```

- **관측 공백**: WORKING 동안 그 종목의 유효 시세 간격이 5분 [제안]을 넘은 구간이 하나라도 있으면. 그 구간에 지정가 이하 거래가 있었는지 알 수 없으므로 정상 미체결로 세지 않는다.
- **부분 체결**: SHADOW 는 관측할 수 없어 만들지 않는다. 상태 이름은 LIVE 용으로 예약한다(`PARTIAL_FILLED`).
- 한 번 종료 상태가 되면 바뀌지 않는다. 정정은 새 `system_events` 기록으로만.

### 10.2 가상 손절·청산
```
포지션 OPEN ──(유효 시세 ≤ initial_stop, 급변이면 재조회 일치 확인)──> EXITING + SELL WORKING
  지정가 = reference_price × (1 − slip 50bp), S01 quotes.limit_price
SELL WORKING ──(주문 시세보다 새 시각의 유효 시세 ≥ 지정가)──> FILLED → 포지션 CLOSED(STOP)
SELL WORKING ──(300초 미체결)──> 같은 세션 재가격 (최대 3회, S01 기본값과 동일)
SELL WORKING ──(마감 미체결)──> 다음 정규장 이월 1회 → 이후 NEEDS_REVIEW
급변 불일치·재조회 실패 ──> 포지션 NEEDS_REVIEW (가상 매도 중단, 감시 계속)
```
- 갭 하락으로 첫 시세가 이미 손절가 아래여도 체결은 다음 새 시세 기준. 손절가로 체결됐다고 가정하지 않는다.
- 숫자(50bp, 300초, 3회, 이월 1회)는 S01 기본값과 같게 두어 LIVE S01 과 비교 가능하게 한다. 새 시스템 자체 설정으로 두고 S01 env 를 읽지 않는다.

### 10.3 LIVE (향후, 이번 구현 없음)
`SENDING → SUBMITTED → PARTIAL_FILLED → FILLED`, `REJECTED`, `UNKNOWN`(재전송 금지, 주문내역 증거로만 해제), `CANCEL_SENDING → CANCEL_PENDING → CANCELLED`, `NOT_SENT`. 의미는 S01 과 같다. 체결은 `ust21150` 누적 체결 차이만 반영.

---

## 11. SHADOW / LIVE 공통 인터페이스

```python
class MarketData(Protocol):
    def quote(self, ticker: str, exchange: str) -> QuoteEvidence: ...   # 실패 시 QuoteUnavailable
    def resolve_exchange(self, ticker: str) -> str: ...

class AccountView(Protocol):            # SHADOW: 전략별 가상 자금 / LIVE: 실계좌
    def state(self, strategy: str) -> AccountState: ...   # cash, equity, positions, open_risk

# decision.py — 순수 함수. SHADOW·LIVE 가 같은 코드를 쓴다.
def plan_entries(signals, strategy, account: AccountState, cfg) -> list[EntryPlan]   # 적격·순위·수량·위험
def open_check(plan: EntryPlan, q: QuoteEvidence, cfg) -> OpenVerdict               # 개장 검사
def stop_check(pos: Position, q: QuoteEvidence, prev: QuoteEvidence | None, cfg) -> StopVerdict
def exit_limit(q: QuoteEvidence, cfg) -> Decimal

class Executor(Protocol):
    kind: Literal["SHADOW", "LIVE"]
    def submit(self, order: OrderIntent) -> SubmitResult         # ACCEPTED(ref) / REJECTED / UNKNOWN
    def observe(self, order: Order, q: QuoteEvidence | None) -> Observation   # 상태, 누적 체결, 체결가, 근거
    def cancel(self, order: Order) -> CancelResult

class ShadowExecutor:   # 증권사 주문 호출 없음. ref = "V"+uuid. observe = fill_model
class LiveExecutor:     # 이번에는 생성자에서 NotImplementedError. kiwoom_orders 를 import 하지 않는다
```

- `engine.py` 는 Executor 종류를 모른다. 상태 전이·트랜잭션은 같고, 체결 근거만 Executor 가 준다.
- SHADOW 패키지 어디에서도 `KiwoomOrderClient` 를 import 하지 않는다 (테스트로 확인).
- 모드 `LIVE` 설정은 거부하고 OFF 로 동작한다.

---

## 12. 최소 데이터베이스 구조 (전용 SQLite, 전용 Migration `abu0001`)

| 테이블 | 핵심 열 | 쓰기 규칙 |
|---|---|---|
| `signal_days` | session_day PK, status(CAPTURED/NO_SIGNAL_SCAN/MISSED), scan_log_id, captured_at | 한 번 기록 |
| `signals` | id, session_day, scan_log_id, ticker, exchange, signal_type, grade, signal_quality, price, pivot, stop_loss, market_condition, strict 결과, first_detected_at, raw_json, strategy_version · UNIQUE(scan_log_id, ticker) | 삽입만 |
| `decisions` | id, strategy, session_day(D+1), signal_id, verdict(PLANNED/REJECTED/RANK_CUT/SIZE_ZERO/HEAT_LIMIT…), reasons, qty, limit_price, stop, entry_heat · UNIQUE(strategy, session_day, ticker) | 삽입만 |
| `orders` | id, strategy, decision_id/position_id, side, qty, limit_price, state, executor, fill_model, created_at, created_quote_id, expires_at, broker_ref, ended_at | 상태 전이만 (§10), 종료 후 불변 |
| `order_events` | order_id, seq, from_state, to_state, at, evidence_json | 삽입만 |
| `fills` | order_id, cum_qty (PK 함께), qty, price, at, quote_id, model, basis | 삽입만 |
| `positions` | id, strategy, ticker, exchange, qty, entry_price, initial_stop, state(OPEN/EXITING/CLOSED/NEEDS_REVIEW), opened_at, closed_at, exit_reason, exit_price | 상태 전이만 |
| `sleeves` | strategy PK, initial_cash, initial_basis(account1 순자산·시각), cash, realized_pnl | 체결 트랜잭션에서만 갱신 |
| `quotes` | id, ticker, exchange, requested_at, received_at, as_of, cur, bid, ask, source_tr, valid, invalid_reason | 삽입만 |
| `account_snapshots` | at, net_asset, orderable_cash, holdings_json, complete | 실계좌 참고, 일 1회 |
| `daily_perf` | strategy, day, equity, cash, open_value, unpriced, entries, closed, status(COMPLETE/INCOMPLETE) | 마감 후 1회, 재기록 없음 |
| `system_events` | at, level, kind, details_json | 삽입만 |
| `api_calls` | minute, tr, ok, err, http_429, timeout | 증가만 |
| `runtime` | key PK, value (lease, kill, error_hold, heartbeat) | — |

원자성 단위:
- **매수 체결**: fill 삽입 + order FILLED + order_event + position 생성(initial_stop 고정) + sleeve 현금 차감 — 한 트랜잭션.
- **손절 발동**: position EXITING + SELL order WORKING + order_event — 한 트랜잭션.
- **청산 체결**: fill + SELL FILLED + position CLOSED + sleeve 현금·실현손익 — 한 트랜잭션.
- 상태 전이는 `UPDATE … WHERE id=? AND state=?` 조건부로 하고, 영향 행이 0 이면 롤백한다.

불변성: 삽입만 하는 표(`signals`, `decisions`, `order_events`, `fills`, `quotes`, `system_events`)에는 UPDATE/DELETE 거부 트리거만 둔다. 그 이상의 무결성 프레임워크는 만들지 않는다.

가상 자금: SHADOW 시작 시 account1 순자산(`ust21120`/`ust21121` 기준) × 30% 를 전략마다 같은 값으로 한 번 기록 [제안]. 실계좌가 바뀌어도 다시 계산하지 않는다.

---

## 13. 성과 보고 규칙

- 지표: 수익률, PF, MDD, 평균 R(청산만 / 미청산 포함 따로), Entry Heat, Current Heat, 가상 체결률, 미체결·불명 비율, 손절 결과, 시세 신선도, 관측 누락률, API 호출량·실패율·429, 주문 중복(0이어야 함), S01 영향(같은 기간 S01 보고의 429·지연).
- 미청산 평가: 그날 마지막 유효 시세. 없으면 `UNPRICED` — 0원이나 전일값으로 채우지 않고 그날 `daily_perf.status=INCOMPLETE`.
- `INCOMPLETE` 인 날이 있거나 `EXPIRED_UNOBSERVED`·`NOT_STARTED` 가 있으면 보고서 상단에 표시하고, 해당 지표는 '불명 포함' 범위로 함께 보인다.
- 표본: 전략별 진입 40건 (D4). 마일스톤 5 / 20 / 60 / 120 거래일. 충족해도 LIVE 승인 아님.

---

## 14. 단계별 구현 계획 (설계 승인 후)

| 단계 | 내용 | 증권사 호출 | 완료 조건 |
|---|---|---|---|
| P0 | 승인 후 `main` 기반 신규 브랜치 생성 | 없음 | 브랜치에 PR #14 이력 없음 |
| P1 | settings(OFF/SHADOW, LIVE 거부), 저널 Migration `abu0001` + migrate 스크립트(백업·사전 점검·사후 검증), store 트랜잭션 함수 | 없음 | 스키마·불변 트리거·원자성 테스트 |
| P2 | signals 수집(운영 DB 읽기), strategy·sizing·decision 순수 함수 이식 | 없음 | 적격·순위·수량 단위 테스트, PR #14 함수와 같은 입력 같은 출력 |
| P3 | market(조회 전용 어댑터·거래소 확정), limiter, quotes 기록 | 가짜 세션만 | 예산·TR별·피크·429 정지 테스트 |
| P4 | ShadowExecutor·fill_model CUR_V1, 매수 상태 머신, 포지션·가상 손절·청산 | 가짜 세션만 | 상태 머신·체결 근거 시각·관측 공백·원자성 테스트 |
| P5 | engine tick·lease·복구·kill switch·오류 보류·heartbeat·알림·일일 보고·성과 | 가짜 세션만 | 재시작·중복 실행·장애 주입 테스트 |
| P6 | 리뷰 → 운영자 승인 → VM 기본 OFF 배포 → 저널 Migration → 조회 전용 계약 확인(§5) | 조회 전용 | OFF 무영향 확인 |
| P7 | SHADOW 활성화(별도 승인) 및 운영 검증 (§15.2) | 조회 전용 | 마일스톤별 보고 |
| (별도) | LIVE Executor 설계·구현 | 실주문 | 별도 승인 |

---

## 15. 필수 테스트와 개발 종료 기준

### 15.1 테스트 (P1~P5, 모두 임시 SQLite·가짜 키움 응답)
- **주문 경로 차단**: 패키지가 `kiwoom_orders` 를 import 하지 않음, LIVE 설정 거부, LiveExecutor 생성 거부.
- **체결 근거**: 주문 생성에 쓴 시세·그 이전 시각 시세로는 체결되지 않음, 같은 분 시각 거부, `usa20100`(시각 없음) 거부, 프리마켓 시각 거부, 120초 초과 거부.
- **상태 구분**: FILLED / EXPIRED / EXPIRED_UNOBSERVED / NOT_STARTED / SKIPPED_OPEN 각각 재현.
- **원자성**: 체결 트랜잭션 중간 실패 시 포지션·현금·주문이 모두 이전 상태. 조건부 전이 경합에서 한쪽만 성공.
- **단일 작업자·재시작**: 두 작업자가 같은 tick 을 돌려도 주문·체결 중복 0, lease 상실 후 쓰기 중단, 재시작 후 진행 중 상태 복구.
- **호출 예산**: 초당 1회·TR별·tick 상한·피크 0회·429 즉시 정지·종목 중복 제거, S01 세션 객체 미변경.
- **신호 수집**: 수동 스캔 무시, 덮어쓰기 가능 시 MISSED, 마감 초과 NO_SIGNAL_SCAN, strict null/false 제외.
- **손절**: 고정 손절가, 급변 재조회, 갭 하락 시 손절가 체결 가정 없음, 이월·NEEDS_REVIEW.
- **성과**: UNPRICED·불명 상태를 정상 집계하지 않음, 진입 40건 계산, 청산만/미청산 포함 R 분리.
- **회귀**: 기존 S01·Scanner·수동 매수·전체 테스트 통과 (지정 환경 Python 3.11 + pandas 1.5.3, 호환 환경 결과는 따로 보고).

### 15.2 운영 검증 (P7)
| 기간 | 확인 |
|---|---|
| 첫 5거래일 | 중복 0, S01 429·지연 변화 없음, 호출량 예산 이내, tick 지연·누락, 알림 |
| 20거래일 | 재시작·네트워크 오류·토큰 만료 복구, 관측 누락률, 신호 수집 실패율 |
| 60거래일 | 중간 성과·체결률·불명 비율 검토 |
| 120거래일+ | 필요 시 연장. 전략별 진입 40건 이후 재평가 |

### 15.3 개발 종료 기준 (P5 완료 판정)
15.1 전부 통과, 전체 테스트 회귀 없음, 미검증 계약(§5)이 문서와 코드 게이트에 남아 있음, 독립 리뷰 P1 0건. 관리자 DDL 완전 방어·자동 복구는 만들지 않는다 (안전 중단은 유지).

---

## 16. 기존 시스템 영향

| 대상 | 영향 | 근거 |
|---|---|---|
| S01 자동손절 | 코드·설정·저널 변경 없음. 같은 App Key 조회 한도를 SHADOW 가 최대 초당 1회 사용 | §6, §9 |
| Scanner | 변경 없음 (hook 없음, 운영 DB 읽기만) | §8.3 |
| 수동·Scanner 매수 | 변경 없음 | — |
| 운영 DB | 읽기만, 표 추가 없음 | — |
| `scheduler.py` | 작업 1개 추가 (기본 OFF) | §8.1 |
| 토큰 | 기존 캐시 공유, 발급 증가 없음 | §4 |
| PR #14 | 동결 보존, 병합하지 않음 | §7.3 |
| 문서 | `docs/auto_buy_us_shadow.md` 신설, CLAUDE.md·AGENTS.md 에 진입점 한 줄 | — |

---

## 17. 남은 위험

- `usa20101` 은 분 단위 시각이라 체결 근거가 최소 1분 늦다. 빠른 돌파 종목은 가상 체결을 놓칠 수 있다(보수적 방향).
- 현재가 모델(CUR_V1)은 대기열 순서·잔량을 모르므로 LIVE 체결률과 다를 수 있다. LIVE 초기 결과와 비교해 보정한다.
- 시가 필드가 없으면 P7 개장 검사와 다르다 → 모델 버전으로 구분, 백테스트와 직접 비교하지 않는다.
- 자동 청산이 손절뿐이라 청산 표본이 손실로 치우친다 → D4 로 평가하되 해석 주의를 보고서에 고정 표기.
- 한도가 App Key 단위라는 가정이 틀리면(계정 단위 등) account1·account2 를 합산해야 한다 → P6 조회 전용 확인 항목에 포함.

## 18. 설계자가 정한 것 (확인 요청)

1. 결정 스캔 마감 D 22:00 ET, 수동 스캔 제외
2. 개장 검사 시세 대기 30분, 관측 공백 기준 5분
3. 가상 자금 = 시작 시점 account1 순자산 × 30%, 세 전략 같은 값, 이후 재계산 없음
4. 실계좌 주문가능금액 부족은 표시만, 가상 진행 계속
5. SHADOW 호출 예산 초당 1회, TR별 1회, 감시 40종목, tick 20회, 피크 0회, 429 즉시 정지
6. 가상 손절 수치는 S01 기본값(50bp·300초·3회·이월 1회)과 동일
7. 패키지 `trading/auto_buy_us/`, Migration `abu0001`, 문서 `docs/auto_buy_us_shadow.md`
8. WebSocket 1단계 미사용
