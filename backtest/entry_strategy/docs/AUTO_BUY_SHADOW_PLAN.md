# account1 미국 Scanner 자동매수 SHADOW 구현 Plan (P8, 검토안)

2026-11-25 작성. **계획 문서**이며 코드·운영 VM·운영 DB·저널·Migration 을 바꾸지 않았다.
이 Plan 의 구현, 운영 DB 스냅샷 표 추가, 배포는 각각 별도 승인 대상이다. 실제 주문·취소 권한과 자동매수 정책은 승인되지 않았다.
연동 설계: `AUTO_BUY_S01_LINK_DESIGN.md` (v2). 근거 백테스트: P0~P7 (`entry_strategy_backtest_report.html`).

## 0. 목표와 비목표

- **목표:**
  - 앞으로 생기는 운영 Scanner 신호로 B00·B00-N·B01·B02 를 같은 조건의 독립 가상 계좌에서 병행 평가한다. 이를 통해 RECONSTRUCTED 백테스트의 한계(생존 편향, 재현 신호, 이미 본 Test 구간)를 넘는 전향적 증거를 모은다.
  - 위험 예산 정책(Entry/Current Heat, 손절가 없는 보유, 손절선 이탈)이 실제 계좌 상태에서 어떻게 작동하는지 관찰한다.
- **비목표:**
  - 실주문
  - S01 운영 환경(VM SHADOW, Codex 담당) 변경
  - 기존 NOT_RECOMMENDED 판정 변경
  - 새 점수제나 파라미터 탐색

## 1. 전향적 불변 신호 스냅샷

### 1.1 표 `scanner_signal_snapshots` (운영 DB Alembic, 승인 S1)
INSERT 만 한다. UPDATE·DELETE 경로를 두지 않는다. DB 권한 분리가 가능하면 앱 역할에서 UPDATE 를 회수한다.

| 열 | 내용 |
|---|---|
| `id` | PK |
| `scan_log_id` | `scan_logs.id` (스캔 실행) |
| `scan_result_id` | `scan_results.id` (그 시점 행. 이후 재스캔으로 바뀌어도 스냅샷은 그대로) |
| `signal_key` | 운영 signal_key (market·ticker·signal_type·signal_date) |
| `market`, `ticker`, `signal_type`, `signal_date` | |
| `scan_completed_at` | 스캔 완료 시각 (UTC). **매수 판단은 이 시각 이후 데이터만** |
| `first_detected_at` | 운영 `scan_results.first_detected_at` (그 시점 값 복사) |
| `is_first_detection` | 이 signal_key 의 첫 스냅샷인지 |
| `strict_passed`, `strict_reasons` | Strict 판정과 사유 원문 |
| `is_first_strict_pass` | 이 signal_key 의 첫 Strict 통과 스냅샷인지 |
| `grade`, `signal_quality`, `market_condition` | 그 시점 값 |
| `decision_price` | 매수 판단 기준가 (스캔 시점 종가) |
| `pivot_price`, `stop_loss` | 그 시점 값. **이후 손절 기준은 이 값** |
| `entry_warnings` | 경고 원문 (JSON) |
| `rs_value`, `volume_ratio`, `weekly_volume_ratio`, `ext_pct`, `stop_pct` | 그 시점 값 |
| `scanner_commit`, `threshold_hash` | 코드 커밋 해시, 임계값(config) 해시 |
| `created_at` | |

- UNIQUE(`scan_log_id`, `signal_key`). 같은 스캔 안에서 중복을 막고, 재스캔하면 **새 행**이 생긴다.
- 기록 위치는 `scanner/scan_engine._save` 직후다. 같은 트랜잭션 안에서 처리하고, 실패해도 스캔은 계속하되 `snapshot_gap` 을 집계한다.
  Strict 거부 행도 남긴다. MCP 에 보이지 않는 거부 신호도 이력으로 남겨 늦은 Strict 통과를 판정하기 위해서다.
