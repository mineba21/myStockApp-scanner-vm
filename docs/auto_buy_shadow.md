# account1 미국 Scanner 자동매수 SHADOW

2026-11-25 작성, 같은 날 재리뷰 지적(P1 3건·주요 P2) 반영, 남은 운영 정책 확정 (아래 '운영 정책').
같은 날 Draft PR #14 독립 리뷰(FAIL: P1 6건·P2 7건)를 feature branch 에서 반영했다 (아래 '독립 리뷰 반영'). 공개 이력 항목은 이력 재작성 승인이 필요해 보류.
2026-11-26 Codex 재리뷰(2차, FAIL: 신규 P1 1건·P2 3건)를 같은 브랜치에서 반영했다 (아래 'Codex 재리뷰(2차) 반영').
같은 날 Codex 최종 재리뷰(신규 P1 1건·P2 2건·S5 이력)를 반영했다 (아래 'Codex 최종 재리뷰 반영'). SHADOW 활성화는 계속 보류다.
- **승인 범위:** 로컬 구현·테스트 (사용자 승인: P7/P8 검토 후 I1~I7 SHADOW 구현, 이후 P1·P2 정합성 보완), feature branch `feature/auto-buy-shadow` 커밋·Push·Draft PR.
- **아직 하지 않은 것:** main 병합, VM 배포, 저널 Migration 실행. Codex 재리뷰를 통과하기 전에는 기본 OFF 배포도, SHADOW 활성화도 하지 않는다.
- **판정은 그대로 유지한다.**
  - 기존 NOT_RECOMMENDED
  - B01 = NEED_MORE_DATA (전향적 SHADOW 검증 대상만)
- **근거:** `backtest/entry_strategy` P0~P7 보고서, `backtest/entry_strategy/docs/AUTO_BUY_SHADOW_PLAN.md`, `AUTO_BUY_S01_LINK_DESIGN.md` v2
- **저널 스키마:** `ab0001`(공개 후 동결) + `ab0002`(거래량, 2026-11-26, 동결) + `ab0003`(거래량 출처·근거, run fencing, 2026-11-26). 최종 목표 리비전은 `ab0003`. 아래는 `ab0001` 의 공개 전 이력이다 — 배포·커밋 전이라 첫 재리뷰 반영 때 같은 리비전을 **제자리에서 교체**했다 (run 단위 상태, 거래일 입력·일봉 표 추가).
  - 확인(2026-11-25): Git 이력에 `trading/auto_buy` 커밋 없음(원격도 main 뿐), 로컬 `.env` 저널 경로 미설정, 프로젝트 내 SQLite 에 `ab_alembic_version` 없음. 이전 `ab0001` 이 적용된 파일은 개발 세션 임시 폴더의 데모·벤치 파일뿐이다. 운영 VM 은 Git 으로만 배포되고 이 코드가 Push 된 적이 없어 적용 이력이 없다 (VM 에 접속해 확인하지는 않았다).
  - 이 브랜치가 커밋·Push 된 이후에는 `ab0001` 을 바꾸지 않고 새 리비전으로만 바꾼다.

## Codex 최종 재리뷰 반영 (PR #14, 2026-11-26, head 7ef619b)

재리뷰 결과: 테스트·실제 재구성 데이터 정합성은 통과했다. 실제 데이터 수집 경로에서 신규 P1 1건, P2 2건, S5 이력 집계 보완이 나왔고, SHADOW 활성화 보류를 수용한다.
스키마는 새 리비전 `ab0003` 으로만 늘렸다. `ab0001`·`ab0002` 는 바꾸지 않았다.

| 등급 | 지적 | 반영 | 회귀 테스트 |
|---|---|---|---|
| P1 | yfinance 가 Yahoo `volume:null` 을 0 으로 바꿔 미확인 거래량이 '0 확정'으로 처리됨 | 수집 단계부터 원본 응답으로 세 상태를 구분하고 출처·근거를 저장. 아래 '거래량 원본 수집' | `tests/test_auto_buy_shadow_review4.py` — 원본 응답 주입으로 실제 다운로드·파싱 경로 전체 |
| P2 | heartbeat 가 늦은 살아 있는 작업자를 FAILED 처리할 수 있음 | 사망 근거가 있을 때만 정리, 응답 없는 작업자는 차단·경고. DB fencing. 아래 '재계산 실행권 v2' | `tests/test_auto_buy_shadow_review4_lease.py` — 실제 별도 프로세스 관찰자·SIGKILL |
| P2 | 이미 최신인 저널의 권한을 확인하지 않음 | `--check` 권한 검증, 적용 모드 0600 보장. 아래 'Migration 파일 권한' | `tests/test_auto_buy_shadow_review4_migration.py` |
| S5 | 재계산 복구 뒤 위반 이력이 사라짐 | 현재 미해결·관찰 기간·복구됨·복구 불가 분리, 연속 5거래일 평가. 아래 'S5 이력 집계' | `tests/test_auto_buy_shadow_review4_s5.py` |
| 보안 | 테스트 픽스처 `ACCT` 가 실계좌 관측값과 비슷한 비율 | 실계좌와 무관한 합성 값(순자산 100,000·현금 40,000·보유 5,000)으로 교체하고 기대값 재계산 | `tests/test_auto_buy_shadow.py` |

### 거래량 원본 수집 (P1)

- **수집 경로:** 종목마다 Yahoo v8 chart 원본 JSON 을 한 번 받는다. yfinance 의 HTTP 세션(`yfinance.data.YfData`)만 쓰고 DataFrame 변환은 쓰지 않는다. 같은 응답에서 OHLC 와 거래량을 함께 읽으므로 서로 다른 응답을 합치지 않는다. 다른 종목의 응답(심볼 불일치)이나 다른 거래일 행은 쓰지 않는다.
- **조정:** yfinance `auto_adjust` 와 같은 식을 같은 응답에 적용한다 (ratio = adjclose ÷ close, O·H·L × ratio, C = adjclose). 공급자 이름은 `yahoo_chart_raw_v8_auto_adjust`.
- **상태 판정:**

  | 원본 응답 | 상태 | 처리 |
  |---|---|---|
  | 정상 OHLC + `volume` > 0 | `POSITIVE_VOLUME` | 기존 체결 규칙 |
  | 정상 OHLC + `volume: 0` | `CONFIRMED_ZERO_VOLUME` | 매수·매도 체결 없음, D+1 이면 `ZERO_VOLUME_EXPIRED` |
  | `volume: null`, 거래량 필드 없음, 배열 길이 부족, 음수·숫자 아님 | `UNKNOWN_VOLUME` | 마감 후 `INVALID/UNKNOWN` → `FILL_UNKNOWN`·`DATA_INCOMPLETE`, 복구되면 재계산 |
  | 응답 오류·결과 없음·심볼 불일치·그날 행 없음·중복 행·OHLC null·조정 종가 없음 | 결측(일봉 없음) | 기존 결측·복구 정책 |

- **0 확정 조건:** 출처가 `yahoo_chart_raw` 일 때만 0 을 확정 0 으로 본다 (`vfills.CONFIRMED_ZERO_SOURCES`). 변환된 DataFrame(`yfinance_dataframe`)이나 출처가 없는 기존 행의 0 은 `UNKNOWN` 이다.
- **저장:** `ab_bars.volume_source` 에 출처, `ab_bars.source_evidence` 에 근거를 JSON 으로 남긴다. 근거에는 원본 값(null 보존), 타임스탬프, 조정 비율, 거래량 필드 유무, 응답 SHA-256 이 들어간다. 출처를 확인할 수 없는 0 은 마감 후 `INVALID` 로 저장하고 `recover_missing` 이 원본 경로로 다시 확인한다.
- 기존 정책은 그대로다: D+2 이월·재매수 없음, 보유 위험 유지, 복구 시 새 run 재계산.
- yfinance 버전은 바꾸지 않았다.
- **한계:**
  - 이미 저장된 재구성 OHLCV 캐시(백테스트)는 변환된 값이라 원본 null 여부를 되살릴 수 없다.
  - P7-OPS 정합성 하네스는 `vfills.assume_confirmed_zero("backtest_reconstructed_cache")` 안에서만 캐시 0 을 확정 0 으로 가정한다. 그 가정은 보고서의 `zero_volume_source_assumption` 에 기록된다.
  - 이 대조는 **엔진 정합성**만 보여 준다. 원본 데이터 정확성이나 과거 성과를 새로 검증한 것이 아니다.

### 재계산 실행권 v2 (P2)

- **실행권 식별자:** `ab_runtime.replay_in_progress` 에 다음을 기록한다. 스키마 변경 없이 JSON 이다.
  - pid, 호스트, 부트 식별자(Linux `/proc/sys/kernel/random/boot_id`, macOS `kern.boottime`)
  - 프로세스 시작 식별자 (psutil → `/proc/<pid>/stat` → `ps -o lstart=`)
  - 프로세스 토큰, heartbeat
- **heartbeat:** 거래일마다, 일봉 조회 직후, 계좌마다 갱신한다. 그래서 하루 처리가 길어도 늦어지지 않는다. 갱신 쓰기가 실패하면(DB 잠김 등) 경고 로그를 남기고 계속한다. 실행권을 잃었을 때만 멈춘다 (`ReplayLeaseLost`).
- **상태 판정 (`classify_lease`):**

  | 상태 | 조건 | 처리 |
  |---|---|---|
  | OWN | 이 프로세스의 토큰 | — |
  | ALIVE / ALIVE_UNVERIFIED | 같은 호스트에서 pid 가 살아 있고 시작 식별자가 같음 (확인 불가면 UNVERIFIED), heartbeat 15분 이내 | 보호, 새 재계산 대기 |
  | STALLED / STALLED_UNVERIFIED | 같은 조건에서 heartbeat 15분 초과 | **자동 탈취하지 않음.** 대기, WARN `replay_stalled` 1회, S5 운영 위반, 화면에 '응답 없음' 표시 |
  | REMOTE_ACTIVE / REMOTE_STALE | 다른 호스트 | 사망을 확인할 수 없어 정리하지 않음 (오래되면 STALE 로 표시) |
  | DEAD | 부팅 식별자가 다름(재부팅), pid 없음, pid 재사용(시작 식별자가 다르거나 이 프로세스가 같은 pid 를 다른 토큰으로 가짐) | BUILDING run 을 `FAILED`(`ABANDONED_BUILD`, `death_evidence`)로 바꾸고 ERROR 이벤트를 남긴 뒤 재시도 |

- **lease 만료와 프로세스 사망을 구분한다.** heartbeat 지연만으로는 사망으로 보지 않는다. 응답 없는 작업자는 운영자가 확인한 뒤 `inputs.release_stalled_lease(db, run, note)` 로만 해제한다 (`FAILED`, `ABANDONED_MANUAL`, ERROR 이벤트).
- **DB fencing (`ab0003` 트리거):**
  - 다음 표의 INSERT/UPDATE 는 run 이 `BUILDING`·`CURRENT` 일 때만 허용한다: `ab_account_runs`, `ab_decisions`, `ab_orders`, `ab_reservations`, `ab_positions`, `ab_stop_events`, `ab_daily`, `ab_days`. `ab_fill_ledger` 는 주문의 run 기준이다.
  - `FAILED`·`SUPERSEDED` 는 다른 상태로 바뀌지 않는다.
  - `CURRENT` 는 하나만 허용한다.
  - 실행권을 잃은 이전 작업자는 파이썬 확인을 건너뛰더라도 실패 run 에 쓸 수 없고, 새 CURRENT 를 덮어쓸 수 없다. 트리거 거부는 `ReplayLeaseLost` 로 바꿔 멈춘다.
  - CURRENT 교체 직전에 같은 트랜잭션 안에서 실행권과 토큰을 다시 확인한다.