- 스캐너 표시·Gate·매매 판정 로직은 바꾸지 않는다. 기록만 추가한다.

### 1.2 매수 적격 판단 기록 `auto_buy_eligibility` (자동매수 SHADOW 저널, 승인 S2)
- 스냅샷 1행 × 전략 1개마다 한 행을 만든다: `snapshot_id`, `strategy_id`, `strategy_version`, `config_hash`, `eligible`, `exclusion_reasons[]`.
- B01 제외 사유 코드 (P0~P7 정의 그대로):
  - `NOT_STRICT`
  - `MARKET_NOT_BULL`
  - `LATE_STRICT_PASS` (첫 Strict 통과가 첫 탐지가 아님)
  - `BELOW_PIVOT` (BREAKOUT/RE_BREAKOUT, 피벗 대비 < 0%)
  - `CHASE_WARNING` (운영 entry_control 추격 경고)
  - `STOP_GT_15`
  - REBOUND 는 피벗 관련 조건을 적용하지 않는다.
- 제외하지 않는 것: STRONG 아님, Grade A 아님, 손절폭 > 7%, 주봉 거래량 경고, 돌파 실패 판정 보류.

## 2. 독립 가상 계좌

| 계좌 | 전략 | 후보군 | 순위 | 하루 |
|---|---|---|---|---|
| `V_B00` | B00 | Strict 통과 전체 | Grade → Quality | 제한 없음 |
| `V_B00N` | B00-N | 같음 | 같음 | 최대 2 |
| `V_B01` | B01 | 1.2 의 B01 적격 | Grade → Quality | 최대 2 |
| `V_B02_s{1..K}` | B02 | B01 과 같은 후보군 | 무작위 (고정 시드 K개, 권장 K=20) | 최대 2 |

- 모든 계좌는 초기자금 $50,000 으로 시작하고, 같은 위험 설정(권장: 균형형 0.75% / Entry Heat 4% / 종목 15% / 현금 하한 20%)과 같은 진입·청산 규칙을 쓴다.
  보수형 세트를 같은 구조로 병행할 수 있다 (선택. 계좌 수가 두 배가 된다).
- 실제 account1 의 수동 보유·현금은 **가상 계좌에 섞지 않는다**. 대신 실제 계좌 상태로 "오늘 LIVE 였다면 차단되었을 사유" (손절가 없는 보유 한도, 손절선 이탈, Heat)를 별도 열 `live_gate_would_block` 에 기록한다.
- 가상 체결 모델 (일봉 백테스트 T1_100 과 같게):
  - 다음 정규장 시가가 손절가 이하이거나 R < 3% 면 전송하지 않는다.
  - 시가 ≤ 지정가 상한이면 시가에 체결된다.
  - 장중 저가 ≤ 지정가면 지정가에 체결된다.
  - 이 밖의 경우 미체결이며 추격하지 않는다.
  - 부분 체결은 모델링하지 않는다. 체결 판정은 장 마감 후 일봉으로 확정한다.
  - 장중 실시간 시세를 쓰는 판정 변형은 2단계에서 별도 열로 둔다.
- 가상 S01: 고정 손절가 기준이며 기간 제한이 없다.
  - 갭으로 손절가 아래에서 시작하면 시가에 매도한 것으로 처리한다.
  - 장중 저가 ≤ 손절가면 손절가에 매도한 것으로 처리한다.
  - 같은 날 매수·손절이 겹치면 보수 가정을 쓰고, 대안 가정 열을 함께 남긴다.
- 모든 가상 체결 행에 `is_virtual=1`, `fill_model='DAILY_T1_100_v1'` 을 둔다. 실제 체결 표와 **물리적으로 다른 파일**에 저장한다.
- 전략·파라미터는 평가 시작일에 `strategy_version` 으로 고정한다. 변경하면 새 버전으로 새 계좌를 열고, 기존 계좌는 그대로 계속하거나 종료한다. 기존 계좌의 수치를 덮어쓰지 않는다.