- CURRENT run, 실패 run 과 감사 이력은 지우지 않는다.

### Migration 파일 권한 (P2)

- **`--check`:** 저널과 존재하는 `-journal`/`-wal`/`-shm` 의 권한이 0600 이 아니면 `CHECK_FAILED_PERMISSIONS`(종료 코드 2)이다. 이미 최신이어도 확인한다. 권한·데이터는 바꾸지 않고 백업·보고서도 만들지 않는다.
- **적용 모드:** 새로 만들 때, upgrade 할 때, `ALREADY_CURRENT` 일 때 모두 0600 으로 맞추고 다시 확인한다. chmod 실패나 확인 불일치는 종료 코드 4 이며 성공으로 반환하지 않는다. 백업 사본과 보고서(`O_EXCL|O_NOFOLLOW` 0600)도 확인한다. 실행 중에는 `umask 077` 이다.
- **chmod 전 검증 (SQLite 로 열기 전):** 아래 중 하나라도 해당하면 chmod 하지 않고 중단한다. 임의 파일과 S01 저널에는 chmod 하지 않는다.
  - 저널 경로가 심볼릭 링크
  - 부속 파일이 심볼릭 링크이거나 일반 파일이 아님
  - 소유자가 실행 사용자가 아님
  - 하드링크가 2개 이상
  - 운영 주문 저널(`SCANNER_ORDER_JOURNAL_PATH` = S01 저널)이나 ETF 저널과 같은 파일
  - `ab_` 이외의 객체가 있음

### S5 이력 집계

- **관찰 기간 위반** (= S5 안전 위반 수, 기준 0 유지): 검증 기간에 결과로 쓰인 모든 run(`CURRENT`·`SUPERSEDED`)을 센다. DATA_INCOMPLETE 거래일, FILL_UNKNOWN 주문, 미확정 포지션은 같은 거래일·같은 신호를 한 번만 센다. 그 밖에 ERROR 이벤트, 중단·복구 불가·미수집 스캔, 복구 기간이 지난 결측 일봉, 운영 상태(비가상 행·heartbeat 지연·버전 차단·재계산 비정상 종료·응답 없음)를 센다. **재계산으로 복구돼도 지우지 않는다.**
- **현재 미해결:** 지금 남아 있는 것 (현재 run 기준).
- **복구됨:** 관찰 기간에 있었으나 지금은 해소된 것. 복구 성공은 무오류 운영이 아니다. 복구됐어도 안전 통과로 보지 않는다.
- **복구 불가:** 복구 불가 스캔, 복구 기간이 지난 결측 일봉.
- **마감 전 재시도:** 데이터 마감 전에 기다렸다가 정상으로 확정된 날은 위반이 아니다. runtime `pre_cutoff_waits` 에 기록하고 따로 표시한다. 마감 뒤 DATA_INCOMPLETE 는 위반이다.
- **연속 정상 운영:** 거래일마다 처음 처리한 결과로 판정한다. 정상이려면 모두 해당해야 한다:
  - 처음 처리 결과가 COMPLETE
  - 그날 결정 스캔의 중단·복구 불가가 없음
  - 처리 지연이 없음 (데이터 마감 + 30분 이후 처리는 runtime `late_processing` 에 기록)
  - 그 처리 구간에 ERROR 이벤트가 없음
- **5거래일 기능·안전성:**
  - 최초 5거래일이 모두 정상이어야 통과다 (`initial_window`).
  - 위반이 있으면 기록을 남기고, 그 이후 연속 5거래일 정상 운영을 따로 평가한다 (`current_clean_streak`, `functional_pass`).
- 성과 기준과 판정(NOT_RECOMMENDED, B01 = NEED_MORE_DATA)은 바꾸지 않았다.

## Codex 재리뷰(2차) 반영 (PR #14, 2026-11-26)

재리뷰 결과(head 10a51e5): 기존 P1 6건 해결, 신규 P1 1건·P2 3건 → SHADOW 활성화 FAIL 판정을 수용하고 같은 feature branch 에서 고쳤다.
`ab0001` 은 이미 공개됐으므로 바꾸지 않고 새 리비전 `ab0002` 로만 스키마를 늘렸다 (아래 'Migration').

| 등급 | 지적 | 반영 | 회귀 테스트 |
|---|---|---|---|
| P1 | 거래량을 받지 않아 거래량 0 봉(O=H=L=C)에서 가상 체결 (SW 2021-04-19) | 거래량 수집·저장. 아래 '거래량 정책' | `tests/test_auto_buy_shadow_review3.py` SW 재현, NULL·음수·NaN, D+1 만료·D+2 이월 없음, 거래량 0 보유 손절 |
| P2 | 46 계좌 처리 후 일 요약 저장 전 중단 → 재실행 시 FILL_UNKNOWN 종목이 요청에서 빠지고 COMPLETE·결측 0 기록 | 필요 종목에 그날 목표 주문(모든 상태)·그날 청산·미확정 포지션 포함. 일 완료 판정 `day_completeness` | 요약 직전 중단·재실행, 미해결 상태가 남은 날 COMPLETE 금지 |
| P2 | 재계산 중 프로세스 종료 → BUILDING 잔존, 화면이 계속 '재계산 중' | 재계산 실행권(lease)·하트비트·비정상 종료 정리. 아래 '재계산 실행권' | 중단 재현·정리·재시도, 다른 작업자 실행 중 보호, 실행권 상실 시 CURRENT 교체 없음 |
| P2 | S5 안전 집계에 ABANDONED·UNRECOVERABLE·DATA_INCOMPLETE·FILL_UNKNOWN 누락 | 운영 안전·데이터 완전성 두 묶음으로 집계. 기준(위반 0)은 그대로 | 각 항목 집계·5거래일 검증 중 통과 아님 |
| — | P7 비교 | 기존 P7 은 그대로 두고 운영 정책 비교판 P7-OPS 를 따로 작성, 실제 1,480거래일 23 계좌 전체 대조 | `backtest/entry_strategy/tests/test_p7_ops_parity.py` |

### 거래량 정책

- 거래량 상태는 셋이다: `POSITIVE`(> 0), `ZERO`(0 확정), `UNKNOWN`(없음·NULL·음수·NaN·숫자 아님). 0 과 미확인을 섞지 않는다.
- **정상 OHLC + 거래량 > 0:** 기존 체결 규칙. 거래량이 있어도 지정가 체결을 보장한다고 보지 않는다 (일봉 근사 한계 그대로).
- **거래량 0 확정:** 가상 매수·매도 체결을 만들지 않는다.
  - 데이터 마감 전 거래량 0 은 잠정치로 보고 저장하지 않고 다시 받는다. 마감 후에도 0 이면 `OK/ZERO` 로 저장한다 (결측 아님, 그날 `ZERO_VOLUME` 경고 이벤트).
  - 매수 주문: 유효기간은 다음 정규장 1일 그대로다. D+1 이 거래량 0 이면 미체결 만료(`UNFILLED`, 사유 `ZERO_VOLUME_EXPIRED`), 예약 해제. D+2 이월·같은 신호 재매수 없음.
  - 보유 포지션: 손절 체결 없음. 마지막 종가는 갱신해 평가하고 `zero_volume_days`·`last_zero_volume_day` 를 늘린다. 진입 위험을 유지하고 손절선 아래 종가면 `STOP_BREACHED_OPEN` 으로 신규 매수를 막는다.
  - **거래 재개 후 손절 정책:** 재개 첫 정상 봉에 기존 S01 규칙 그대로 — 시가 ≤ 손절가면 시가 청산(`STOP_GAP`), 저가 ≤ 손절가면 손절가 청산. 손절 이벤트에 `zero_volume_history`(일수·마지막 0 거래일·재개 첫 봉)와 `gap_loss_vs_stop_usd` 를 남긴다.
  - **갭 위험:** 거래가 없던 동안 손절가 아래로 내려간 가격은 재개 시가에서 한꺼번에 실현된다. 손실이 진입 위험(1R)을 넘을 수 있고 SHADOW 는 이를 그대로 기록한다 (보정하지 않음).
- **거래량 미확인:** 데이터 불완전으로 본다. 기존 데이터 마감·결측 복구 정책을 그대로 따른다 — 마감 전 대기, 마감 후 `INVALID/UNKNOWN`(근거용 OHLC 는 남김) → 주문 `FILL_UNKNOWN`·포지션 미확정 → 20 거래일 안에 확인되면 새 run 재계산.
- ab0002 이전에 저장된 행(거래량 열 NULL)은 미확인으로 본다.
- 체결 ledger 에 `bar_volume` 을 남긴다.

### 지정가 단위

- 지정가 = 판단가 × 1.01 을 **센트 내림**한다 (`Decimal`, `ROUND_FLOOR`). 반올림은 +1% 상한을 넘을 수 있어 바꿨다 (예: 100.005 → 반올림 101.01 > 101.00505, 내림 101.00). 운영 주문 경로의 미국 지정가 형식(소수 2자리)과 같다.

### 일 완료 판정 (`ab_days`)

- 거래일 D 를 `COMPLETE` 로 기록하려면 모두 0 이어야 한다: 확정되지 않은 일봉, 거래량 미확인, D 까지 목표일인 `FILL_UNKNOWN` 주문, 미확정 포지션, 처리되지 않았거나 불완전한 계좌 결과. 하나라도 남으면 `DATA_INCOMPLETE` 이고 notes 에 항목별 건수를 적는다. 거래량 0 확정은 완료를 막지 않는다 (notes 에 `ZERO_VOLUME` 건수).
- `bars_missing` 은 확정되지 않은 필요 종목 수다 (FILL_UNKNOWN 종목 포함).
- 이미 기록된 `ab_days` 행은 다시 쓰지 않는다 (재실행 시 그대로 반환).

### 재계산 실행권 (BUILDING 복구)

- BUILDING run 생성과 실행권(`ab_runtime.replay_in_progress`: run_id·host·pid·프로세스 토큰·heartbeat) 기록을 한 `BEGIN IMMEDIATE` 트랜잭션에서 한다. 살아 있는 실행권이 있으면 새 재계산을 시작하지 않는다 (`ReplayBusy`, job 은 대기).
- 재계산은 거래일마다 heartbeat 를 갱신하고, 실행권을 잃었으면 멈춘다 (`ReplayLeaseLost`, ERROR 이벤트). CURRENT 교체 트랜잭션 안에서 실행권 소유를 다시 확인하고, 교체는 한 트랜잭션에서 원자적으로 한다.
- ~~살아 있음 판단: heartbeat 15분 초과면 비정상 종료로 봄~~ → **최종 재리뷰에서 바뀜.** 위 '재계산 실행권 v2' 를 따른다 (사망 근거가 있을 때만 정리, 응답 없음은 차단·경고).
- **정리:** job 시작 때 죽은 실행권의 BUILDING run 을 `FAILED`(요약 사유 `ABANDONED_BUILD`)로 바꾸고 ERROR 이벤트(`replay_abandoned`)를 남긴 뒤 실행권을 지운다. CURRENT run 은 그대로다. 실패 run·감사 이력은 지우지 않는다. 다음 `replay_needed` 처리에서 다시 재계산한다.
- 화면·health 는 `RUNNING`(다른 작업자 진행 중)과 `ABANDONED_SUSPECTED`(비정상 종료 의심)를 구분한다. 의심 상태는 '재계산 중'으로 보이지 않고 S5 운영 안전 위반으로 센다.

### S5 안전 집계

- 기준(위반 0)은 바꾸지 않았다. 집계 항목만 늘리고 두 묶음으로 나눴다.
  - **운영 안전:** 비가상 행, ERROR 이벤트, heartbeat 지연, 버전 차단, 재계산 비정상 종료 의심
  - **데이터 완전성:** 중단(ABANDONED) 스캔, 복구 불가(UNRECOVERABLE) 스캔, 미수집 스캔, 현재 run 의 DATA_INCOMPLETE 일, 미해결 FILL_UNKNOWN 주문, 미확정 포지션
- 합계가 0 일 때만 안전 통과다. 5거래일 검증 기간 중에도 중단·복구 불가가 하나라도 있으면 통과로 표시하지 않는다.
- 최종 재리뷰에서 이력 기준으로 바뀌었다 (위 'S5 이력 집계'). 현재 run 만 세면 복구 뒤 위반이 사라지므로 관찰 기간 전체를 센다.

### P7 운영 정책 비교판 (P7-OPS)

- 기존 P7 결과(`p7_*.csv`·보고서)는 바꾸지 않았다. `backtest/entry_strategy/src/p7_ops.py` 는 SHADOW 코드를 import 하지 않는 독립 구현이고, `ops_parity.py` 가 같은 데이터로 실제 SHADOW 엔진 경로(저널·일봉 저장·process_day)를 돌려 둘을 대조한다. 실행: `python -m backtest.entry_strategy.run p7ops` (결과는 `output/`, Git 제외).
- 맞춘 운영 정책: 센트 내림·+1% 상한, 유효기간 1일, 거래량 0·미확인, 비용(`P7_COMPARISON_v1`), 같은 신호 재매수 없음, S01 손절(Day-0 보수 가정·갭), Entry/Current Heat, 일봉 내 순서 가정. P7 체결 조건을 SHADOW 에 맞춰 완화하지 않았다.
- **기존 P7 과 다른 점 (`p7_ops.DIFF_VS_P7`):**

| 항목 | 기존 P7 | P7-OPS / SHADOW |
|---|---|---|
| 지정가 | 판단가 × 1.01 (반올림 없음) | 센트 내림 |
| D+1 거래량 0 | 거래량 0 봉을 건너뛰고 다음 거래량 있는 봉에서 체결 시도 | 미체결 만료, 이월 없음 |
| D+1 일봉 없음 | 다음 봉으로 넘어감 | FILL_UNKNOWN (복구 대기) |
| 보유 중 거래량 0 | 가격만 보고 손절 판정 | 손절 체결 없음, 재개 첫 봉에서 판정 |
| 보유 중 일봉 없음 | 다음 봉으로 넘어감 | 가격 미확정, 판정 보류 |
| 체결 시 현금 | 판단 때 예약 기준 | 판단 때 예약 기준 (동일, 명시) |
| Heat 기록 시점 | 일말 | 일말 (동일, 명시) |
| 마지막 날 판단 | 판단하지 않음 | 판단·예약 (다음 정규장 목표) |
| B02 순위 | numpy 순열 1,000회 | SHA 기반 seed 20개 (`random_rank`) |

- 기존 P7 의 거래량 0 건너뛰기에 해당한 D+1 은 실제 데이터에서 5건이다 (SW 2건, FER 3건). 그중 3건은 P7 이 다음 봉에서 체결했다 (예: SW 2021-04-16 판단 → 04-19 거래량 0 → P7 은 04-20 체결, 운영 정책은 미체결 만료).

**실제 재구성 데이터 대조 (2026-11-26, 2021-01-04 ~ 2026-11-23, 1,480 거래일, 후보 2,805건, P7C = BAL 프로필 23 계좌)**

- SHADOW 엔진 ↔ P7-OPS: **23 계좌 전부 일치.** 거래 3,545건의 진입·청산일, 수량, 체결가, 손절 사유, 미체결·만료 주문, 매일의 현금·예약·평가금액·Entry/Current Heat·보유 수를 비교했다. 최대 상대 차이 평가금액 3e-16, 현금 6e-16, Heat 절대 차이 7e-16 (부동소수점 수준).
- 데이터 특이점: 결측·NaN 행 0, 거래량 0 봉 2,513개(SW·FER·FERG·AMCR 등), OHLC 불일치 1행(허용 오차 안이라 정상 봉). 거래소 캘린더에는 거래일이지만 SPY 데이터에 없는 2025-01-09(임시 휴장)는 추가 휴장일로 넣었다.
- 거래량 0 결과: 미체결 만료 36건(23 계좌 합계), 거래량 0 날을 보유한 포지션 22건. DATA_INCOMPLETE 일 0.
- 기존 P7 과 P7-OPS 비교 (BAL·기간 제한 없는 S01·전체 구간). 차이는 위 표의 정책 차이 때문이다:

| 전략 | 기존 P7 거래 / 누적수익률 / MDD | P7-OPS 거래 / 누적수익률 / MDD |
|---|---|---|
| B00 | 231 / +55.1% / −11.3% | 216 / +55.4% / −11.6% |
| B00-N | 195 / +111.0% / −33.8% | 201 / +113.1% / −33.5% |
| B01 | 168 / +13.9% / −14.7% | 164 / +13.6% / −15.6% |
| B02 | 1,000회 순열 중앙값 +53.6% | seed 20개 중앙값 +52.5% (최소 +10.4%, 최대 +91.4%) |

- 기간 제한 없는 S01 에서 청산된 거래는 모두 손절이라 청산 기준 PF 는 양쪽 모두 0 이다 (미청산 평가 포함 PF 는 P7 보고서 참조).
- P7-OPS 에서도 B01 은 B00-N 보다 99.5%p 낮고 B02 seed 분포의 10 백분위다. **판정은 그대로다: NOT_RECOMMENDED, B01 = NEED_MORE_DATA.**
- **검증 범위:** 위 대조는 P7C 23 계좌(B00·B00-N·B01·B02 s01~s20)만이다. OPS 23 계좌는 과거 계좌 스냅샷이 없어 실제 데이터로 재구성하지 않았다 — 합성 테스트로만 확인한다. 합성 테스트(`test_p7_ops_parity.py` 합성 2개 seed: 거래량 0·NaN·행 없음·OHLC 불일치 포함)와 실제 데이터 검증은 따로 실행한다 (실제 데이터 테스트는 로컬 캐시가 있을 때만 돈다).

## 독립 리뷰 반영 (PR #14, 2026-11-25)

| 등급 | 지적 | 반영 | 회귀 테스트 (`tests/test_auto_buy_shadow_review2.py`) |
|---|---|---|---|
| P1 | 주기 60분이면 APScheduler 가 `*/60` 거부 → 공용 스케줄러(S01 포함) 시작 실패 | 60분은 매시 정각(`minute="0"`). SHADOW 등록 실패는 기록만 하고 스케줄러는 시작 | 60분 설정에서 S01·SHADOW job 함께 등록·시작, 등록 실패 시 S01 유지 |
| P1 | 단일 종목 yfinance 응답이 `(Ticker, Price)` 다중 열이면 일봉 None | 단일·복수, `(Ticker, Price)`·`(Price, Ticker)`·평평한 열 모두 처리 (`bars_from_download`) | 응답 형태별·단일 종목 복구 |
| P1 | 종가가 고가보다 1.42e-14 큰 봉 거부 → B01 포지션 미확정·매수 차단 | OHLC 일관성 검사에 상대 허용 오차 1e-9. 그보다 큰 불일치는 계속 INVALID | 정밀도 오차 허용·0.1% 불일치 거부, B01 체결 |
| P1 | 복구 봉 저장 후 replay 실패 → 다음 job 이 재계산하지 않음 | 복구 봉과 같은 트랜잭션에 `replay_needed` 저장, run 교체 완료 때만 해제 | 실패 후 CURRENT 유지·다음 job 재시도·교체 후 해제 |
| P1 | job 의 적격 기록 직후 hook 이 그날 확정 → 후보 0건으로 확정 | 일 처리 직전에 적격 기록을 다시 하고, 결정 스캔 적격 기록이 끝나지 않았으면 처리하지 않음 (`build_inputs` 도 거부) | 해당 실행 순서 재현 |
| P1 | 22:00 ET 이후 새로 시작한 정규 스캔도 FROZEN | 신호 마감 이후 시작한 스캔은 결정 스캔이 될 수 없음 (`SCAN_AFTER_SIGNAL_CUTOFF`). 마감 후에는 마감 전에 시작한 그날 정규 스캔만 기다림 | 마감 후 시작·마감 전 시작 후 늦게 종료·실행 중 대기 |
| P2 | DONE 커밋 뒤 만료된 `log.id` 평가로 OFF·KR 에서도 추가 SELECT | 시작 시 받은 ID 를 보관해 hook 에 전달 | 커밋 뒤 ID 재조회 없음 (KR) |
| P2 | 중단 후보 선정 후 기록 직전 원본이 DONE 이어도 ABANDONED 저장 | 쓰기 트랜잭션 안에서 원본 상태를 다시 읽고 끝났으면 저장하지 않음. 단일 Scanner 프로세스 전제를 문서화 | 기존 ABANDONED 테스트 |
| P2 | 복구가 가장 오래된 10일만 반복, 조회 범위 21세션 | 커서로 순환, 창은 마지막 마감 세션 포함 정확히 20세션 | 마지막 자동 조회일·첫 수동 검토일, 순환 |
| P2 | 추가 휴장일 변경·`runner.py` 가 해시 밖, 버전 상향 시 이전 run 재사용 | 추가 휴장·조기폐장일을 설정 해시에, `runner.py` 를 로직 해시에 포함. 버전이 바뀌면 이전 run 을 SUPERSEDED 로 보존하고 새 버전 run 을 이전 버전 마지막 처리일 다음 거래일부터 시작 | 해시 변화, 버전 상향 후 이틀 처리 |
| P2 | S5: 미청산 손익 상계 PF, 미청산 매도세 누락, 안전 위반이 비가상 행만 | P7 과 같은 거래별 PF, 미청산은 매도세 포함 매도 비용으로 평가, 안전 위반 = 비가상 행 + ERROR 이벤트 + 미수집 스캔 + heartbeat 지연 + 버전 차단 | P7 `portfolio_metrics` 와 PF 일치, 매도세, 오류 집계 |
| P2 | Migration `--check` 가 불완전 `ab_*` 스키마·하드링크를 통과 | 최신이 아니면 임시 사본에서 upgrade 를 시험. 다른 저널과 inode 비교 | 불완전 스키마·하드링크 거부, 원본 불변 |
| P2 | 공개 이력에 실계좌 조회 파생 구성 정보 | **보류 (사용자 결정 필요)** — 이력 재작성(force-push)은 승인 범위 밖 | — |

- 리뷰 검증의 **실제 재구성 데이터 대조 차이**(B00·일부 B02 잔여 차이)와 지정가 반올림 차이(P7 미반올림, SHADOW 센트 반올림)는 아래 '한계'에 남긴다. B01·B00-N 은 리뷰의 원인 분리 실험에서 반올림·OHLC 경계를 맞추면 일치했다.

## 운영 정책 (2026-11-25 사용자 확정)

`settings.POLICY`·`COST_BASIS`·`OPS_COST_ESTIMATE` 에 고정하고 설정 해시에 포함한다. 바꾸려면 버전을 올린다.