## 3. 운영 모드 `AUTO_BUY_US_MODE`

| 값 | 동작 |
|---|---|
| `OFF` (기본) | 아무것도 하지 않음. 스냅샷 기록(1.1)은 별도 스위치 `SCANNER_SNAPSHOT_ENABLED` |
| `SHADOW` | 스냅샷 → 적격 판단 → 가상 계좌 결정·가상 체결·가상 S01 → 일일 보고. **주문 모듈 import 금지** |
| `LIVE` | 이번 Plan 범위 밖. A1~A12 승인, SHADOW 판정, AUTO_STOP LIVE 정상, 기존 주문 스위치가 모두 충족될 때만. 기본 비활성 |

- SHADOW 는 `trading/auto_buy/shadow/` 패키지로 분리한다. 테스트로 `trading.kiwoom_orders`, `trading.scanner_buy_orders`, `trading.auto_stop.brokers` 의 주문 함수가 import 되지 않음을 검증한다 (import 그래프 검사).
- 시세는 기존 읽기 전용 일봉 공급원(스캐너와 같은 데이터)만 사용한다. 키움 API 를 호출하지 않으므로 S01 호출량에 영향이 없다.
- 실행은 APScheduler 에 미국 마감 후 1회 job 으로 한다 (`US_SCHEDULE_TIMES` 스캔 완료 이후). S01 job·lease 와 공유하지 않는다.
- 저장은 별도 SQLite `AUTO_BUY_SHADOW_JOURNAL_PATH` 에 한다. 운영 DB 와 S01 저널과 분리한다 (A12). 스키마는 자동매수 저널 전용 Migration 으로 배포 작업에서만 실행한다.
- 자동손절 운영 환경(`AUTO_STOP_US_*`, VM SHADOW 저널)은 읽지도 쓰지도 않는다. LIVE 단계에서만 헬스를 읽는다.

## 4. 구현 단위 (승인 후, 순서대로)

| 단계 | 산출물 | 테스트 |
|---|---|---|
| I1 | 운영 DB Alembic: `scanner_signal_snapshots` + `_save` 직후 INSERT (스위치 기본 off) | 재스캔해도 기존 행 불변, 첫 탐지·첫 Strict 플래그, 거부 행 기록, 스위치 off 일 때 무동작 |
| I2 | 자동매수 SHADOW 저널 Migration (`virtual_accounts`, `auto_buy_eligibility`, `auto_buy_decisions`, `virtual_orders`, `virtual_fills`, `virtual_positions`, `risk_reservations`, `daily_reports`) | Migration 왕복, 런타임 DDL 없음 |
| I3 | 적격 판단 엔진 (P7 `strategies.pool_masks` 정의를 순수 함수로 이식) | 백테스트 후보와 같은 입력에서 같은 판정 (골든 테스트) |
| I4 | 위험 예산 함수 `auto_buy_risk` (Entry/Current Heat, 예약, 이탈, 손절가 없는 보유) — 운영 `scanner.sizing` 은 호출만 | P7 시뮬레이터와 같은 결과 (교차 검증) |
| I5 | 가상 계좌 엔진 (결정 → 가상 체결 → 가상 S01, 멱등 일일 처리) | 같은 날 재실행해도 결과 불변, 누락일 보충 처리 |
| I6 | 일일 보고 (사이트 읽기 전용 화면 + Slack 요약, 실제 주문 표시와 시각 구분) | 가상 표기 누락 시 실패 |
| I7 | `AUTO_BUY_US_MODE` 스위치, 기본 OFF, import 격리 테스트 | 주문 모듈 import 금지 |

배포는 Codex 운영 절차(백업 → Migration → 기본 OFF 배포 → 스위치 SHADOW)를 따른다. Claude Code 는 배포하지 않는다.

## 5. 검증 일정과 기준