| 항목 | 확정 내용 |
|---|---|
| 장기 미확정 포지션 | 신규 매수 차단을 유지한다. 가격·위험 확인 없이 자동 청산하거나 차단을 해제하지 않는다 |
| 정규 스캔 실패·불완전일 | 대체 결정 스캔을 허용하지 않는다 (`NO_DECISION_SCAN`, 그날 신규 매수 없음) |
| 신호 마감 | 22:00 ET (마감 + 6시간) |
| 데이터 마감 | 23:20 ET (마감 + 7시간 20분). 조기폐장일은 두 마감 모두 실제 마감 시각에 맞춰 당긴다 |
| 자동 결측 복구 시도 | 최근 20 거래일 |
| 20 거래일 이후 | 결측 위험·불완전 상태·차단을 유지하고 수동 검토 대상(`MANUAL_REVIEW`)으로 표시한다. 자동 재조회·재계산하지 않는다 |
| 계좌 대조 허용 오차 | max($5, 0.5%) — 초기 검증 기준 |
| 거래비용 | P7 과 SHADOW 비교에는 같은 비용(`P7_COMPARISON_v1`)을 쓴다. 실제 운영 추정 비용은 따로 기록한다 (아래) |
| S5 채택 기준 | 아래 확정 기준 유지 |
| 판정 | NOT_RECOMMENDED, B01 = NEED_MORE_DATA 유지 |

### 거래비용 두 가지

| 구분 | 값 | 용도 |
|---|---|---|
| 비교용 비용 `COSTS` (`P7_COMPARISON_v1`) | 매수·매도 수수료 0.25%, 슬리피지 10bp, 매도세 0.0000278 — `backtest/entry_strategy/configs/entry_backtest.yaml` 과 동일 | 가상 체결·손익·R·S5 판단. 대조 테스트가 P7 설정과 일치를 확인 |
| 운영 추정 비용 `OPS_COST_ESTIMATE` | 수수료 0.25%/0.25%, 슬리피지 10bp, 매도세 0, 환전 비용 미기록, 상태 `UNVERIFIED` | 기록·표시만. 증권사 실제 수수료·환전·규제 수수료를 확인하지 않은 추정치이며 손익에 쓰지 않는다 |

## 무엇을 하나 / 하지 않나

- **한다:**
  - 운영 Scanner 의 US 신호를 스캔 직후 **불변 스냅샷**으로 별도 SQLite 에 남긴다.
  - 거래일마다 정규 Scanner 결과 1회로 후보를 **확정**하고, 46 개 가상 계좌가 같은 입력을 쓴다.
  - B00 / B00-N / B01 / B02 를 **독립 가상 계좌**에서 같은 조건으로 비교한다.
  - 가상 체결과 가상 S01 손절을 일봉으로 처리한다. 일봉 결측은 정상으로 보지 않는다.
  - 결과는 AUTO BUY MONITOR 탭과 조회 API 로 본다.
- **하지 않는다:**
  - 실제 매수·매도·취소
  - LIVE
  - 운영 DB 스키마 변경, 운영 DB outbox
  - 운영 S01 코드·설정·저널·스케줄러 변경
  - 기존 Migration 변경
  - Sector Stage 를 매수 필터로 쓰는 것
  - 새 점수제, 사후 파라미터 탐색

## 운영 모드 `AUTO_BUY_US_MODE`

| 값 | 동작 |
|---|---|
| `OFF` (기본) | 아무것도 하지 않는다. Scanner hook·health API 도 파일·테이블을 만들지 않는다. SHADOW job 을 등록하지 않는다 |
| `SHADOW` | 스냅샷 → 후보 확정 → 적격 판정 → 가상 계좌 (결정·예약·가상 체결·가상 S01·일일 기록). 실주문 모듈을 import 하지 않는다 |
| `LIVE` | **거부.** 설정 오류로 표시하고 OFF 로 동작한다. LIVE 주문 경로가 코드에 없다 |

- 변경은 재시작해야 반영된다. 스케줄러 job 이 시작할 때만 등록되기 때문이다.
- 다음 경우도 SHADOW 가 동작하지 않는다 (health 에 `config_error` 로 표시).
  - `AUTO_BUY_SHADOW_JOURNAL_PATH` 가 비어 있다.
  - 상대경로다.
  - 운영 주문 저널(`SCANNER_ORDER_JOURNAL_PATH`)이나 ETF 저널(`ALLOCATION_JOURNAL_PATH`)과 같은 파일이다.

## 구성

| 파일 | 역할 |
|---|---|
| `trading/auto_buy/settings.py` | 모드, 저널 경로 검증, **동결된** 전략·위험·입력 확정·S5 기준 (`STRATEGY_VERSION=SV1`, 설정 해시), 가상 계좌 목록 |
| `trading/auto_buy/version.py` | 판정 로직 해시 (AST 정규화, 주석·docstring 무시) |
| `trading/auto_buy/shadow/journal_migrations/` | SHADOW 저널 전용 Alembic. 리비전 `ab0001`, 버전 표 `ab_alembic_version`, DDL `ddl_v1.py` |
| `trading/auto_buy/shadow/schema.py` | 런타임 스키마 확인. `mode=ro` 로만 연다 (파일 생성 없음). `upgrade_journal` 은 배포 스크립트 전용 |
| `scripts/auto_buy_shadow_journal_migrate.py` | 배포 작업: 대상 검증 → (기존 파일이면 백업) → upgrade → 사후 검증 → 권한 600 |
| `trading/auto_buy/shadow/snapshot.py` | S1: Scanner hook, 보충 수집, 중단(ABANDONED) 스캔 기록, 당일 후보 확정, 최초 Strict 통과 판정, 스캔 건수 대조 |
| `trading/auto_buy/shadow/inputs.py` | 거래일 입력(결정 스캔·계좌 스냅샷·일봉) 확정, 일봉 대기·마감, 결측 복구와 run 재계산 |
| `trading/auto_buy/shadow/eligibility.py` | 적격 판정 (P7 정의 그대로, 순수 함수), Grade 순위, 고정 seed 무작위 순위 |
| `trading/auto_buy/shadow/risk.py` | Entry/Current Heat, 예약, Sleeve·섹터·현금 하한, 미확정 포지션 스트레스, 안전 차단 |
| `trading/auto_buy/shadow/vfills.py` | 가상 체결 `DAILY_T1_100_v1`, 가상 S01 |
| `trading/auto_buy/shadow/engine.py` | 가상 계좌 일 단위 처리 (run 단위·멱등), 체결 ledger(일봉 근거 포함), 손절·매수잔량 충돌 |
| `trading/auto_buy/shadow/market_data.py` | 일봉(yfinance, Scanner 와 같은 공급원), OPS 계좌 스냅샷 (읽기 전용, 잔고 대조) |
| `trading/auto_buy/shadow/runner.py` | 스케줄러 진입점 |
| `trading/auto_buy/shadow/report.py`, `web/auto_buy_api.py` | 조회 전용 GET API `/api/auto-buy/{health,summary,accounts/{id},signals}` |
| `scanner/scan_engine.py` | `_auto_buy_snapshot_hook` (스캔 DONE 커밋 직후, OFF 면 즉시 반환, 예외 미전파) |
| `scheduler.py` | `auto_buy_us_shadow` job. SHADOW 일 때만 등록. 월~토 17:00~23:59 ET, N분 간격, `max_instances=1` |
| `web/templates/index.html` | `🤖 AUTO BUY MONITOR` 탭 |

## S1 — 불변 Scanner 스냅샷 (운영 DB 변경 없음)

> **S1 한계:** 운영 DB outbox 없이 별도 SHADOW 저널만으로는 신호 수집의 **완전성을 보장하지 못한다.** 아래 장치는 누락을 감지·표시하고 확정할 수 없는 신호를 매수 판단에서 빼는 것이다. (화면 수집 이력 상단에도 같은 안내를 표시한다.)

운영 `scan_engine._save` 는 같은 신호 행(ticker·signal_date·signal_type)을 재스캔 때마다 **덮어쓴다**. 그래서 스캔 직후의 값을 별도 SHADOW SQLite 의 `ab_signal_snapshots` 에 복사한다.

- **HOOK 수집:** 스캔이 DONE 으로 커밋된 직후, 같은 스캔 실행권 안에서 돈다. `scan_time ∈ [started_at, finished_at]` 인 US 행 전체(Strict 거부·미평가 포함)를 복사한다. 운영 DB 는 읽기만 한다.
- **불변:** UPDATE·DELETE 는 SQLite 트리거로 거부한다. 재스캔하면 새 행이 생긴다. 각 행에 운영 행 전체 JSON·해시·scanner 커밋·임계값 해시·판정 로직 해시를 남긴다.
- **중복 방지:** `ab_scan_captures.scan_log_id` PK, `UNIQUE(scan_log_id, signal_key)`. 스캔 1회분은 한 트랜잭션이다.
- **보충 수집 (CATCHUP):** hook 이 실패·누락한 DONE·ERROR 스캔을 job 이 보충한다. 스캔 진행 중이면 보충하지 않는다. 그 사이 다음 스캔이 덮어쓴 행은 복구할 수 없어 `complete=0` · `recoverability=UNRECOVERABLE` 로 남긴다.

### 당일 후보 확정 (P1-1)

- 거래일 D 의 후보는 **정규 Scanner 결과 1회**로 확정한다 (`ab_day_inputs`, `FROZEN`).
  - 조건: `triggered_by='scheduler'`, DONE, 완전 수집(`complete=1`), 장 마감 후 시작. 처음 그 조건을 만족해 수집된 스캔이 결정 스캔이다.
  - 신호 마감(정상일 22:00 ET = 마감 + 6시간, 조기폐장일은 마감에 맞춰 당겨짐)까지 결정 스캔이 없으면 `NO_DECISION_SCAN` 으로 확정하고 그날 신규 매수 판단을 하지 않는다.
  - 마감 **전에 시작한** 그날 정규 스캔이 아직 실행 중이거나, 끝났지만 아직 수집되지 않았으면 마감을 미룬다. 그 스캔은 마감 뒤에 끝나도 결정 스캔이 될 수 있다.
  - 마감 **이후 새로 시작한** 스캔은 기다리지 않고 결정 스캔이 될 수 없다. 스냅샷은 저장하고 `SCAN_AFTER_SIGNAL_CUTOFF` 로 기록한다.
  - job 은 그날을 처리하기 직전에 적격 판정을 다시 기록하고, 결정 스캔 스냅샷의 적격 기록이 모두 끝났을 때만 그날을 확정한다 (hook 과 job 의 실행 순서와 무관).
- **확정 이후 스캔** (수동 재스캔, 신호 마감 뒤 도착한 정규 스캔 등):
  - 다음 정규장 전이라도 당일 후보에 추가하지 않고, 확정된 후보를 소급 변경하지 않는다.
  - 스냅샷은 그대로 저장하고 `late_after_decision=1` 로 표시한다.
  - `LATE_SCAN_AFTER_DECISION` 이벤트에 스캔 시각·실행 주체·확정 후보에 없던 Strict 종목을 남기고, 적격 판정에 같은 사유를 기록한다. 화면에 건수·종목·스캔 시각·사유를 표시한다.
- **다음 날 중복 방지:** 늦은 스캔에서 처음 Strict 통과한 같은 신호(signal_key)가 다음 날 정규 스캔에 다시 나와도 신규 신호가 아니다 (`first_strict_pass=0`). 기록은 `PRIOR_STRICT_NOT_DECIDED` → 사유 `ALREADY_PASSED_NOT_DECIDED` 로 남겨 조용히 사라지지 않게 한다. 다른 signal_date 의 독립 신규 신호는 정상 평가한다.
- 결정 스캔이 아닌 스냅샷의 역할: `DECISION` / `LATE_AFTER_DECISION` / `NON_DECISION` (확정 전 비정규 스캔, ERROR, ABANDONED, 장중). `NON_DECISION` 사유는 `NOT_DECISION_SCAN`.

### 최초 Strict 통과 판정 (거래일 구간 기준)

- 거래일 구간 = 개장 시각이 그 시각 이하인 가장 최근 정규장. 장 마감 후 저녁 스캔은 그날, 다음 날 개장 전 스캔도 그날이다. P7 의 일 단위 '최초 등장'과 같다.
- 이전 거래일 구간의 장 마감 후 스냅샷에 같은 signal_key 의 Strict 통과가 있으면 이미 통과(0). 그 통과가 그날 결정 스캔이 아니었으면 `PRIOR_STRICT_NOT_DECIDED`.
- 최초 탐지(`first_detected_at`)가 이 스캔과 같은 거래일 구간이면 확정(1).
- 그 밖에는 최초 탐지 이후 이 스캔 전까지 행을 썼을 수 있는 모든 US 스캔(종료 기록 없는 RUNNING·중단 스캔 포함)이 완전 수집됐을 때만 확정. 아니면 `NULL` → `FIRST_PASS_UNKNOWN` (모든 전략 제외). 수집 시작 이전 신호는 `BEFORE_COLLECTION`, 수집 누락은 `CAPTURE_GAP`.
- 장중 시작 스캔은 미완성 봉을 볼 수 있어 `intraday=1`, `INTRADAY_SCAN` 으로 제외한다.

### 중단 스캔과 스캔 건수 대조 (P1-2)

- **ABANDONED 판정:** 종료 기록 없는 RUNNING 스캔 중, 스캔 실행권이 비어 있고 다음 중 하나면 중단으로 본다.
  - 그 뒤 다른 ScanLog 가 있다 (스캔은 프로세스 안에서 하나씩만 돈다)
  - 현재 프로세스 시작 이전에 시작했다 (재시작으로 끊긴 스캔)
  - 시작 후 `MAX_SCAN_HOURS`(6시간)이 지났다
  - 실행권이 잡혀 있거나 같은 프로세스에서 6시간 안이면 **정상 장기 실행**으로 보고 건드리지 않는다.
- 기록 직전에 쓰기 트랜잭션 안에서 원본 상태를 다시 읽는다. 그 사이 끝난 스캔은 ABANDONED 로 저장하지 않고 다음 보충 수집이 정상 처리한다.
- **전제:** Scanner 는 이 앱 프로세스 하나에서만 돈다. 실행권·`scan_status` 는 프로세스 안에서만 유효하고, ScanLog ID 순서를 실행 순서로 해석한다. 다른 프로세스·서버가 같은 운영 DB 로 스캔하면 이 판정은 성립하지 않는다.
- ABANDONED 스캔은 `[시작, 다음 스캔 시작)` 구간 행을 `complete=0` 으로 기록한다. 다음 스캔이 이미 있으면 `UNRECOVERABLE`, 없으면 `PARTIAL`. 매수 판단에는 쓰지 않는다.
- 완전 수집 여부 판정에 종료 기록 없는 스캔도 포함한다. 그래서 중단 스캔 이후 처음 보이는 신호는 `FIRST_PASS_UNKNOWN` 이 된다.
- 매 job 이 수집 시작 이후 운영 US ScanLog 건수(완료·실패·진행)와 수집 이력(완전·불완전·중단·복구 불가·확정 후)을 대조해 `scan_reconciliation` 에 저장한다. 화면에 전체·수집·진행 중·실패·중단·복구 불가·미수집 건수를 표시한다.

### 보장 범위와 한계

| 항목 | 보장 | 한계 |
|---|---|---|
| hook 정상 동작 | 그 스캔이 저장한 US 행 전체를 정확한 값으로 수집 | — |
| hook 실패 후 다음 스캔 전 보충 | 정확 수집 (`complete=1`) | — |
| hook 실패 후 다음 스캔까지 지남 | 덮어쓰지 않은 행만 수집 (`UNRECOVERABLE`) | 덮어쓴 행의 옛 값·건수는 알 수 없다. 영향 신호는 `FIRST_PASS_UNKNOWN` |
| 프로세스가 스캔 중 종료 | ABANDONED 로 기록, 영향 범위 표시 | 끊기기 전에 쓴 행만 남는다 |
| 정규 스캔이 실패·불완전 | 그날 `NO_DECISION_SCAN` (신규 매수 없음) | 대체 결정 스캔을 허용하지 않는다 (확정 정책) |
| `STRICT_PERSIST_REJECTED=false` 로 바꾼 경우 | — | 거부 신호가 운영 DB 에 남지 않아 "늦은 통과"를 최초 탐지로 보게 된다 (기본 true 유지 필요) |
| 다중 프로세스 | 스캔 실행권은 프로세스 내부 락 | 단일 uvicorn 가정 (기존과 같음) |

- 완전 보장이 꼭 필요할 때만 운영 DB outbox 를 쓴다 (`_save` 와 같은 트랜잭션의 INSERT-only 표). 운영 DB 변경이라 이번에 구현하지 않았다. 필요하면 별도 Migration Plan 으로 승인을 요청한다.

## 거래일 입력 확정과 일봉 (P1-3, P2-4.3)

거래일 D 의 입력은 46 계좌가 함께 쓰고, 한 번 정하면 바꾸지 않는다 (트리거로 보호).

| 입력 | 저장 | 비고 |
|---|---|---|
| 결정 스캔·신호 집합 | `ab_day_inputs` (결정 스캔 ID, 신호 집합 해시, 전략 버전·설정 해시·로직 해시) | 위 '당일 후보 확정' |
| 계좌 스냅샷 (OPS) | 같은 행 `account_status` OK / UNAVAILABLE / LATE, 사유, 평가·현금·보유별 평가액 | 그날 처리 때 1회만 조회. 계좌번호·원 응답 미저장 |
| 일봉 | `ab_bars(day, ticker, input_rev)` OK / MISSING / INVALID, OHLC, 공급자, 조회 시각, 용도 | INSERT 만 허용. 복구는 새 리비전 |

- **필요 종목:** 그날이 목표일인 미체결 주문 + 가격 확인 중인 보유 포지션. 이 집합을 먼저 정하고 공통 입력으로 받는다.
- **일봉 시각 (거래소 캘린더 기준, 조기폐장은 마감에 맞춰 당김, 휴장일은 처리 대상 아님):**
  - 마감 + 60분 전: 잠정치로 보고 받지 않는다.
  - 데이터 마감(정상일 23:20 ET = 마감 + 7시간 20분) 전: 하나라도 없거나 무효면 기다리며 다음 job 에서 다시 받는다. 받은 OK 봉만 저장한다.
  - 데이터 마감 후에도 없으면 `MISSING`, 날짜·OHLC 불일치(고가 < 저가 등)면 `INVALID` 로 저장하고 그날을 `DATA_INCOMPLETE` 로 처리한다.
  - 일봉 날짜는 요청 거래일과 정확히 같아야 한다 (공급자 함수에서 확인). yfinance 응답은 단일·복수 종목, 다중 열·평평한 열 형태를 모두 처리한다.
  - OHLC 일관성 검사는 상대 허용 오차 1e-9 를 둔다 (수정주가 계산의 부동소수점 오차는 정상 봉, 그보다 큰 불일치는 `INVALID`).
- **결측을 정상으로 보지 않는다:**
  - 목표일 일봉이 없는 주문 → `FILL_UNKNOWN`. 예약 현금·예정 위험을 유지하고 `PENDING_UNRESOLVED` 로 신규 매수를 막는다.
  - 보유 포지션 일봉이 없으면 → `price_status=UNRESOLVED` (그날부터). 손절 판정을 보류하고 '손절 없음'으로 보지 않는다. `PRICE_DATA_MISSING` 으로 신규 매수를 막는다.
  - 일일 기록에 `data_status=INCOMPLETE`, 미확정 포지션·주문 수, 마지막 확인 종가 기준 평가, **전액 손실 가정 스트레스**를 남긴다. 위험은 체결가 기준을 유지한다 (0 처리·삭제 없음).
- **재실행:** job 을 다시 돌려도 저장된 결정 스캔·계좌 스냅샷·일봉을 다시 읽는다. 공급자를 다시 부르지 않는다.
- **체결 근거:** 체결마다 `ab_fill_ledger` 에 체결가·근거(`DAILY_T1_100_v1:open|limit`)·사용한 OHLC·공급자·조회 시각·입력 리비전을 남긴다.
- **체결 당일(Day-0) 모호성:** 일봉으로는 매수와 손절의 순서를 알 수 없다. 체결 당일 저가 ≤ 손절가면 매수 후 손절가 청산으로 **보수 가정**한다 (`STOP_DAY0`, 손절 이벤트 details 에 가정 문구 기록).

### 결측 복구와 재계산

- 최근 20 거래일(마지막 마감 세션 포함 정확히 20세션)의 MISSING·INVALID 일봉을 job 마다 다시 받는다. 1회 최대 10 거래일이며, 지난 job 이 본 날 다음부터 순환해 앞쪽 결측이 계속 남아도 뒤쪽 날짜가 밀리지 않는다.
  - 예: D 의 결측은 D 가 창의 가장 오래된 세션(20번째)인 날까지 자동 재조회하고, 다음 세션부터 `MANUAL_REVIEW` 다.
- 하나라도 확인되면 새 리비전(`RECOVERY`)으로 저장하고 **새 run** 을 만들어 시작일부터 46 계좌 전체를 시간순으로 다시 계산한다.
  - 현금·Heat·포지션·수량·신규 판단까지 모두 다시 나온다. 단일 손절 이벤트만 끼워 넣지 않는다.
  - 원래 run 에 없던 종목·날짜 일봉은 이때 받는다 (`REPLAY`).
  - 끝나면 한 트랜잭션에서 이전 run 을 `SUPERSEDED`, 새 run 을 `CURRENT` 로 바꾼다. 결과가 달라진 계좌 수·최초 차이일·손절 건수 전후를 이벤트와 run 요약에 남긴다.
  - 원래 스냅샷·결정 입력·이전 run 행(주문·체결·포지션·일일 기록)은 지우거나 덮어쓰지 않는다. 화면은 현재 run 을 보이고 이전 run 은 재계산 이력으로 보인다.
  - 재계산 중(`BUILDING`)에는 health 에 진행 중으로 표시하고 기간을 미완료로 본다. 실패하면 `FAILED` 로 남기고 현재 run 을 유지한다.
  - 복구 봉 저장과 같은 트랜잭션에 `replay_needed` 를 남기고 run 교체가 끝날 때만 지운다. replay 가 실패해도 다음 job 이 다시 시도한다 (최신 봉이 이미 OK 여도).
- **복구 기간(20 거래일)이 지난 결측:** 더 이상 재조회하지 않는다. 일봉 행(`MISSING`/`INVALID`)·불완전 상태·미확정 포지션·체결 불명 주문·위험·신규 매수 차단을 그대로 둔다. job 이 `MANUAL_REVIEW_REQUIRED` 이벤트를 결측 1건당 한 번 남기고, 화면 미확정 표에 `MANUAL_REVIEW` 와 대상 일봉 목록을 표시한다. 해소는 수동 검토 후 별도 승인 작업으로만 한다.
- 모든 전략이 같은 거래일 입력을 공유하므로 완전성 기준이 같다. 기간 완전성 = 일봉 미완료일 0, 재계산 진행 없음, 미확정 포지션·주문 0.

### 장기 결측 (거래정지·상장폐지·공급자 누락)

- 이번 구현은 셋을 **판정하지 않는다.** 증거가 없기 때문이다. 연속 결측 거래일로 표시만 한다.
  - 5 거래일 미만: `GAP_PROVIDER_OR_HALT`
  - 5 거래일 이상: `NO_DATA_SUSPECTED_HALT_OR_DELISTING`
- 실제 가격이 다시 확인되면 위 재계산으로 반영한다. 청산·상장폐지 근거가 없으면 체결가를 만들지 않는다.
- 미확정 포지션은 화면에 따로 표시한다: 수량, 마지막 확인 종가·일자, 미확정 시작일, 결측 거래일 수, 진입 위험, 전액 손실 가정 금액. 삭제하거나 위험 0 으로 보지 않는다.
- 신규 매수 차단은 유지한다. 가격·위험 확인 없이 자동 청산하거나 차단을 해제하지 않는다 (확정 정책). 20 거래일이 지나면 수동 검토 대상으로 표시한다.