| 시점 | 확인 | 통과 기준 | 미달 시 |
|---|---|---|---|
| 최초 5거래일 | 기능·안전성 | 스냅샷 누락 0, 재스캔 덮어쓰기 0, 가상 표기 100%, 주문 모듈 호출 0, S01 지표(tick 지연·429) 변화 없음 | 중지·수정 후 재시작 (평가 기간 리셋) |
| 20거래일 | 신호·가상 체결·위험 예산 | 적격 판정을 백테스트 로직으로 재계산하면 100% 일치. 가상 체결을 일봉으로 재계산하면 일치. Heat·예약·차단 사유 집계 일관 | 원인 기록, 버전 올려 재시작 |
| 60거래일 이상 | 전향적 성과 중간 평가 (채택 판단 아님) | 전략별 거래 수·R·PF·MDD·활용률·차단 사유, B01 의 B02 분포 내 위치 | 기간 연장 |
| 표본 충분 | 채택 검토 | 6절 | — |

## 6. 채택 검토 기준 (평가 시작 전 고정)

- 최소 표본: B01 가상 계좌의 **청산 + 미청산 거래 40건 이상**, 그리고 평가 기간 6개월 이상. 미달이면 기간을 연장한다.
  P7 균형형 기준 B01 신규 매수는 운영형(기간 제한 없는 S01)에서 **연 약 28건**이다 (H60 기준은 연 약 74건). 따라서 40건에 **약 1.5년**이 걸린다. 60거래일·6개월 시점의 평가는 중간 점검일 뿐이다.
- 비교 (같은 위험 설정):
  1. B01 의 평균 R, PF 가 B00-N 이상
  2. B01 수익률이 B02 무작위 K개 분포의 75 백분위 이상 (순위·필터 효과 확인)
  3. MDD 악화 2%p 이내
  4. 차단 사유 중 데이터·시스템 오류 0건
- 기간 제한 없는 S01 특성상 미청산 평가손익의 비중이 크다. 그래서 **청산 거래만의 R** 과 **미청산 포함 R** 을 따로 보고, 한쪽만 좋으면 채택하지 않는다.
- 위 기준을 통과해도 LIVE 는 별도 승인이며, A1~A12 결정과 S01 LIVE 정상 운영이 전제다.

## 7. 승인 필요 항목 (이 Plan 고유)

| ID | 항목 | 비고 |
|---|---|---|
| S1 | 운영 DB 에 `scanner_signal_snapshots` 추가 (Alembic) | 운영 DB 변경. 스캐너 `_save` 경로 수정 |
| S2 | 자동매수 SHADOW 전용 SQLite·Migration | S01 저널과 분리 |
| S3 | `AUTO_BUY_US_MODE` 스위치와 SHADOW job 배포 (기본 OFF) | Codex 배포 |
| S4 | 가상 계좌 구성 (균형형 단독 또는 보수형 병행, B02 시드 수 K) | |
| S5 | 채택 검토 기준(6절) 확정 | 평가 시작 전에 고정 |

연동 설계의 A1~A12 는 LIVE 단계 항목이다. SHADOW 에는 A6·A9·A10·A11 의 **정책 값**(관찰용 `live_gate_would_block` 계산)과 A12(저널 위치)만 필요하다.

## 8. 한계

- 가상 체결은 일봉 기준이다. 장중 시세 유효성, 호가 공백, 부분 체결, 주문 거부를 재현하지 않는다.
- 주문 직전 실시간 확인(시장 상태, 시세 나이·급변)은 SHADOW 에서 장 마감 후 일봉 값으로 대체한다. 실제 LIVE 에서는 더 많이 차단될 수 있다.
- B02 무작위 시드 K개는 백테스트의 1,000회보다 분포가 거칠다. 기간 말에 저장된 스냅샷으로 1,000회를 재계산해 보완한다 (입력이 불변이므로 가능).
- 전향적 기간의 시장 국면이 한쪽으로 치우칠 수 있다. 시장 상태별로 나눈 결과를 함께 본다.