## 판정 로직 버전 (P2-4.2)

- 설정 해시(`config_hash`)와 별도로 **판정 로직 해시**(`logic_hash`)를 계좌·거래일 입력·run·적격 판정에 기록한다.
- 대상: 적격 판정, 위험·사이징, 가상 체결, 엔진, 거래일 입력·일봉 처리, 신호 정규화·최초 통과 판정, 거래일 캘린더(SHADOW·S01 규칙 캘린더), 시장 데이터 처리, job 실행 순서(`runner`), 설정, 그리고 운영 `scanner.sizing.calculate_position`(보조 함수·모듈 상수 포함). Python major.minor 도 포함한다.
- 설정 해시에는 추가 휴장일·조기폐장일(`AUTO_BUY_US_EXTRA_CLOSED_DATES`, `AUTO_BUY_US_EXTRA_EARLY_CLOSE_DATES`)도 들어간다. 주문 목표일·마감 시각을 바꾸므로, 바꾸면 버전을 올린다.
- AST 기준이라 주석·docstring·공백 변경은 영향이 없다. 화면·조회(report, API, 템플릿)·저널 접근은 대상이 아니다.
- 기록과 다르면 job 이 `version_mismatch` 이벤트를 남기고 **처리를 멈춘다** (스냅샷 수집은 계속). 기존 결과는 지우지 않는다. 변경을 반영하려면 `STRATEGY_VERSION` 을 올려 새 계좌로 시작한다.
- **버전을 올리면:** 이전 버전 CURRENT run 을 `SUPERSEDED` 로 보존하고(`version_switch` 이벤트) 새 버전 run 을 만든다. 새 버전 계좌는 이전 버전 run 이 마지막으로 처리한 거래일의 다음 거래일부터 시작한다 (`version_start:<버전>`). 과거 구간을 새 버전으로 다시 채우지 않는다.

## 전략 (가상 계좌, 버전 SV1 동결)

| 계좌 | 후보군 | 순위 | 하루 |
|---|---|---|---|
| `SV1:{P}:B00` | 공통 적격 (결정 스캔, Strict, 최초 Strict 통과, 유효 가격·손절) | Grade → Signal Quality → 티커 | 제한 없음 |
| `SV1:{P}:B00N` | 같음 | 같음 | 최대 2 |
| `SV1:{P}:B01` | 공통 + P7 B01 조건 | 같음 | 최대 2 |
| `SV1:{P}:B02:s01..s20` | B01 과 같은 후보군 | 고정 seed 해시 무작위 | 최대 2 |

- **B01 은 P7 정의로 고정한다.** 조건: 최초 Strict 통과 = 최초 탐지, 시장 BULL, BREAKOUT/RE_BREAKOUT 피벗 이상, 추격 경고 없음, 손절폭 ≤ 15%.
- **제외하지 않는 것:** STRONG 아님, Grade A 아님, 손절폭 > 7%, 주봉 거래량 경고, 판정 보류. REBOUND 에는 피벗 조건을 적용하지 않는다.
- **골든 테스트:** 백테스트 후보 2,805건 전체에서 SHADOW 판정과 P7 B01 마스크가 100% 일치한다 (`backtest/entry_strategy/tests/test_shadow_golden.py`).
- **P7 엔진 대조:** 같은 신호·일봉·위험·보유·수수료·체결 규칙이면 SHADOW 엔진과 P7 시뮬레이터 결과가 같다 (`backtest/entry_strategy/tests/test_shadow_p7_parity.py`, 합성 데이터 3 seed). 비교 항목: 주문 수, 체결·미체결·시가 손절 회피 건수, 모든 차단 사유·건수(Top-N·중복·Heat 한도 등), 체결 수량·체결가·비용, S01 손절일·사유·청산가, 손익·R, 일별 순자산·보유 수·판단 전 Entry Heat, 최종 현금·누적수익률.
  - 비용은 같다: P7 쪽은 P7 설정 파일 비용을 그대로, SHADOW 쪽은 비교용 비용 `COSTS` 를 쓴다 (`test_comparison_costs_identical_to_p7`).
  - 다음 표현 차이는 대조에서 맞췄고 숨기지 않는다.
    - 지정가: P7 은 종가×1.01 미반올림, SHADOW 는 센트 반올림 (대조는 정수 종가로 같은 값).
    - Entry Heat 기록 시점: P7 은 판단 전, SHADOW `ab_daily` 는 판단(신규 예약) 후.
    - 체결 당일 손절 사유 이름: P7 `STOP`(day0) ↔ SHADOW `STOP_DAY0`.
    - P7 은 마지막 날 판단을 하지 않는다.
    - 체결 시 현금 부족이면 P7 은 수량을 줄이고, SHADOW 는 예약 현금으로 보장돼 줄일 일이 없다 (대조 데이터에서 발생하지 않음).
- **설정 동결:** 설정 해시·로직 해시가 다른 채 같은 버전으로 실행하면 job 이 처리를 멈춘다. 기존 계좌는 덮어쓰지 않는다.

## 위험 예산 (A6·A9·A10·Sleeve)

### 프로필 (결과는 분리 집계)

| | P7 비교형 `P7C` | 운영 검증형 `OPS` |
|---|---|---|
| 자금 | 독립 가상 $50,000 | 실제 account1 순자산 × 30% (자동매수 전용 예산) |
| 거래당 위험 / Entry Heat / 종목 | 0.75% / 4% / 15% (가상 순자산 기준) | 0.75% / 4% / 15% (**전용 예산 기준**) |
| 섹터 상한 | 없음 (P7 기준 유지) | 35% (전용 예산 기준) |
| 현금 하한 | 가상 순자산 20% | **계좌 전체** 20% |
| 수익률 기준 | $50,000 | 시작일 전용 예산 |

### Heat 정의

- **Entry Heat (A9):** 신규 매수 예산 판단에 쓴다.
  - Σ (확정 체결가 − 고정 손절가) × 남은 수량 + 미체결 매수의 최대 예정 위험 (지정가 − 손절가) × 남은 주문 수량
  - 손절선 이탈 미청산, 가격 미확정 포지션, 체결 불명 주문도 위험이 남는다.
- **Current Heat (A10):** 표시 전용이다. Σ max(종가 − 손절가, 0) × 수량 + 미체결 예정 위험. 이탈·미확정 포지션은 체결가 기준 위험을 유지한다. 10% 경고, 15% 위험 경고. **Current Heat 비율만으로 매수를 막지 않는다.**
- **수동 보유 (A6):** 자동매수 Heat 에는 자동매수(가상) 포지션·예약만 넣는다. 수동 보유는 계좌 전체 평가·투자 비중·집중도·현금 계산에 넣고, 손절가 없는 수동 보유는 **미확정 위험**(평가금액 × 15% 보수 추정)으로 따로 표시한다. 계좌 전체 위험 = 자동 Entry Heat + 수동 미확정 위험 (10% 관찰 기준, 표시만).
- **OPS 계좌 계산:** "가상 매매가 실제 계좌에서 일어났다면"으로 계산한다. 주가 상승으로 상한을 넘으면 신규 매수만 막고(`SLEEVE_FULL`·`SECTOR_CAP`) 강제 매도하지 않는다. 섹터 불명 후보는 `SECTOR_UNKNOWN` 으로 차단한다.

### OPS 계좌 스냅샷 대조

다음이면 `UNAVAILABLE` 로 저장하고 OPS 신규 가상 매수를 막는다 (`ACCOUNT_SNAPSHOT_UNAVAILABLE`).
- 조회 실패, 원장 평가 합계 미확인, 순자산·현금 값 비정상(현금 > 순자산 등)
- 보유 종목 통화가 USD 가 아님
- 보유 종목 평가액 합계와 같은 원장(ust21070) 평가금액 차이가 max($5, 0.5%) 초과 (잔고 불일치)
- 보유 행과 계좌 요약의 조회 시각이 다름
- 그날 장 마감 후 조회 시점을 놓쳐 다음 정규장 이후에 처리하는 날은 `LATE` (조회하지 않음)

### 매수 차단 (안전 조건)

Current Heat 는 여기에 들어가지 않는다.

| 코드 | 의미 |
|---|---|
| `STOP_BREACHED_OPEN` | 종가 < 손절가인 미청산 포지션 (손절 지연) |
| `PRICE_DATA_MISSING` | 보유 종목 일봉 결측 → 가격 미확정 포지션 |
| `PENDING_UNRESOLVED` | 목표일이 지난 미체결 주문 또는 체결 불명(`FILL_UNKNOWN`) 주문 |
| `ACCOUNT_SNAPSHOT_UNAVAILABLE` | OPS: 계좌 조회 실패·불완전(잔고 불일치 포함) 또는 늦은 처리 |
| `RISK_UNCOMPUTABLE` | 위험 기준 금액 계산 불가 |

- 적격 단계 사유: `NOT_DECISION_SCAN`, `LATE_SCAN_AFTER_DECISION`, `INTRADAY_SCAN`, `NOT_STRICT`, `STRICT_UNASSESSED`, `FIRST_PASS_UNKNOWN`, `ALREADY_PASSED`, `ALREADY_PASSED_NOT_DECIDED`, `INVALID_PRICE`, `INVALID_STOP`, (B01) `LATE_STRICT_PASS`, `MARKET_NOT_BULL`, `BELOW_PIVOT`, `CHASE_WARNING`, `STOP_GT_15`
- 수량·예산 사유: `MIN_CASH_RESERVE`, `INSUFFICIENT_CASH`, `R_BAND`, `HEAT_LIMIT_EXCEEDED`, `RISK_BUDGET_TOO_SMALL`, `POSITION_CAP_TOO_SMALL`, (OPS) `SLEEVE_FULL`, `SECTOR_CAP`, `SECTOR_UNKNOWN`, `ACCOUNT_MIN_CASH`
- 판단 순서 사유: `DUPLICATE_HOLDING_OR_ORDER`, `DAILY_TOP_N`, `NO_NEXT_SESSION`

## 가상 체결 / 가상 S01 (`DAILY_T1_100_v1`)

- **매수:** D 결정 스캔 → 판단가 × 1.01 지정가 (센트 내림, +1% 초과 없음), 다음 정규장 1일.
  - 시가 ≤ 손절가이거나 R < 3% → 전송하지 않음 (`SKIP_OPEN_STOP`)
  - 시가 ≤ 지정가 → 시가 체결, 저가 ≤ 지정가 → 지정가 체결, 그 밖 → 미체결 (추격 없음)
  - 목표일 일봉 결측·거래량 미확인 → `FILL_UNKNOWN` (위), 거래량 0 확정 → 미체결 만료 (`ZERO_VOLUME_EXPIRED`)
- **비용:** 비교용 비용 `P7_COMPARISON_v1` — 수수료 0.25%/0.25%, 슬리피지 10bp, 매도세 0.0000278 (P7 과 동일). 운영 추정 비용은 별도 기록 (위 '거래비용 두 가지').
- **가상 S01:** 판단 스냅샷의 손절가로 고정하고 기간 제한이 없다. 재스캔으로 손절가가 바뀌어도 쓰지 않는다.
  - 체결 당일 저가 ≤ 손절 → 손절가 청산 (`STOP_DAY0`, 보수 가정)
  - 이후 시가 ≤ 손절 → 시가 청산 (`STOP_GAP`), 저가 ≤ 손절 → 손절가 청산 (`STOP`)
  - 가격 미확정 포지션은 판정 보류 (복구 시 재계산)
  - 거래량 0 날은 손절 체결 없음, 재개 첫 정상 봉에서 위 규칙으로 판정 (위 '거래량 정책')
- **체결 ledger:** `ab_fill_ledger` PK `(order_id, cum_filled_qty)`. 증분만 반영, 같은 누적값 재전달 무시, 감소는 이상(`FillAnomaly`). 첫 (부분) 체결 수량부터 가상 S01 대상.
- **매수 잔량과 손절 충돌:** 손절 트리거 시 매수 잔량을 먼저 취소한다 (`CANCELLED_BY_STOP`, 예약 해제). 그 뒤 체결 수량만 청산한다.
- **물리적 분리:** 모든 가상 주문·체결·포지션·손절 행은 `is_virtual=1` CHECK. 저장 파일이 운영 DB·S01 저널과 다르다. S01 운영 원장·스케줄러에 가상 포지션을 넣지 않는다.

## 일일 처리와 멱등성

- **실행 시간:** job 은 월~토 17:00~23:59 ET 에 N분(기본 20, 5~60)마다 돈다. 60분은 매시 정각이다. SHADOW job 등록이 실패해도 공용 스케줄러(S01·스캔)는 그대로 시작한다. 기존 S01 운영 일정은 바꾸지 않는다.
- **job 1회 순서:**
  1. 보충 수집·중단 스캔 기록·스캔 건수 대조 (스캔 진행 중이면 건너뜀)
  2. 판정 로직·설정 해시 확인 (다르면 처리 중단)
  3. 적격 기록 (그날 입력이 확정된 스냅샷, 늦은 스냅샷 포함)
  4. 결측 일봉 재조회 → 확인되면 새 run 재계산
  5. 처리하지 않은 거래일을 순서대로: 결정 스캔 확정(또는 신호 마감 후 NO_DECISION_SCAN) → 일봉 대기·마감 → 계좌 스냅샷 1회 → 적격 기록 재확인 → 46 계좌 (체결 → S01 → 평가 → 판단·예약) → `ab_days`
- **재실행:** `ab_daily(run_id, account_id, day)` 가 있으면 건너뛴다. 계좌·거래일 처리는 한 트랜잭션이다. 한 계좌가 중간에 실패하면 그 계좌만 롤백되고 다음 실행에서 같은 입력으로 다시 처리한다.
- **API 호출:** 계좌 조회(OPS)는 거래일당 1회, 정규장 마감 후 그날 처리 때만, 기존 60초 캐시 읽기 전용 함수로 한다. 장중에는 호출하지 않는다 (S01 우선). 일봉은 키움을 쓰지 않는다 (yfinance 일 1회 묶음 + 결측 재조회).

### 하트비트 (거래소 캘린더 기준)

- `status`: 스케줄러 cron 기준 마지막 예정 실행 시각 이후 job 기록이 없으면 `STALE` (2분 여유).
- `processing`: 가장 최근 정규장(휴장일 건너뜀)을 처리했으면 `CURRENT`, 데이터 마감 + 30분이 지났는데 처리하지 않았으면 `LAGGING`, 그 전이면 `WAITING`. 조기폐장일은 데이터 마감이 당겨진다.

## 저널과 Migration (S2, 별도 파일)

- 테이블 (`ab_*`):
  - 수집: `runtime`, `scan_captures`, `signal_snapshots` (불변 트리거)
  - 거래일 입력: `day_inputs` (고정 트리거), `bars` (불변 트리거), `eligibility`
  - 계좌·run: `accounts`, `runs`, `account_runs`, `decisions`, `orders`, `reservations`, `fill_ledger`, `positions`, `stop_events`, `daily`, `days`
  - `events`
- 런타임(Scanner hook, job, API)은 DDL 을 실행하지 않고 파일도 만들지 않는다. schema 확인은 `mode=ro`, 쓰기는 `mode=rw`.
- **운영 DB `alembic/`(head `d4e8a17c5b92`)과 S01 저널 Migration(`as0001`)은 바꾸지 않았다.**
- **권한:** 위 'Migration 파일 권한'. `--check` 는 0600 이 아니면 실패하고(바꾸지 않음), 적용 모드는 생성·upgrade·`ALREADY_CURRENT` 모두 0600 을 보장하며, chmod 전 파일 검증을 한다.
- **`--env-file`:** `.env` 를 셸에서 실행하지 않고 `dotenv_values` 로 경로 키 3개(`AUTO_BUY_SHADOW_JOURNAL_PATH`, `SCANNER_ORDER_JOURNAL_PATH`, `ALLOCATION_JOURNAL_PATH`)만 읽는다. 값은 출력하지 않는다.

| 명령 | 동작 |
|---|---|
| `--check` | 아무것도 만들지 않고 권한도 바꾸지 않는다. 파일이 없으면 종료 코드 2. 저널·부속 파일 권한이 0600 이 아니면 `CHECK_FAILED_PERMISSIONS`·종료 코드 2 (최신이어도). 기존 파일이 최신이 아니면 임시 사본에서 upgrade 를 시험해 실패하면 종료 코드 2 (불완전 `ab_*` 스키마 등). 최신이면 `CHECK_ONLY_ALREADY_CURRENT` |
| `--check --allow-create` | 아무것도 만들지 않고 생성 가능 여부(상위 디렉터리 존재·쓰기 가능, 다른 저널과 겹치지 않음)만 확인. 결과 `CHECK_ONLY_WOULD_CREATE` |
| `--allow-create` | 파일이 없을 때만 새로 만든다 |
| (옵션 없음) | 기존 파일 백업 후 upgrade. 이미 최신이면 `ALREADY_CURRENT` (권한은 0600 으로 맞추고 보고서를 남김) |

- **종료 코드:** 0 성공, 2 사전 점검 실패(권한 불일치·파일 검증 실패 포함), 3 Migration 실패(롤백) 또는 상위 디렉터리 없음, 4 사후 검증 실패 또는 권한 변경·확인 실패.
- **중단하는 경우:** 대상이 운영 저널과 같은 파일(경로·심볼릭 링크·하드링크 inode 비교), 상대경로, 대상에 `ab_` 아닌 객체, 무결성 검사 실패, 사본 upgrade 시험 실패.
- 스키마 비교 기준은 메모리 DB 에 해당 리비전 DDL(`ab0001` = v1, `ab0002` = v1 + `ddl_v2`, `ab0003` = + `ddl_v3`)을 실행해 만든다 (대상 파일에는 DDL 을 실행하지 않음).
- **리비전:** `ab0001`(공개 후 동결, 변경 없음) → `ab0002`(거래량). `ab0002` 는 `ALTER TABLE … ADD COLUMN` 만 한다: `ab_bars.volume`·`volume_status`(CHECK), `ab_fill_ledger.bar_volume`, `ab_positions.zero_volume_days`(기본 0)·`last_zero_volume_day`. 시작 전에 대상이 정확히 `ab0001` 구조인지 확인하고 아니면 중단한다. downgrade 는 지원하지 않는다. 기존 행은 그대로이며 거래량은 NULL(미확인)이다.
- **`ab0003`** (최종 목표 리비전): `ab_bars.volume_source`·`source_evidence` 열 추가(`ALTER TABLE … ADD COLUMN`)와 run fencing 트리거(위 '재계산 실행권 v2'). 시작 전에 대상이 정확히 `ab0002` 구조인지 확인한다. downgrade 는 지원하지 않는다. 기존 행의 출처는 NULL 이며, 그 행의 거래량 0 은 미확인으로 보고 원본 경로로 다시 확인한다.
- 런타임은 `ab0003` 만 받아들인다. `ab0001`·`ab0002` 저널은 schema 미준비로 보고 처리하지 않는다 (Migration 실행은 별도 승인 — 이번 작업에서 실행하지 않음).
- 기존 파일이면 백업한다. 실패해도 자동 복원하지 않는다. 새로 만든 뒤 upgrade 가 실패하면 빈 0600 파일이 남는다 (다음 실행에서 이어서 upgrade).

## AUTO BUY MONITOR (화면)

기존 화면 스타일을 쓰고, 조회만 한다. 표시 항목:
- 모드, 설정 오류, 저널·Migration 상태, 하트비트(예정 실행 기준 STALE·처리 지연, 최근 정규장·조기폐장), 마지막 job·최근 오류
- **실제 자동매수 주문 0건** (실주문 경로 없음, 가상 주문 수, 비가상 행 수 = 0 확인)
- 판정 로직 해시(현재·기록), 현재 run, 재계산 진행 여부, 버전 불일치로 처리 중단, **비용 기준(비교용·운영 추정 구분)**
- 미국 스캔 대조: 전체·수집(완전)·진행 중·실패·중단·복구 불가·미수집·확정 후 스캔
- 전향적 검증 진행: 거래일 5/20/60, B01 종료 거래 n/40, **기간 데이터 완전/미완료**, 일봉 미완료일, 결정 스캔 없는 날
- **S5 패널** (아래 기준과 현재 값)
- 프로필별 전략 계좌 표: 누적수익률, MDD, PF, R, 종료·보유, 주문·체결률, Entry/Current Heat, Sleeve, 최대 종목·섹터, 현금·예약, **데이터 상태·미확정·전액 손실 가정**, 매수 차단, B02 분포와 B01 백분위
- 가격 미확정 포지션·체결 불명 주문 표 (분류는 판정 아님, 자동 복구 중 / `MANUAL_REVIEW`, 복구 기간이 지난 결측 일봉 목록)
- 계좌 전체 (OPS)
- 계좌 상세: 평가금액·Heat 추이, 가상 주문, **가상 체결 근거(OHLC·공급자·리비전·조회 시각)**, S01 가상 손절, 가상 포지션(가격 미확정 표시), 매수 판단·사유
- Scanner 신호 수집 이력: **상단 S1 한계 안내**, 완전/누락 가능·복구 가능성·확정 후 표시, 확정 이후 스캔 목록(건수·종목·스캔 시각·사유)
- 적격·제외 사유 집계, 판단일별 신호 목록(역할 표시)
- 재계산 이력 (run, 사유, 결과 달라진 계좌, 최초 차이일, 손절 전후)
- 오류·누락·경고 이벤트

## 검증 일정과 S5 채택 검토 기준 (평가 시작 전 고정)

`settings.S5_CRITERIA` 에 고정하고 설정 해시에 포함한다. 바꾸려면 버전을 올린다.

| 시점 | 확인 | 통과 기준 |
|---|---|---|
| 5거래일 | 기능·안전성 | **연속 5거래일 정상 운영** (위반 시 기록 보존 후 이후 연속 5거래일 재평가). 스냅샷 누락 0, 중단·복구 불가 0, 비가상 행 0, job 오류 0, 버전 중단 0, S01 지표(tick 지연·429) 변화 없음 |
| 20거래일 | 신호 수집·가상 체결·위험 계산 | 적격 판정·가상 체결을 백테스트 로직으로 재계산하면 일치. Heat·예약·차단 집계 일관. 일봉 미완료일 원인 기록 |
| 60거래일 | 전향적 성과 **중간** 평가 | 아래 성과 기준을 표시만 한다 |
| B01 종료 거래 40건 이상 | 전략 평가 **검토 시작** | 아래 기준 전체 검토 |

성과 기준 (같은 프로필의 B00-N 대비, **미청산 평가손익 포함**):
- B01 누적수익률 ≥ B00-N
- PF > 1.0 이고 ≥ B00-N
- 평균 R > 0 이고 ≥ B00-N (95% 신뢰구간 함께 표시)
- MDD 악화 ≤ 2%p
- B01 이 B02 분포 백분위 ≥ 75 이면 순위 효과 검토
- 시장 국면 편중(결정 스캔 market_condition 분포)과 통계적 불확실성 확인
- 안전 위반 0 — **관찰 기간 전체** 운영 안전(비가상 행, ERROR 이벤트(job·스냅샷·재계산 실패, 버전 불일치), heartbeat 지연, 버전 차단, 재계산 비정상 종료·응답 없음) + 데이터 완전성(중단·복구 불가·미수집 스캔, DATA_INCOMPLETE 일, FILL_UNKNOWN, 미확정 포지션, 수동 검토 일봉)의 합계. 재계산 복구로 지우지 않는다. 현재 미해결·복구됨·복구 불가를 따로 표시
- 필터 효과 = B01 vs B00-N, 순위 효과 = B01 vs B02
- PF 는 P7 과 같은 거래별 총이익 ÷ 총손실이다 (총손실 0 이면 없음). 미청산 종목은 종목별로 매도 비용(수수료·매도세·슬리피지)을 뺀 마지막 확인 종가 평가로 포함하고 서로 상계하지 않는다.

판정은 유지한다: NOT_RECOMMENDED, B01 = NEED_MORE_DATA. **40건 확보나 기준 충족만으로 LIVE 를 승인하지 않는다.** 표본이 부족하거나 시장 국면이 치우치면 기간을 연장한다. 미달·이상이면 중지하고 원인을 기록한 뒤 버전을 올려 재시작한다 (기존 계좌 수치를 덮어쓰지 않는다).

## 배포 절차 (Codex 재리뷰 통과 후, 단계별 별도 승인)

원칙: `.env` 를 셸에서 실행(`source`, `set -a; . ./.env`)하지 않는다. 환경 값 원문을 화면·로그에 출력하지 않는다. 확인은 '설정됨/없음'·허용된 열거 값만 출력한다.

1. **OFF 배포 (승인 후):** 기본값 그대로 배포한다 (`AUTO_BUY_US_MODE` 미설정 = OFF). 다음을 확인한다.
   - 진행 중 스캔 없음, 배포 대상 커밋
   - 운영 DB Alembic head 변경 없음, S01 저널 Migration 변경 없음
   - `/api/auto-buy/health` 가 `mode=OFF`, `schema_ready=false`, 저널 파일 없음
   - 환경 확인 (값 출력 없음):
     ```bash
     .venv/bin/python - <<'PY'
     from dotenv import dotenv_values
     v = dotenv_values(".env")
     mode = (v.get("AUTO_BUY_US_MODE") or "").strip().upper()
     print("AUTO_BUY_US_MODE:", mode if mode in ("", "OFF", "SHADOW", "LIVE") else "INVALID")
     for k in ("AUTO_BUY_SHADOW_JOURNAL_PATH", "SCANNER_ORDER_JOURNAL_PATH", "ALLOCATION_JOURNAL_PATH"):
         p = (v.get(k) or "").strip()
         print(k + ":", "unset" if not p else ("set, absolute" if p.startswith("/") else "set, NOT absolute"))
     PY
     ```
2. **⛔ 저널 생성 (승인 후):** 영속 로컬 디스크의 절대경로를 정한다 (운영 저널과 다른 파일).
   ```bash
   .venv/bin/python -m scripts.auto_buy_shadow_journal_migrate --env-file .env --backup-dir /var/backups/mystockapp --check --allow-create; echo "exit=$?"
   # 결과 CHECK_ONLY_WOULD_CREATE · exit=0 확인 후 별도 승인
   .venv/bin/python -m scripts.auto_buy_shadow_journal_migrate --env-file .env --backup-dir /var/backups/mystockapp --allow-create; echo "exit=$?"
   ```
   - 결과의 `schema_version` 이 **`ab0003`**(최종 목표 리비전)인지 확인한다.
   - 생성 후 권한 확인: `stat -c '%a %U' <저널 경로>` → `600 <서비스 사용자>` (경로는 운영자가 직접 입력, 출력은 권한만).
   - 백업에서 복원한 저널이면 먼저 `--check` 로 권한을 확인한다. 0600 이 아니면 `CHECK_FAILED_PERMISSIONS`(exit=2)이다. 승인 후 적용 모드를 다시 실행하면 `ALREADY_CURRENT` 와 함께 0600 으로 맞춰진다.
2-1. **⛔ 활성화 전 조건 (모두 확인 후 별도 승인):**
   - 복원·이전한 저널(있다면)의 `--check` 가 권한 포함 통과 (exit=0, 0600)
   - 수집 경로 검증: 원본 응답 주입 회귀 테스트(`tests/test_auto_buy_shadow_review4.py`)가 배포 커밋에서 통과. 원본 `volume:null`·필드 없음은 `UNKNOWN`, `volume:0` 만 확정 0 인지 본다. 실제 Yahoo 호출 1회 점검은 별도 승인 시에만 하고, 결과 원문은 보고서에 붙이지 않는다.
   - 장시간 BUILDING 실행권 검증: `tests/test_auto_buy_shadow_review4_lease.py`(901초 이상 정상 작업자·pid 재사용·다른 호스트·시작 식별자 확인 불가·SIGKILL·실행권 상실 후 재개·동시 replay)가 배포 커밋에서 통과
   - VM 에서 프로세스 시작 식별자와 부트 식별자를 읽을 수 있는지 확인. 못 읽으면 `ALIVE_UNVERIFIED` 로 보수적으로 보호된다 (pid 재사용 판정 없음).
3. **⛔ SHADOW 활성화 (승인 후):** `AUTO_BUY_US_MODE=SHADOW` 로 재시작한다. 확인:
   - 스케줄러 job `auto_buy_us_shadow` 등록, S01 job 일정 변화 없음
   - 다음 정규 미국 스캔 후 수집 이력·후보 확정(`day_inputs`) 표시, 스캔 대조 미수집 0
   - 데이터 마감 이후 하트비트 `CURRENT`
4. 백업 cron 에 SHADOW 저널을 추가할지 정한다 (가상 기록이라 선택).

참고: 기존 S01 runbook(`docs/vm_shadow_runbook.md`)은 `set -a; . ./.env` 방식을 쓴다. S01 문서라 이번 범위에서 바꾸지 않았다. 같은 원칙으로 고칠지 별도로 정한다.

## 남은 승인 항목

| ID | 항목 | 상태 |
|---|---|---|
| S1 | 운영 DB 스냅샷 표(outbox) | **미승인 유지.** 별도 SHADOW DB 방식. 완전 보장이 필요하면 outbox Migration Plan 을 따로 승인 요청 |
| S2 | SHADOW 전용 SQLite·Migration | 구현 완료 (`ab0001`·`ab0002` 동결 + `ab0003` 거래량 출처·fencing, 최종 목표 `ab0003`), **Migration 실행 미승인** |
| S3 | 스위치·SHADOW job 배포 (기본 OFF) | 구현 완료, Codex 재리뷰 대기 |
| S4 | 가상 계좌 구성 | P7 비교형 + 운영 검증형, B02 seed 20개 (총 46 계좌) |
| S5 | 채택 검토 기준 | **확정** (위 기준 유지) |
| P-1 | 장기 미확정 포지션 정책 | **확정:** 차단 유지, 자동 청산·자동 해제 없음, 20 거래일 후 수동 검토. 거래정지·상장폐지 확인 시 평가 방법은 수동 검토 때 별도 승인 |
| P-2 | 정규 스캔 실패일 대체 결정 스캔 | **확정:** 허용하지 않음 (`NO_DECISION_SCAN`) |
| P-3 | 신호·데이터 마감 시각 | **확정:** 22:00 / 23:20 ET, 조기폐장 시 당김. 조정은 버전 변경 |
| P-4 | 결측 복구 기간 | **확정:** 20 거래일, 이후 불완전·위험 유지 + `MANUAL_REVIEW` |
| P-5 | 계좌 대조 허용 오차 | **확정:** max($5, 0.5%) 초기 검증 기준 |
| P-6 | 비용 가정 | **확정:** 비교는 P7 과 같은 비용, 운영 추정 비용은 별도 기록(미검증) |
| P-7 | 운영 추정 비용 검증 | 증권사 실제 수수료·환전·규제 수수료 확인 — LIVE 검토 전 필요 (미완료) |
| A1~A5, A7, A8, A11 | LIVE 연동 항목 | **미승인 유지** (운영 S01 변경 필요) |
| A6 | 수동 보유 정책 | SHADOW 표시 정책 반영. **LIVE 전 계좌 전체 위험 차단 정책을 확정해야 한다** |
| A9 / A10 | Entry Heat 채택 / Current Heat 표시·경고 | SHADOW 반영 |
| A12 | SHADOW 저널 분리 | 반영 (실제 VM 경로는 배포 때 결정) |

## 한계

- S1: outbox 없이 완전 수집을 보장하지 못한다 (위).
- 가상 체결은 일봉 기준이다. 재현하지 않는 것: 장중 시세 유효성, 호가 공백, 부분 체결, 주문 거부, 주문 직전 실시간 확인. 체결 당일 손절 순서는 보수 가정이다.
- 일봉 공급자(yfinance)는 하나다. 공급자 정정(사후 수정 봉)은 감지하지 않는다 (확인된 OK 봉은 다시 받지 않는다).
- 거래정지·상장폐지·공급자 누락을 구분하는 근거 데이터가 없다. 결측 기간으로 '의심' 표시만 한다. 20 거래일이 지나면 자동으로는 해소되지 않는다 (수동 검토).
- 가상 손익은 비교용 비용 기준이다. 실제 운영 비용(환전 포함)은 확인되지 않았다.
- 재계산은 시작일부터 전체를 다시 계산한다 (거래일당 약 0.2~0.7초 × 처리 일수). 수백 거래일이 쌓이면 시간이 늘어난다.
- OPS 의 계좌 상태는 장 마감 후 하루 1회 값이다. 수동 보유 손절가는 알 수 없어 모두 미확정 위험으로 본다. 수동 보유 섹터는 SHADOW 스냅샷에 있는 종목만 안다.
- 프로세스 시작 시각은 psutil 또는 Linux `/proc` 로 읽는다. 둘 다 없으면 '이후 스캔 존재'·'6시간 경과' 조건만 쓴다.
- B02 seed 20개는 분포가 거칠다. 기간 말에 불변 스냅샷으로 1,000회 재계산해 보완할 수 있다.
- **P7 대조 범위:** 실제 재구성 데이터 1,480 거래일에서 P7C 23 계좌가 P7-OPS 와 일치한다 (위 'P7 운영 정책 비교판'). 이전 리뷰의 잔여 차이(지정가 단위·거래량 0·마지막 날 판단 등)는 정책 차이로 분리해 기록했다. 기존 P7 과의 차이는 정책 차이이며 버그로 보지 않는다.
- 일봉 거래량은 공급자 값 그대로다. 거래량이 있어도 지정가 가격에 실제로 체결됐는지는 알 수 없다. 거래 재개 갭 손실은 1R 을 넘을 수 있다.
- 원본 null 과 확정 0 의 구분은 `ab0003` 이후 원본 수집 경로에서만 가능하다. 재구성 캐시·기존 저장 행은 원본을 되살릴 수 없다. 그래서 P7-OPS 일치는 같은 변환 캐시 입력에서의 **엔진 정합성**이며, 원본 데이터 정확성이나 새 과거 성과 검증이 아니다.
- 원본 chart 응답 형식은 Yahoo 비공식 API 다. 형식이 바뀌면 결측·미확인으로 처리된다 (자동 체결하지 않음).
- 다른 호스트의 실행권은 사망을 확인할 수 없어 자동 정리하지 않는다. 응답 없는 작업자는 수동 확인 후 해제해야 하며, 그동안 재계산은 대기한다.
- OPS 계좌의 과거 손익은 당시 계좌 스냅샷이 없어 역사 데이터로 검증할 수 없다.
