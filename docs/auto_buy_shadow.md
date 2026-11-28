# account1 미국 Scanner 자동매수 SHADOW

2026-11-25 작성, 같은 날 재리뷰 지적(P1 3건·주요 P2) 반영, 남은 운영 정책 확정 (아래 '운영 정책').
같은 날 Draft PR #14 독립 리뷰(FAIL: P1 6건·P2 7건)를 feature branch 에서 반영했다 (아래 '독립 리뷰 반영'). 공개 이력 항목은 이력 재작성 승인이 필요해 보류.
2026-11-26 Codex 재리뷰(2차, FAIL: 신규 P1 1건·P2 3건)를 같은 브랜치에서 반영했다 (아래 'Codex 재리뷰(2차) 반영').
같은 날 Codex 최종 재리뷰(신규 P1 1건·P2 2건·S5 이력)를 반영했다 (아래 'Codex 최종 재리뷰 반영').
2026-11-27 Codex 재리뷰(head 9a315db, FAIL: P1 1건·P2 4건·운영 안전성)를 공통 안전 원칙과 장애 주입 테스트로 반영했다 (아래 'Codex 재리뷰 반영 (2026-11-27)').
같은 날 Codex 재리뷰(head c27d9d0, 신규 P1 2건·P2 1건)를 반영했다 (아래 'Codex 재리뷰 반영 (2026-11-27, head c27d9d0)').
같은 날 Codex 독립 재리뷰(head 36e5d2f, P1 3건·P2 2건)를 테이블별 데이터 불변성 계약으로 반영했다 (아래 'Codex 재리뷰 반영 (2026-11-27, head 36e5d2f)').
2026-11-28 Codex 재리뷰(head a183b99, FAIL: P1 1건·P2 2건·검증 데이터)를 신호 집합·관계 단위 계약(`ab0007`)과 엔진 공통 무결성 검증으로 반영했다 (아래 'Codex 재리뷰 반영 (2026-11-28, head a183b99)'). SHADOW 활성화는 계속 보류다.
같은 날 Codex 독립 재리뷰(head d95beab, FAIL: P1 1건·P2 2건)를 적격 판정 집합 완전성, 계좌별 쓰기 트랜잭션 안의 전체 재검증, 원본 row_json ↔ 정규화 필드 계약으로 반영했다 (아래 'Codex 재리뷰 반영 (2026-11-28, head d95beab)'). 스키마 변경은 없다 (새 리비전 없음). SHADOW 활성화는 계속 보류다.
- **승인 범위:** 로컬 구현·테스트 (사용자 승인: P7/P8 검토 후 I1~I7 SHADOW 구현, 이후 P1·P2 정합성 보완), feature branch `feature/auto-buy-shadow` 커밋·Push·Draft PR.
- **아직 하지 않은 것:** main 병합, VM 배포, 저널 Migration 실행. Codex 재리뷰를 통과하기 전에는 기본 OFF 배포도, SHADOW 활성화도 하지 않는다.
- **판정은 그대로 유지한다.**
  - 기존 NOT_RECOMMENDED
  - B01 = NEED_MORE_DATA (전향적 SHADOW 검증 대상만)
- **근거:** `backtest/entry_strategy` P0~P7 보고서, `backtest/entry_strategy/docs/AUTO_BUY_SHADOW_PLAN.md`, `AUTO_BUY_S01_LINK_DESIGN.md` v2
- **저널 스키마:** `ab0001`(공개 후 동결) + `ab0002`(거래량, 2026-11-26, 동결) + `ab0003`(거래량 출처·근거, run fencing, 2026-11-26, 동결) + `ab0004`(종료 run 원장·감사 이력 불변성, 2026-11-27, 동결) + `ab0005`(충돌 교체 차단·남은 감사 자료 불변, 2026-11-27, 동결) + `ab0006`(테이블별 데이터 불변성 계약, 2026-11-27, 동결) + `ab0007`(신호 집합·관계 단위 계약과 수집 봉인, 2026-11-28). 최종 목표 리비전은 `ab0007`. 아래는 `ab0001` 의 공개 전 이력이다 — 배포·커밋 전이라 첫 재리뷰 반영 때 같은 리비전을 **제자리에서 교체**했다 (run 단위 상태, 거래일 입력·일봉 표 추가).
  - 확인(2026-11-25): Git 이력에 `trading/auto_buy` 커밋 없음(원격도 main 뿐), 로컬 `.env` 저널 경로 미설정, 프로젝트 내 SQLite 에 `ab_alembic_version` 없음. 이전 `ab0001` 이 적용된 파일은 개발 세션 임시 폴더의 데모·벤치 파일뿐이다. 운영 VM 은 Git 으로만 배포되고 이 코드가 Push 된 적이 없어 적용 이력이 없다 (VM 에 접속해 확인하지는 않았다).
  - 이 브랜치가 커밋·Push 된 이후에는 `ab0001` 을 바꾸지 않고 새 리비전으로만 바꾼다.

## Codex 재리뷰 반영 (PR #14, 2026-11-28, head d95beab)

판정: FAIL (P1 1건·P2 2건, 모두 재현). 범위는 이 세 결함과 전체 검증 재실행으로 제한했다. 투자 전략, 매매 계산, 리스크 정책, UI 기능, 기존 S01 은 바꾸지 않았다. 공개된 `ab0001`~`ab0007` 리비전·DDL 은 그대로다. 스키마 변경이 필요 없어 `ab0008` 은 만들지 않았다 (모두 애플리케이션 코드·Migration 진입점에서 처리).

### d95beab 에서 막히지 않던 것 (조사 결과)

- **P1 적격 판정 완전성:** `eligibility_pending()` 은 스냅샷에 현재 버전 판정이 **하나라도** 있으면 완료로 셌다. 공통 검증은 **저장된 판정만** 다시 계산했다. 그래서 B00 판정만 지우면(B01 유지) 대기 0, 검증 통과, 후보 조회는 B00 후보 없음이 됐다. replay 는 기존 B00 가상 주문이 빠진 새 CURRENT 를 만들었다. 현재 버전 판정이 모두 없어지고 과거 버전만 남으면 `record_eligibility` 가 다시 만들어 누락을 숨겼다.
- **P2 계좌별 쓰기:** 계좌 트랜잭션의 `fingerprint` 는 저장된 봉인 해시를 다시 읽고 행 수만 셌다. 후보 검증 이후 구성원 수를 유지한 채 `stop_loss` 를 바꾸면 46 계좌가 모두 커밋된 뒤에야 일 기록 단계에서 막혔다. 적격 판정 변경은 지문에 없어 아예 막히지 않았다.
- **P2 기존 데이터 봉인:** `row_sha256` 은 `row_json` 만 보호한다. ab0006 저널에서 `row_json`·`row_sha256`·건수를 그대로 두고 정규화 컬럼(`stop_loss`)만 바꾸면 ab0007 사전 검사를 통과했다. backfill 은 그 값을 SSV2 로 봉인했고, 이후 런타임 검증(SSV2 = 봉인)도 통과했다.

### 1. 적격 판정 집합 완전성 (`integrity.eligibility_state`)

기대 집합은 결정 스캔 구성원마다 현재 전략 버전 (B00, B01) 정확히 1개씩이다. 이것을 실제 저장 집합과 대조하고, 있는 판정은 모두 같은 스냅샷 재계산과 비교한다.

| 상태 | 분류 |
|---|---|
| B00·B01 모두 있음, 재계산 일치 | 완료 |
| eligible=0 정상 부적격 판정 | 유효한 판정 (완료) |
| 정상 빈 결정 스캔 | 유효한 빈 집합 (완료) |
| 현재 버전 판정 전부 없음, 아직 어떤 현재 버전 run 도 쓰지 않음 | 정상 미완료 (pending) → 최초 생성 허용 |
| 과거 버전 판정만 있음, 미사용 (버전 전환 직후) | 정상 미완료 (pending) → 최초 생성 허용 |
| B00 만 / B01 만 / 일부 구성원만 | 오류 `ELIGIBILITY_INCOMPLETE` |
| 현재 버전 판정 전부 없음, 이미 사용됨 (과거 버전만 남은 경우 포함) | 오류 `ELIGIBILITY_MISSING_AFTER_USE` |
| 저장 판정 ≠ 재계산 | 오류 `ELIGIBILITY_MISMATCH` |

- **'사용됨'의 정의** (`integrity.eligibility_used`): 현재 버전 run(상태 무관)에 그날 `ab_days` 나 `ab_daily`(일부 계좌만 처리된 날 포함)가 있거나, 이 스캔 구성원에 대한 `ab_decisions` 가 있으면 사용된 것이다. 이것으로 "아직 만들지 않은 정상 상태"와 "확정된 판정의 누락·훼손"을 구분한다.
- 일부 누락은 사용 여부와 상관없이 오류다. `record_eligibility` 는 한 트랜잭션에서 구성원 전체를 기록하므로 정상 경로에서는 생기지 않는다.
- **자동 재생성 금지:** `record_eligibility` 는 기록 전에 공통 검증을 한다. 그래서 사용된 날의 누락이나 일부 누락을 다시 만들지 않고 멈춘다.
- **적용 경로** (모두 `decision_set_issues` 하나를 쓴다):
  - `eligibility_pending`: 오류면 감사 기록 후 예외
  - 후보 조회(`candidates`): 완전하고 재계산과 일치할 때만 돌려준다. 아니면 후보 0건으로 확정하지 않는다
  - `build_inputs`
  - 신규 거래일 처리 (계좌별·일 기록 트랜잭션)
  - 중단 후 재실행, replay
  - CURRENT 전환 직전 최종 검증
- 불일치 시 처리는 기존과 같다: 기존 CURRENT·원장 보존, 처리 중단, ERROR `signal_set_integrity`, MONITOR 표시, S5 안전 위반. 불완전 판정으로 기존 가상 주문이 빠진 새 CURRENT 를 만들지 않는다.

### 2. 무결성 검사와 계좌별 쓰기 원자성 (`integrity.check_inputs_current`)

계좌 하루 처리 트랜잭션(BEGIN IMMEDIATE) 안에서, 원장 쓰기와 같은 트랜잭션으로 다음을 **다시 계산해** 확인한다. 저장된 봉인 해시를 다시 읽는 것만으로는 통과시키지 않는다.

- 공통 검증 전체:
  - 실제 SSV2 재계산 = 봉인
  - 구성원별 `sha256(row_json) = row_sha256`
  - 원본 ↔ 정규화 필드 (아래 3)
  - 동결 v1 해시 재계산
  - 적격 판정 완전성·재계산
- 지문(`fingerprint`)이 `build_inputs` 때와 같은지. 지문은 실제 재계산 값이다: 실제 SSV2, 봉인 다이제스트, 건수, v1 저장값·재계산값, 현재 버전 적격 판정 다이제스트.
- `build_inputs` 이후 후보 집합(행 내용 전체)이 바뀌지 않았는지, 후보가 모두 그날 결정 스캔 구성원인지.

같은 쓰기 트랜잭션이므로 검증과 계좌 원장 쓰기 사이에 다른 연결이 쓸 수 없다. 일 기록(`ab_days`) 트랜잭션도 같은 검증을 한다.

**트랜잭션 구조는 바꾸지 않았다.** 46 계좌를 한 장시간 쓰기 트랜잭션으로 묶는 방안을 검토했고, 다음 이유로 채택하지 않았다.

1. 그동안 Scanner hook 수집 쓰기가 잠금을 기다리다 30초 timeout 에 걸릴 수 있다.
2. 재계산 heartbeat 가 같은 연결의 미커밋 트랜잭션에 묶인다. 다른 프로세스에는 보이지 않으므로 실행권이 응답 없음으로 오판될 수 있다.
3. 중단 후 재실행의 계좌 단위 멱등(`ab_daily` 가 있으면 건너뜀)이 사라진다.

fencing 트리거에는 영향이 없다. 계좌별 커밋은 유지하고, 아래 복구 정책으로 불완전한 날을 격리한다.

### 일부 계좌 처리 후 오류 — 복구 정책 (`integrity.quarantine_day`)

| 시점 | 결과 |
|---|---|
| 첫 계좌 처리 전 변경 | 첫 계좌 트랜잭션 롤백. 신규 주문·체결·포지션·예약·판단·`ab_daily`·`ab_days` 커밋 0건 |
| 일부 계좌 커밋 후 변경 | 앞선 계좌 기록은 **보존(삭제 없음)**. 오류 계좌부터는 기록 없음. 그날 `ab_days` 미기록 (COMPLETE 아님) |
| CURRENT 신규 거래일 처리 중 | runtime `integrity_day_block`(run·거래일·커밋된 계좌·사유)과 ERROR `integrity_day_block` 기록. 표시가 있는 동안 job 은 **신규 거래일 처리·재계산을 하지 않는다** (수집은 계속). 자동 해제 없음 — 운영자 확인 후 수동 해제 |
| BUILDING run(재계산) 중 | 기존과 같이 그 run 을 FAILED 로 남기고 기존 CURRENT 유지 (정상 승격 차단). 격리 표시는 남기지 않는다 |
| CURRENT 전환 직전 | 전환 트랜잭션의 최종 검증이 막는다 (`replay_current_switch`). run FAILED, 기존 CURRENT 유지 |

- **성과:** `account_metrics` 는 `ab_days` 가 있는 거래일의 `ab_daily` 만 쓴다. 격리된 날은 정상 성과에서 빠진다.
- **S5·MONITOR:** S5 `integrity_day_block` 은 관찰 기간·현재 미해결 모두 위반이다. MONITOR 는 기존 '동결 신호 집합 무결성 오류' 표시를 그대로 쓴다 (UI 변경 없음).
- **기존 복구 정책과의 관계:** 일반 계좌 실패(예외)는 그 계좌만 롤백되고 다음 job 이 이어서 처리한다. 이 정책은 그대로다. 무결성 오류만 예외로, 이어서 처리하지 않고 격리한다. 오염 데이터로 나머지 계좌를 처리하지 않기 위해서다. 커밋된 기록을 지우거나 해시를 다시 만들어 정상화하지 않는다.

### 3. 원본 row_json ↔ 정규화 필드 계약 (`raw_contract.py`)

원본에서 재현 가능한 필드를 실제 수집 코드와 같은 규칙으로 다시 계산해 **정확히** 대조한다. 같은 연산이므로 허용 오차는 없다. 정상 정규화는 오탐하지 않는다: 문자열 → 실수, NaN·무한대 → NULL, 피벗 0·손절가 없음 → 파생값 NULL, 경고 문자열 → 목록.

| 원본 형식 (`capture_source`) | 대조 필드 |
|---|---|
| `SCANNER_ROW` (HOOK·CATCHUP, 운영 `ScanResult` 행 전체) | scan_result_id, signal_key, market, ticker, name, signal_type, signal_date, first_detected_at, strict_passed, strict_reasons, grade, signal_quality, market_condition, decision_price(`price`), pivot_price, stop_loss, ext_pct·stop_pct(파생식), entry_warnings, rs_value, volume_ratio, weekly_volume_ratio, sector_name, sector_source |
| `P7_RECONSTRUCTED` (BACKTEST_REPLAY, `source=P7_RECONSTRUCTED_CANDIDATE`) | scan_result_id(`cid`), signal_key, market, ticker, name, signal_type, signal_date, is_first_detection(`first_seen`), strict_passed, grade, signal_quality, market_condition, decision_price(`close`), pivot_price, stop_loss(`stop`), ext_pct, stop_pct, entry_warnings(`warnings`), sector_name |

- **원본 근거가 부족한 필드** (정확성을 추정·보증하지 않는다):
  - 운영 형식의 최초 탐지·최초 Strict 통과 판정(`is_first_detection`·`first_strict_pass`·`first_pass_basis`): 수집 이력·캘린더 설정에 의존
  - 수집 소속(`trading_day`·`intraday`·`scan_status`·`scan_completed_at`·`late_after_decision`): 수집 기록과의 관계로 따로 검증
  - 수집 메타데이터(`scanner_commit`·`threshold_hash`·`capture_logic_hash`·`capture_source`·`created_at`)
- 다음은 오류다 (우회 경로 없음): 알 수 없는 출처, JSON 해석 불가, 운영 출처인데 P7 형식인 원본. 백테스트 형식도 같은 엄격도로 자기 원본의 모든 판단 필드를 대조한다.
- **적용:** 공통 검증(`scan_set_issues` → 동결·적격 기록·build_inputs·계좌 트랜잭션·일 기록·replay·CURRENT 전환), Migration(아래), P7-OPS 검증 저널 점검(`journal_integrity.raw_contract_issues`).
- **불일치 시:** 사용·Migration 중단, 기존 데이터·봉인 보존, 자동 수정·해시 재생성·재봉인 없음, 원인(스냅샷·필드) 기록.

### 4. Migration (스키마 변경 없음, 사전 검사 경로)

- **같은 트랜잭션의 사전 검사:** 저널 전용 Alembic `env.py` 가 BEGIN IMMEDIATE 직후, **어떤 리비전보다 먼저** 저널 전체의 원본 계약을 검사한다. 실패하면 전체 롤백된다. 그래서 ab0006 → 최신 업그레이드에서 ab0007 backfill 이 오염을 봉인하기 전에 멈추고, 원본과 리비전(`ab0006`)이 그대로 남는다. 배포 스크립트를 거치지 않은 직접 `upgrade_journal` 도 같다.
- **배포 스크립트:** 기존 저널이면(이미 최신이어도) 백업·upgrade·권한 변경 전에 읽기 전용으로 같은 검사를 한다. 실패하면 `RAW_CONTRACT_FAILED`(종료 코드 2)이고, 보고서에 문제 목록을 남긴다. `--check` 도 같다.
- **확인한 경로 (테스트):**
  - 신규 저널, ab0001 → 최신, ab0006 → 최신(정상 제품 원장), ab0007 → 최신(`ALREADY_CURRENT` + 원본 검사)
  - 재실행 멱등
  - ab0006 오염 저널 거부 (전체 덤프·리비전 보존)
  - ab0007 backfill 로 이미 봉인된 오염 저널 거부 (스크립트·런타임 모두)
  - `--check` 무변경
  - OFF 모드 파일·DDL 없음, S01 저널 보호 (기존 테스트)
- 운영 Migration 은 실행하지 않았다.

### Runbook 추가 사항

- `ab_capture_seals.source = AB0007_BACKFILL` 은 **ab0007 이관 당시 저장돼 있던 데이터 상태**를 기준으로 한 봉인이다. 과거 시점의 모든 파생 필드가 정확했다는 증거가 아니다. 원본으로 재현 가능한 필드는 위 계약으로 다시 확인한다. 원본 근거가 없는 필드(최초 탐지·최초 Strict 통과 판정 등)의 과거 정확성은 추정하거나 보증하지 않는다.
- `integrity_day_block` 표시가 생기면:
  1. 새 처리를 하지 않은 채 원인(오염 스냅샷·판정, 커밋된 계좌 목록)을 조사한다.
  2. 커밋된 기록은 지우지 않는다.
  3. 해제는 운영자 확인 후 runtime 표시를 수동으로 지우는 별도 승인 작업이다 (자동 해제·웹 진입점 없음).

### 수정 전 실패·수정 후 통과 (`tests/test_auto_buy_shadow_review9_repro.py`)

d95beab 와 현재 코드에 모두 있는 API 만 쓴다. d95beab worktree 에서는 10 실패 / 0 통과다. 실패는 모두 동작 단언 실패이며, 모듈·API 부재는 없다. 현재 코드에서는 10 통과다.

| 재현 | d95beab | 현재 |
|---|---|---|
| CURRENT 에 B00 주문 → B00 판정만 제거 → replay | 실패 (새 CURRENT 전환) | 통과 (탐지, CURRENT·주문 보존, 전환 없음) |
| B01 판정만 제거 → replay | 실패 | 통과 |
| 현재 버전 판정 제거, 과거 버전만 남김 | 실패 (재생성 후 전환) | 통과 (재생성 없음) |
| eligible=0 정상 부적격 판정 누락 | 실패 | 통과 |
| 검증 후 stop_loss 변경 (구성원 수 유지), 첫 계좌 전 | 실패 (46 계좌 커밋) | 통과 (커밋 0) |
| 검증 후 적격 판정 제거, 첫 계좌 전 | 실패 (예외 없음) | 통과 |
| 5 계좌 커밋 후 변경 | 실패 (46 계좌 커밋) | 통과 (5 계좌 보존, 격리 표시) |
| CURRENT 승격 직전 판정 제거 | 실패 (전환) | 통과 (FAILED, CURRENT 유지) |
| ab0006 stop_loss 만 오염 → Migration | 실패 (적용·봉인) | 통과 (거부, 덤프·ab0006 보존) |
| ab0007 backfill 로 봉인된 오염 저널 | 실패 (스크립트·런타임 통과) | 통과 (둘 다 거부) |

수정 후 계약 테스트는 `tests/test_auto_buy_shadow_review9_contract.py` 에 있다: 원본 계약 필드별 탐지·정상 정규화·P7 형식·알 수 없는 형식, 적격 상태 분류, 격리·차단·성과 제외, Migration 경로.

### 테스트 하네스 변경 (우회 제거)

`backtest/entry_strategy/tests/test_shadow_p7_parity.py` 는 적격 판정을 거치지 않고 후보를 직접 주입했다. B01 판정상 부적격인 `stop_pct=None` 행도 포함됐다. 새 계좌 트랜잭션 검증(`CANDIDATES_CHANGED_SINCE_BUILD`)이 이를 거부했다.

검사를 완화하지 않고, 하네스를 제품 적격 판정 경로(`record_eligibility` → `candidates`)로 바꿨다. 재구성 후보에는 P7 과 같은 정의의 `ext_pct=0`·`stop_pct`(손절폭 식)를 넣었다. 그래서 모든 후보가 B01 적격이며, P7 시뮬레이터와의 결과 대조(3 seed)는 그대로 일치한다.

### 무결성 검사 성능 영향 (측정, 검증 생략 최적화 없음)

합성 결정 스캔 1회·46 계좌를 이 작업 컨테이너(Linux)에서 측정했다. 계좌 트랜잭션마다 전체를 다시 검증하므로 비용은 구성원 수에 비례한다 (구성원 1건당 계좌마다 약 0.18ms).

| 결정 스캔 구성원 | d95beab `process_day` | 현재 `process_day` | 현재 계좌당 재검증 |
|---|---|---|---|
| 2 | 77ms | 123ms | 0.6ms |
| 50 | 141ms | 633ms | 9.2ms |
| 200 | 321ms | 2.28s | 36ms |
| 500 | 596ms | 5.58s | 91ms |

- 계좌 트랜잭션의 쓰기 잠금 유지 시간이 구성원 500건에서 약 0.1초 늘어난다 (SQLite timeout 30초 대비 여유 있음). 재계산에서는 거래일 수만큼 곱해진다.
- P7-OPS 재구성 데이터(하루 평균 약 2건)는 하루 약 +46ms, 1,480 거래일 약 +70초로 추정한다 (실데이터 미실행 — 아래).

### P7-OPS 실데이터 대조 — 이번 환경에서 미실행

- **실행하지 못한 이유:** 재구성 캐시(`backtest/exit_strategy/data/cache/ohlcv_US.pkl`·`reconstructed_US.pkl`)가 이 작업 환경에 없다. 사용자 로컬 컴퓨터도 연결돼 있지 않고, Yahoo 접근이 막혀 재구성할 수도 없었다.
- 따라서 23 계좌·1,480 거래일 실데이터 대조는 **실행하지 않았고, 통과로 보고하지 않는다.** 실데이터 테스트 3건은 캐시가 없어 skip 됐다.
- **실행한 것:** 같은 하네스·같은 무결성 경로의 합성 대조 (`test_p7_ops_parity.py` 합성 2 seed, `test_shadow_p7_parity.py` 3 seed). 신규 원본 계약은 P7 재구성 JSON 형식(`snapshot_row_json`)을 같은 엄격도로 검증하며, 합성 데이터에서 통과한다.
- **로컬 실행 방법** (캐시가 있는 컴퓨터에서, 캐시의 읽기 전용 사본으로):
  - `.venv/bin/python -m backtest.entry_strategy.run p7ops`
  - `.venv/bin/python -m pytest backtest/entry_strategy/tests/test_p7_ops_parity.py -q`
  - 기준값: 23 계좌, 1,480 거래일, 주문 3,734, 진입 3,545, 청산 2,233, 최종 보유 1,312, 거래량 0 만료 36
  - 검증 저널 보고서의 `raw_contract_issues` 가 0 이어야 한다
- 기존 P7 결과·계산 정책은 바꾸지 않았다. 판정 유지: NOT_RECOMMENDED, B01 = NEED_MORE_DATA.

## Codex 재리뷰 반영 (PR #14, 2026-11-28, head a183b99)

판정: FAIL (P1 1건·P2 2건·검증 데이터 1건). 범위는 동결 신호 집합 무결성, Migration 사전 검증, 429 집계 원자성, P7-OPS 검증 데이터로 한정했다. 투자 전략, 엔진 계산 방식, 리스크 정책, UI 기능은 바꾸지 않았다. 화면에는 무결성 오류 표시 한 줄만 추가했다.

### a183b99 에서 막히지 않던 변경 (조사 결과)

- 행 단위 계약(ab0006)이 막는 것은 이미 있는 행의 UPDATE·DELETE·rowid 변경뿐이었다. FROZEN 결정 스캔(`ab_day_inputs.decision_scan_log_id`)에 **새 스냅샷 행을 INSERT** 하는 경로는 막지 않았다 (INSERT·OR IGNORE·REPLACE·UPSERT 모두, 기본 연결 포함). 빈 결정 스캔에도 사후 신호를 넣을 수 있었다.
- 엔진도 동결 해시를 다시 계산하지 않았다. 후보 조회는 `scan_log_id` 로 그날 행을 모두 읽었으므로 사후 추가 신호가 그대로 가상 주문이 됐다. replay 는 오염된 집합으로 새 CURRENT 를 만들었다.
- 기존 동결 해시 v1 = `sha256(json.dumps(sorted(row_sha256)))[:16]` 은 각 행의 `row_sha256`(원본 JSON 의 해시)만 덮는다. 저장된 값이 정말 `sha256(row_json)` 인지는 확인하지 않았다. 스냅샷 id·scan_log_id·거래일과, 엔진이 실제로 읽는 정규화 필드(`stop_loss`·`pivot_price`·`strict_passed`·`decision_price` 등)도 해시 밖에 있었다.
- 수집 건수 `(rows_captured, strict_rows)` 는 동결 시점의 실제 집합과 묶이지 않았다. ab0006 사전 검사는 (0, 0) 이면 실제 행 수 대조를 건너뛰었다.
- `_record_rate_limit` 은 읽기와 쓰기 사이에 잠금이 없었다. 두 연결이 동시에 기록하면 증가분이 사라졌다 (1 → 2, 기대값 3).

### 집합·관계 단위 계약 (`ab0007`, `journal_migrations/ddl_v7.py`)

새 표 `ab_capture_seals` 는 수집 1회당 1행이다. 수집 트랜잭션 안에서 건수 기록 직후에 쓴다. 담는 값은 구성원 수, Strict 수, SSV2 다이제스트, source(`CAPTURE`·`AB0007_BACKFILL`), 봉인 시각이다. 봉인 행은 이후 변경·삭제·교체할 수 없다 (`_v7_` rowid·허용 목록·삭제 거부 트리거).

| 보호 | 트리거 | 내용 |
|---|---|---|
| 구성원 추가 (봉인 후) | `ab_signal_snapshots_v7_sealed` | 봉인된 수집에 스냅샷 INSERT 거부 |
| 구성원 추가 (동결 후) | `ab_signal_snapshots_v7_frozen` | FROZEN 결정 스캔으로 참조된 scan_log_id 에 INSERT 거부 (빈 스캔 포함) |
| 소속 | `ab_signal_snapshots_v7_relation` | 수집 기록이 있어야 하고, 거래일·장중 여부·스캔 상태가 같아야 함 |
| 건수·봉인 | `ab_capture_seals_v7_insert` | 봉인 INSERT 시 봉인 건수 = 수집 건수 = 실제 구성원 수 (전체·Strict) |
| 건수 고정 | `ab_scan_captures_v7_counts_sealed` | 봉인 후 수집 건수 변경 거부 |
| 참조 | `ab_eligibility_v7_relation` | 존재하는 스냅샷에만 적격 판정 기록 |
| 동결 범위 | `ab_day_inputs_v7_frozen_set` | FROZEN INSERT 는 봉인된 같은 거래일의 정규(`scheduler`)·DONE·complete·FULL·장 마감 후·확정 전 수집만 참조할 수 있음. 건수 일치, 구성원이 모두 그 거래일 소속, 해시 길이 16 |

- RAISE(ABORT) 이므로 OR IGNORE·OR REPLACE·UPSERT 충돌 해소 방식과 상관없이 문장 전체가 거부된다. 제품 연결과 기본 sqlite3 연결(외래키·recursive_triggers 꺼짐) 모두에서 같다.
- 기존 UPDATE·DELETE·rowid 보호(ab0004~ab0006)는 그대로 둔다.
- 허용하는 것: 정상 수집, 최초 동결, 정규 결정 이후 **다른 scan_log_id** 로 들어온 재스캔의 정상 수집 (`LATE_SCAN_AFTER_DECISION`·`LATE_AFTER_DECISION` 정책 유지).
- 계약 문서화: `ddl_v7.SET_CONTRACT` 에 표별 집합·관계·동결 조건을 적었다. 기존 테이블 전수 계약 테스트(`review7_contract`)가 v7 표까지 분류·검사한다. 새 프레임워크는 만들지 않았다.

### 동결 시점 검증과 경쟁

- 수집 → 건수 기록 → 봉인 → 동결은 한 BEGIN IMMEDIATE 트랜잭션이다 (`snapshot._capture`).
- `freeze_decision` 은 동결 직전에 공통 검증(`integrity.scan_set_issues`)을 한다. 항목은 수집 조건, 봉인, 건수, 소속, 구성원별 `sha256(row_json) = row_sha256`, SSV2 다이제스트다. 하나라도 다르면 동결하지 않는다.
- 빈 스캔(0건)도 정상 동결 집합이다 (봉인 0/0).
- 다른 연결이 수집 트랜잭션 도중 INSERT 하면 잠금을 기다린다. 커밋 뒤에는 봉인·동결된 집합이므로 거부된다. 불완전하거나 바뀐 집합은 동결되지 않는다 (`review8_signal_set` 경쟁 테스트).

### SSV2 기준 (기존 v1 해시는 덮어쓰지 않음)

- 저장된 `signal_set_hash`(v1)는 바꾸지 않는다. 다시 계산해서 비교만 한다.
- 별도 버전 기준 `SSV2` 다이제스트는 구성원 40개 열(id·scan_log_id·거래일·정규화 필드·파생 필드·`row_sha256` 등, `row_json` 은 `row_sha256` 을 통해 포함)을 id 순서로 직렬화한 SHA-256 이다. 봉인 표에 저장한다.
- v1 은 원본 JSON 집합을, SSV2 는 엔진이 읽는 필드와 식별자를 덮는다. 둘 다 맞아야 한다.

### 엔진 공통 검증 (`trading/auto_buy/shadow/integrity.py`)

- `decision_set_issues(db, day)` 검사 항목:
  - `scan_set_issues`
  - v1 해시 재계산
  - 저장된 적격 판정 = 같은 스냅샷으로 다시 계산한 판정 (현재 전략 버전)
  - NO_DECISION_SCAN 인데 스캔·해시가 있는 경우
- 사용 지점:
  - 적격 판정 기록 (`record_eligibility`, 트랜잭션 안)
  - 후보 조회 (`build_inputs`): 검증과 후보 읽기를 같은 BEGIN IMMEDIATE 안에서 하고 지문을 남긴다
  - 신규 거래일·중단 후 재실행·결측 복구 (`process_day`·`process_account_day`): 계좌별 처리 트랜잭션이 지문과 후보 소속을 다시 확인한다 (검증 → 사용 경쟁 차단)
  - replay: 재계산 run 의 매 거래일이 같은 경로를 거친다
  - CURRENT 전환 직전: 전환 트랜잭션 안에서 대상 거래일 전체를 다시 검증한다
- 불일치 시:
  - 신규 가상 주문 없이 처리를 멈춘다 (트랜잭션 롤백)
  - 기존 CURRENT·원장은 그대로 두고, 새 CURRENT 로 바꾸지 않는다. BUILDING run 은 FAILED 로 남기고 실패를 기록한다
  - ERROR 이벤트 `signal_set_integrity` 를 남긴다 (같은 오류 반복은 이벤트 1회 + runtime 반복 횟수)
  - runtime `signal_set_integrity` 를 기록한다. AUTO BUY MONITOR 에 빨간 줄로 표시하고, S5 안전 위반(`signal_set_integrity`)으로 센다
  - 자동 정상화는 하지 않는다: 해시 재계산 덮어쓰기·자동 삭제·재동결·신호 제외 없음. runtime 표시는 운영자 확인 후 수동으로만 지운다
- 관리자 DDL(트리거 삭제 후 수정)은 DB 계약 밖이다. 엔진 검증이 그 결과를 잡는다. 테스트는 이 방식으로 "이미 오염된 기존 저널"을 만든다.

### Migration `ab0007` 사전 검증

- ab0001~ab0006 은 바꾸지 않았다. 시작 조건은 구조가 정확히 ab0006 이고 `ddl_v7.precheck` 를 통과하는 것이다.
- 검사 항목:
  - **모든** 수집 기록의 건수 = 실제 구성원 수 (전체·Strict). (0, 0) 도 건너뛰지 않는다.
  - 스냅샷의 수집 기록 존재, 거래일·장중 여부·상태 일치
  - 구성원별 `row_sha256`
  - FROZEN 거래일: 결정 스캔이 정상 완료 정규 수집이고, 기존 v1 해시가 실제 집합과 같음
- 불완전 수집(RUNNING·ABANDONED·UNRECOVERABLE·PARTIAL)은 불완전으로 표시해 보고한다. 완료로 바꾸거나 이력을 지우지 않는다. 건수가 맞으면 통과하고, 맞지 않으면 거부한다.
- 통과하면 기존 수집 기록마다 봉인 1행(`AB0007_BACKFILL`)을 기록한다. 기존 행·해시는 바꾸지 않는다.
- 실패하면 전체 롤백한다 (원본 sha256·리비전 그대로). 자동 수정은 없다. ab0005→ab0007 같은 다단계 upgrade 도 한 트랜잭션이다.

| 경우 | 결과 |
|---|---|
| 정상 빈 스캔 (0/0, 실제 0) | 통과 |
| 정상 스캔 | 통과 |
| 기록 0건 / 실제 1건 | 거부 |
| 기록 1건 / 실제 0건 | 거부 |
| Strict 건수만 다름 | 거부 |
| 동결 해시 불일치 | 거부 |
| row_sha256 불일치 | 거부 |
| 결정 스캔이 불완전 수집 | 거부 |
| 불완전 수집 + 건수 불일치 | 거부 (불완전 표시) |

### 429 집계 원자성

- `_record_rate_limit` 은 한 쓰기 트랜잭션(BEGIN IMMEDIATE)에서 다음을 한다: 이전 상태 조회 → 증가/초기화 → 경고 임계값 판단 → 이벤트 → runtime 갱신.
- 결과:
  - 초기 1에서 두 연결이 동시에 RATE_LIMITED 를 기록하면 3이 되고, 경고는 1건이다.
  - 정상과 제한이 경쟁하면 두 직렬 순서 중 하나의 결과만 나온다.
  - 트랜잭션 도중 실패하면 증가·경고 모두 롤백한다.
- 같은 job 재기록 구분: job 마다 `job_id`(uuid)를 붙인다. 최근 20개(`recent_jobs`)에 있으면 다시 세지 않는다. 다른 job 은 각각 센다. 기존 정책(정상 수집 확인 시에만 초기화, 그 밖은 유지)은 그대로다.

### P7-OPS 검증 데이터

- 이전 상태:
  - `row_sha256` 전부 `"backtest"`, `row_json` = `{}`
  - 912개 스캔이 스냅샷이 있는데도 건수 0
- 바꾼 것 (`ops_parity.write_scan_capture`):
  - 재구성 후보마다 정규화 JSON(`snapshot_row_json`, 키 정렬·NaN 불허)과 그 SHA-256 을 만든다
  - 제품과 같은 수집 → 건수 → 봉인(`seal_capture`) → 동결(`freeze_decision`) 경로를 쓴다 (검증 우회 옵션 없음)
  - `run_shadow(replay=True)` 는 처리 후 운영 재계산 경로(`inputs.replay`)로 전체를 다시 계산한다. 동결 입력 검증과 CURRENT 전환 전 최종 검증을 거치고, 결과가 같은지 확인한다
- 한계: 이 해시는 **재구성 후보 내용의 무결성 근거**일 뿐이다. 실제 과거 Scanner 원본 행을 입증하지 않는다 (원본 미보존).
- 대조 결과 (로컬 재구성 캐시, 기존과 같은 비교):
  - 23개 계좌 전부 일치, 1,480 거래일, 진입 3,545건 (SHADOW·P7-OPS 동일)
  - 최대 상대 차이: 평가금액 3.1e-16, 현금 6.0e-16. 최대 절대 차이: 진입 Heat 4.2e-17, 현재 Heat 6.7e-16
  - 판정은 NOT_RECOMMENDED 유지, B01 은 NEED_MORE_DATA
  - 검증 저널 (동결 경로·replay 후 모두 같음):
    - 스냅샷 2,805건 전부 `sha256(row_json) = row_sha256`
    - 수집 1,480건 전부 건수 일치. 신호가 있는 912건 포함, 나머지 568건은 정상 빈 스캔
    - FROZEN 1,480일 모두 공통 검증 문제 0
  - `replay_identical` = true (처음 계산과 replay 결과의 원장·주문·일별 기록이 같음. 실행 이벤트 목록만 비교 제외)
  - 소요 시간 (로컬 macOS): SHADOW 재생 138.2초 (a183b99 기준 141.8초). replay 포함 296.0초 (재계산 약 158초 추가)

### 남은 위험·한계

- 관리자 DDL(트리거 삭제)·파일 직접 편집은 DB 계약으로 막을 수 없다. 엔진 공통 검증이 처리 전에 잡지만, 오염 자체를 되돌리지는 않는다 (운영자 검토 필요).
- 공통 검증은 FROZEN 거래일마다 적격 판정을 다시 계산한다. 거래일 수가 늘면 job 시간이 늘 수 있다. 실제 VM 에서 관측한다 (운영 검증 체크리스트).
- 기존 운영 저널(있다면)은 ab0007 사전 검사가 불완전 수집을 표시한다. 거부되면 원본 그대로 멈춘다. 자동 복구 경로는 없다.
- P7-OPS 해시는 재구성 후보의 무결성 근거일 뿐, 과거 Scanner 원본을 입증하지 않는다.

### 수정 전 실패·수정 후 통과 (`tests/test_auto_buy_shadow_review8_repro.py`)

a183b99 와 현재 코드에 모두 있는 API 만 쓴다. a183b99 worktree 에서 실행하면 11 실패 / 1 통과다. 실패는 모두 동작 단언 실패이며, 모듈·API 부재는 없다. 현재 코드에서는 12 통과다.

| 재현 | a183b99 | 현재 |
|---|---|---|
| FROZEN 결정 스캔 사후 INSERT (INSERT·OR IGNORE × 제품·기본 연결) | 4 실패 (DID NOT RAISE) | 통과 |
| 빈 결정 스캔 사후 INSERT (제품·기본) | 2 실패 | 통과 |
| 오염 저널의 신규 거래일 처리 | 실패 (오염 신호로 가상 주문) | 통과 (주문 없음·중단·ERROR) |
| 오염 저널 replay | 실패 (CURRENT 교체) | 통과 (CURRENT 유지) |
| 동결 직전·직후 동시 INSERT | 실패 (삽입됨) | 통과 (대기 후 거부) |
| 별도 ID 재스캔 보존 (기존 정책) | 통과 | 통과 |
| Migration 기록 0 / 실제 1 | 실패 (적용됨) | 통과 (거부·원본 유지) |
| 두 연결 동시 429 | 실패 (2) | 통과 (3, 경고 1) |

## Codex 재리뷰 반영 (PR #14, 2026-11-27, head 36e5d2f)

재리뷰 결과: 전체 테스트, P7-OPS 대조, 이전 결함 수정은 확인됐다. P1 3건·P2 2건이 재현돼 FAIL 을 수용했다.
이번에는 개별 SQL 연산·컬럼에 보호 조건을 하나씩 붙이지 않았다. 대신 **테이블별 데이터 불변성 계약**(`journal_migrations/ddl_v6.py` 의 `CONTRACT`)을 정의하고, 트리거를 그 계약에서 만들고, 실제 스키마와 전수 대조한다. 전략·위험 계산·매매 엔진·UI 는 바꾸지 않았다.

### 36e5d2f 에서 막히지 않던 변경 (조사 결과)

리뷰 지적 외에도 같은 종류의 빈틈을 표 전체에서 찾았다. 아래는 36e5d2f(ab0005) 저널에서 활성(CURRENT) run 행을 대상으로 시도했을 때 성공한 변경이다.
- 기본 연결: recursive_triggers OFF
- 제품 연결: `connect_rw`, recursive_triggers ON

| 종류 | 표·컬럼 | 기본 연결 | 제품 연결 |
|---|---|---|---|
| 동결 입력 컬럼 수정 | `ab_day_inputs`: day·frozen_by·strategy_version·config_hash·logic_hash, 계좌 저장 후 account_reason·account_fetched_at | 성공 | 성공 |
| 숨은 rowid 교체 (`INSERT OR REPLACE … (rowid, 새 키)`, `UPDATE OR REPLACE SET rowid=`) | 정수 PK 별칭이 아닌 9개 표: `ab_bars`·`ab_day_inputs`·`ab_accounts`·`ab_eligibility`·`ab_account_runs`·`ab_decisions`·`ab_daily`·`ab_days`·`ab_fill_ledger` | 성공 (기존 행 삭제) | 차단 (recursive_triggers 로 삭제 거부가 동작) |
| 음수 rowid INSERT | 위 9개 표 | 성공 | 성공 |
| rowid 변경 (`SET rowid=`, 정수 PK 표 포함) | `ab_runs`·`ab_scan_captures`·`ab_orders`·`ab_positions`·`ab_reservations` 등. `BEFORE UPDATE OF <컬럼>` 트리거는 rowid 별칭으로 바꾸면 동작하지 않는다 | 성공 | 일부 (외래키로 차단된 표 제외) |
| 활성 run 의 불변 컬럼 수정 | `ab_runs` reason·strategy_version·config_hash·logic_hash·created_at, `ab_orders` limit_price·stop_price·qty·target_day·signal_key·ticker, `ab_positions` entry_day·stop_price·order_id, `ab_reservations` sector·created_day, `ab_decisions`·`ab_daily`·`ab_days`·`ab_fill_ledger` 의 모든 값 컬럼 | 성공 | 성공 |
| 상반된 적격 판단 재등록 (`INSERT OR IGNORE`) | `ab_eligibility` | 조용히 무시 | 조용히 무시 |

### 테이블별 불변성 계약 (ab0006)

분류는 다섯 가지다: 완전 불변 입력 · 추가 전용 감사 기록 · 최초 1회 입력 허용 · 활성 run 에서만 변경 · 런타임 상태 갱신 허용.

| 분류 | 표 | 행 식별 | PK·UNIQUE | 변경 허용 컬럼 | 상태 전환·최초 1회 | 신규 INSERT | 기존 행 변경 / 삭제 | 충돌 처리 |
|---|---|---|---|---|---|---|---|---|
| 완전 불변 입력 | `ab_bars` | 복합 PK + 숨은 rowid | (day, ticker, input_rev) | 없음 | - | 새 수집 결과는 새 input_rev 행으로만 추가 (기존 행 교체 없음) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 완전 불변 입력 | `ab_signal_snapshots` | 정수 PK id (= rowid) | (id) · (scan_log_id, signal_key) | 없음 | - | 제한 없음 (키 충돌만 거부) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 완전 불변 입력 | `ab_accounts` | TEXT PK id + 숨은 rowid | (id) | 없음 | - | 제한 없음 (키 충돌만 거부) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 추가 전용 감사 기록 | `ab_events` | 정수 PK id AUTOINCREMENT (= rowid) | (id) | 없음 | - | 자동 id 로 추가만 | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 추가 전용 감사 기록 | `ab_eligibility` | 복합 PK + 숨은 rowid | (snapshot_id, pool, strategy_version) | 없음 | - | 제한 없음 (키 충돌만 거부) | 변경 불가 / 거부 | 같은 키 + 같은 판단(eligible·reasons·decision_role·logic_hash)만 멱등 무시, 판단이 다르면 거부 (INSERT OR IGNORE 포함). rowid 충돌 거부 |
| 최초 1회 입력 허용 | `ab_day_inputs` | TEXT PK day + 숨은 rowid | (day) | account_status, account_reason, account_snapshot, account_fetched_at | 계좌 4필드: 모두 NULL → (OK + JSON 스냅샷 + 조회 시각) 또는 (UNAVAILABLE·LATE + 스냅샷 없음 + 조회 시각), 한 번에 함께, 이후 변경 불가 | 계좌 4필드 NULL. FROZEN 은 결정 스캔·신호 해시 필수, NO_DECISION_SCAN 은 둘 다 없음 | 계좌 4필드만 최초 1회 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 최초 1회 입력 허용 | `ab_scan_captures` | 정수 PK scan_log_id (= rowid) | (scan_log_id) | rows_captured, strict_rows | 수집 건수: (0, 0) → 불변 스냅샷의 실제 건수(전체·Strict 통과)와 같은 값으로 1회 | 제한 없음 (키 충돌만 거부) | 수집 건수만 최초 1회 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_runs` | 정수 PK run_id (= rowid) | (run_id) | status, through_day, completed_at, summary | BUILDING→CURRENT·FAILED, CURRENT→SUPERSEDED. FAILED·SUPERSEDED 는 변경 불가 (ab0004) | CURRENT 는 1개만 (ab0003) | BUILDING·CURRENT run 의 허용 목록 컬럼만 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_account_runs` | 복합 PK + 숨은 rowid | (run_id, account_id) | initial_cash, cash, realized_pnl, updated_at | - | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | OLD·NEW run 이 BUILDING·CURRENT 일 때 허용 목록 컬럼만 (ab0003·ab0004 fence). 삭제 거부 (ab0004) / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_decisions` | 복합 PK + 숨은 rowid | (run_id, account_id, snapshot_id) | 없음 | - | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_orders` | 정수 PK id (= rowid) | (id) · (run_id, account_id, signal_key) · (run_id, account_id, ticker) WHERE state IN ('PENDING','PARTIAL','FILL_UNKNOWN') | filled_qty, avg_fill_price, state, fill_kind, updated_at, details | PENDING→그 외 상태, PARTIAL→PENDING 외 상태. 종결 상태(FILLED·UNFILLED·FILL_UNKNOWN 등)는 변경 불가. filled_qty 감소 불가 | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | OLD·NEW run 이 BUILDING·CURRENT 일 때 허용 목록 컬럼만 (ab0003·ab0004 fence). 삭제 거부 (ab0004) / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_reservations` | 정수 PK order_id (= rowid) | (order_id) | reserved_cash, planned_risk, state, closed_day, updated_at | ACTIVE→ACTIVE·CONVERTED·RELEASED. CONVERTED·RELEASED 는 변경 불가 | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | OLD·NEW run 이 BUILDING·CURRENT 일 때 허용 목록 컬럼만 (ab0003·ab0004 fence). 삭제 거부 (ab0004) / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_positions` | 정수 PK id (= rowid) | (id) · (order_id) · (run_id, account_id, ticker) WHERE state = 'OPEN' | qty, entry_price, cost, risk0, last_close, last_close_day, price_status, unresolved_since, state, exit_day, exit_price, exit_reason, proceeds, pnl, r_multiple, updated_at, zero_volume_days, last_zero_volume_day | OPEN→CLOSED (CLOSED 는 변경 불가), price_status OK→UNRESOLVED (되돌림 불가) | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | OLD·NEW run 이 BUILDING·CURRENT 일 때 허용 목록 컬럼만 (ab0003·ab0004 fence). 삭제 거부 (ab0004) / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_stop_events` | 정수 PK id (= rowid) | (id) · (position_id) | 없음 | - | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_daily` | 복합 PK + 숨은 rowid | (run_id, account_id, day) | 없음 | - | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_days` | 복합 PK + 숨은 rowid | (run_id, day) | 없음 | - | run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 활성 run 에서만 변경 | `ab_fill_ledger` | 복합 PK + 숨은 rowid | (order_id, cum_filled_qty) | 없음 | - | 주문이 있고 그 run 이 BUILDING·CURRENT 일 때만 (ab0003·ab0004) | 변경 불가 / 거부 | 기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006) |
| 런타임 상태 갱신 허용 | `ab_runtime` | TEXT PK key + 숨은 rowid | (key) | value, updated_at | - | 제한 없음 | upsert 허용 (운영 상태 kv) / 허용 (제품 경로 없음) | ON CONFLICT(key) DO UPDATE (제품 upsert) |
| 런타임 상태 갱신 허용 | `ab_alembic_version` | TEXT PK version_num | (version_num) | version_num | - | Alembic 전용 | Alembic 전용 / Alembic 전용 | Alembic 전용 |
| 런타임 상태 갱신 허용 | `sqlite_sequence` | SQLite 내부 (AUTOINCREMENT 순번) | - | name, seq | - | SQLite 내부 | SQLite 내부 / SQLite 내부 | SQLite 내부 |

계약에서 만든 트리거 (보호 대상 17개 표 전부, 연결 설정과 무관):
- **숨은 rowid:** `BEFORE INSERT` 에서 같은 rowid 가 있으면 거부한다. `AFTER INSERT` 에서는 확정 rowid ≤ 0 이면 거부한다. `BEFORE UPDATE` 에서는 rowid 를 바꿀 수 없다 (`rowid`·`oid`·`_rowid_`·정수 PK 별칭 모두 같은 값이다).
  - 자동 생성 rowid 는 `BEFORE INSERT` 시점에 아직 정해지지 않아 `NEW.rowid` 가 -1 로 보인다 (SQLite 3.53.4 확인).
  - 그래서 0 이하 rowid 는 `AFTER INSERT` 에서 막는다. 이렇게 하면 자동 생성(-1)은 기존 행과 충돌할 수 없고, 명시적 음수 rowid 는 거부된다.
- **변경 허용 목록:** `mutable` 에 없는 컬럼과 PK·UNIQUE 키 컬럼은 최초 INSERT 이후 바꿀 수 없다. 변경 금지 목록이 아니라 허용 목록이므로, 컬럼을 추가하면 계약에 분류하기 전까지 테스트가 실패한다.
- **상태 전환:**
  - `ab_runs`: BUILDING→CURRENT·FAILED, CURRENT→SUPERSEDED
  - `ab_orders`: 종결 상태는 변경 불가, PENDING 으로 되돌림 불가, filled_qty 감소 불가
  - `ab_positions`: CLOSED 는 변경 불가, UNRESOLVED→OK 불가
  - `ab_reservations`: CONVERTED·RELEASED 는 변경 불가
- **최초 1회 입력:**
  - `ab_day_inputs` 계좌 4필드: 모두 NULL 일 때 한 번에 저장한다. 허용 조합은 (OK + JSON 스냅샷 + 조회 시각) 또는 (UNAVAILABLE·LATE + 스냅샷 없음 + 조회 시각)뿐이다.
  - `ab_day_inputs` INSERT: 계좌 필드는 NULL 이어야 한다. FROZEN 은 결정 스캔·신호 해시가 있어야 하고, NO_DECISION_SCAN 은 둘 다 없어야 한다.
  - `ab_scan_captures` 수집 건수: (0, 0) 에서 불변 스냅샷의 실제 건수로 1회만 바꿀 수 있다.
- **NULL 처리:** 허용 조건은 `COALESCE(…, 0)`·`IS` 로 작성했다. SQL 의 NULL 3값 논리 때문에 조건이 NULL 이 되어 검사를 건너뛰는 일이 없게 하기 위해서다. 전수 테스트가 처음 작성한 트리거에서 이 문제(상태 없이 조회 시각만 쓰는 갱신 통과)를 찾아 고쳤다.
- **앱 경로 (`store_account`):**
  - 허용되지 않은 상태, 또는 OK 인데 스냅샷이 없으면 `AccountInputInvalid` 로 쓰지 않는다.
  - 저장은 같은 트랜잭션에서 4필드가 비어 있을 때만 갱신하는 조건부 갱신이다. 갱신 후 다시 읽어 쓴 값과 동결 필드가 그대로인지 확인하고, 다르면 롤백한다.
- **WITHOUT ROWID 대안:** 비교했지만 쓰지 않았다. 9개 표를 새로 만들어 데이터를 복사하고, 그 표들을 참조하는 트리거 99개를 다시 만들어야 해서 기존 저널과 Migration 에 미치는 영향이 크다. 트리거 방식으로 같은 보호를 얻으므로 표 재구성은 하지 않았다.

### P1 — 거래소 세션 설정 일치

- runner 가 `ChartFetcher(settings=job settings)` 를 만들고, ChartFetcher 는 그날 세션(`calendar.session(day, settings)`)을 `parse_chart(…, session=…)` 에 넘긴다. 추가 휴장일·조기폐장일 설정을 무시하고 기본 캘린더를 따로 조회하지 않는다.
- 신규 수집·결측 복구·replay 는 모두 같은 job 공급자를 쓰므로 같은 세션이 적용된다.
- 실패 근거에 `session_close_et`·`bar_time_et` 를 남긴다.
- 설정상 13:00 ET 폐장일에 15:00 ET 타임스탬프와 `volume:0` 인 HTTP 200 응답이 오면 `BAR_TIME_OUTSIDE_SESSION` 결측이다.
  - `FILL_UNKNOWN`·`DATA_INCOMPLETE` 로 남는다 (`ZERO_VOLUME_EXPIRED`·COMPLETE 없음).
  - 결측 복구에서 같은 응답이 다시 와도 거부된다.
  - 정상 일봉을 받으면 replay 로 복구된다.
- 정상 일봉과 실제 조기폐장일(기본·사용자 추가) 응답은 계속 받아들인다. 확인한 경계:
  - 장 시작 60초 전, 장 마감 시각, 1초 초과
  - DST 전환 전후
  - 기본·사용자 추가 휴장일
- 추가 휴장일·조기폐장일은 설정 해시에 들어간다. 처리 후 설정을 바꾸면 `VersionMismatch` 로 멈추고, 동결 입력·원장은 바뀌지 않는다 (테스트로 확인).

### P2 — ab_eligibility 충돌 정책

- 같은 키에 **같은 판단**(eligible·reasons·decision_role·logic_hash)을 다시 넣는 경우만 멱등 무시한다 (기존 행 유지, rowcount 0). 재등록 시 달라질 수 있는 기록 시각 `created_at` 은 판단 내용과 구분해 비교하지 않는다.
- 판단이 다르면 `INSERT OR IGNORE`·`REPLACE`·일반 INSERT 모두 `eligibility conflict` 로 거부한다.
- 앱(`record_eligibility`)이 충돌을 받으면 다음 순서로 처리한다. 경합 상황은 테스트로 재현했다.
  1. 이번 기록 트랜잭션 전체를 롤백한다.
  2. ERROR 이벤트 `eligibility_conflict` 를 남긴다 (스냅샷·풀·버전·logic_hash).
  3. `EligibilityConflict` 를 던져 job 처리를 멈춘다.

### P2 — Yahoo 호출 제한 카운터

- **증가:** job 이 RATE_LIMITED 로 멈추면 늘린다. 같은 job 에 정상 응답이 있어도 제한이 우선이다.
- **초기화:** **정상 수집이 실제로 확인된 job** 에서만 0 으로 되돌린다.
  - 정상 수집 = 그 job 에서 받은 실제 신규 응답 중 일봉 응답 계약을 통과한 것이 1건 이상 (`fetch.contract_ok`). HTTP 200 만으로는 인정하지 않는다.
  - 거래량만 미확인인 응답은 계약(주기·종목·시간대·세션·OHLC)을 통과했으므로 정상 수집으로 인정한다. 거래량 자체는 기존 미확인 정책을 따른다.
- **보존:** 아래 경우에는 연속 횟수와 warned 를 그대로 둔다.
  - 요청 0건
  - Scanner 실행으로 보류
  - 캐시만 사용
  - 요청했지만 정상 응답 없이 실패 (계약 위반·HTTP 오류·연속 실패 중단·예산 소진)
  - 판단 불가
- `YFRateLimitError` 즉시 중단 정책은 그대로다.

### 수정 전 실패·수정 후 통과

`tests/test_auto_buy_shadow_review7_repro.py` 는 36e5d2f 와 현재 코드에 공통인 API 만 쓴다. pytest 가 19건을 수집해 실행했다.
- **36e5d2f:** 18건이 동작 단언으로 실패했고 1건이 통과했다.
  - 실패 내역: 동결·계좌 필드 수정 12건, 숨은 rowid 교체 (기본 연결) 1건, 사용자 조기폐장 E2E 1건, 상반 판정 2건, 카운터 초기화 2건
  - 통과 1건: 숨은 rowid 교체 (제품 연결). 36e5d2f 의 제품 연결은 recursive_triggers=ON 이라 이미 막혔다.
- **수정 후:** 19건 모두 통과한다.

새 API 기준 테스트를 36e5d2f 에서 돌린 결과 (모듈·API 부재는 결함 재현으로 세지 않는다):
- `review7_contract`(1,195건)·`review7_migration`(9건): `ddl_v6` 모듈이 없어 수집 단계에서 중단된다.
- `review7_session`(32건): 22건은 API 부재(`parse_chart(settings=…)`·`ChartFetcher(settings=…)`·`contract_ok`), 8건은 동작 실패(카운터 보존 6건, runner 의 설정 전달 1건, Scanner 보류 job 의 카운터 1건), 2건 통과.

### Migration `ab0006`

격리된 임시 SQLite 에서 확인했다 (`tests/test_auto_buy_shadow_review7_migration.py`). 운영 Migration 은 실행하지 않았다.
- 신규 생성
- ab0001→ab0006, ab0005→ab0006: 데이터·감사 이력을 보존하고, 기본·제품 연결 모두에서 숨은 rowid·동결 필드·계좌 재수정을 차단한다
- **사전 데이터 검사:** 계약과 맞지 않는 기존 데이터(rowid ≤ 0, 계좌·결정 조합 오류, 수집 건수 불일치)가 있으면 중단하고 원본은 바꾸지 않는다
- 실패 시 ab0005 유지·원본 무변경
- 재실행 `ALREADY_CURRENT`
- `--check` 읽기 전용
- OFF 에서 파일·DDL 없음
- 0600·부속 파일·하드링크·S01 저널 거부는 기존 Migration 테스트가 ab0006 head 에서 통과한다

### P7-OPS 대조와 쓰기 비용

- P7-OPS 대조 도구(`backtest/entry_strategy/src/ops_parity.py`)는 거래일 입력을 직접 넣는다. 그래서 운영 확정과 같은 신호 집합 해시(`signal_set_hash`)를 함께 넣도록 맞췄다 (계약: FROZEN 은 해시 필수). 계산 결과에는 영향이 없다.
- 결과: 23계좌·1,480거래일·진입 3,545건 일치, 판정 유지.
- 트리거 추가에 따른 쓰기 비용은 같은 데이터의 SHADOW 재생 시간으로 비교했다: 36e5d2f 141.1초, ab0006 141.8초 (차이 약 0.5%, 로컬 macOS). 실제 VM 의 잠금 대기 시간은 운영 검증 체크리스트에서 따로 본다.

## Codex 재리뷰 반영 (PR #14, 2026-11-27, head c27d9d0)

재리뷰 결과: 기존 결함 6건, 전체 테스트, P7-OPS 대조, 동시성·S5 검증은 통과했다. 신규 P1 2건과 운영 필수 P2 1건을 반영했고, 범위는 이 세 결함과 경계 조건으로 제한했다 (새 전략·기능·구조 변경 없음).

### P1 — 일봉 응답 계약 (`market_data.parse_chart`)

아래 항목을 하나라도 확인할 수 없으면 정상 응답으로 가정하지 않는다 (`BarUnavailable` → 마감 후 `MISSING` → `FILL_UNKNOWN`·`DATA_INCOMPLETE` → 정상 일봉 확보 후 replay 복구). 그런 응답의 `volume:0` 으로 `ZERO_VOLUME_EXPIRED` 를 만들지 않는다.

| 검사 | 실패 코드 |
|---|---|
| HTTP 200 (`fetch_chart`, 이전 라운드) | `HTTP_{코드}`·`HTTP_STATUS_UNKNOWN` |
| `chart.error` 없음, `result`·`meta` 가 객체 | `CHART_ERROR`·`NO_RESULT`·`META_MISSING` |
| `meta.dataGranularity` 가 문자열 `"1d"` (1m·5m·1h·1wk 등 거부) | `GRANULARITY_NOT_DAILY` |
| 주기 누락·null / 문자열이 아님 | `GRANULARITY_MISSING` / `GRANULARITY_INVALID_TYPE` |
| `meta.symbol` 있음 / 요청 종목과 같음 | `SYMBOL_MISSING` / `SYMBOL_MISMATCH` |
| `meta.exchangeTimezoneName` 있음 / `America/New_York` (대체 시간대로 가정하지 않음) | `TZ_MISSING` / `TZ_MISMATCH` |
| 타임스탬프가 정수·증가 순서·요청 거래일 구간 안 | `TIMESTAMP_INVALID`·`TIMESTAMP_NOT_INCREASING`·`TIMESTAMP_OUT_OF_RANGE` |
| 요청일이 정규장 거래일 / 그날 봉이 정확히 1개 / 봉 시각이 정규장 시간 안(조기폐장 반영) | `DAY_NOT_TRADING_SESSION` / `DAY_NOT_IN_RESPONSE`·`DUPLICATE_DAY` / `BAR_TIME_OUTSIDE_SESSION` |
| OHLC 배열 길이가 타임스탬프와 같음, 값 양수, 고가·저가가 시가·종가를 감쌈 | `{OPEN…}_ARRAY_PARTIAL`·`OHLC_NULL_OR_INVALID`·`OHLC_INCONSISTENT` |
| 거래량 (기존 정책) | 누락·부분·null·음수 = 미확인, `0` = 이 원본 경로에서만 확정 0 |

실패 근거(`data_granularity`·`exchange_tz`·`bar_time_et`·issues·HTTP 상태·응답 SHA-256)를 결측 행 `source_evidence` 에 남긴다. 마감 전 잠정 데이터 대기 정책은 그대로다.

### P1 — 충돌 교체 차단 (`ab0005`, `journal_migrations/ddl_v5.py`)

- **원인:** SQLite 의 `INSERT OR REPLACE`·`REPLACE INTO`·`UPDATE OR REPLACE` 는 키가 겹치는 기존 행을 내부적으로 지운다. `PRAGMA recursive_triggers` 가 꺼져 있으면(SQLite 기본값) 이 내부 삭제는 DELETE 트리거를 부르지 않아 ab0004 의 삭제 거부를 우회했다.
- **스키마 보호 (연결 설정과 무관):** 보호 표마다 PK 와 모든 UNIQUE(부분 UNIQUE 포함) 제약의 **기존 키 충돌을 명시적으로 거부**한다.
  - `BEFORE INSERT`: 같은 키의 행이 있으면 거부 → `INSERT OR REPLACE`·`REPLACE INTO`·`INSERT … ON CONFLICT DO UPDATE`·PK 외 UNIQUE 충돌 교체 차단
  - `BEFORE UPDATE`: 바꾼 키가 다른 행과 겹치면 거부 → `UPDATE OR REPLACE` 로 다른 행을 지우는 우회 차단
  - `ab_eligibility` 만 `RAISE(IGNORE)`: 제품이 `INSERT OR IGNORE` 로 멱등 기록한다. 기존 행은 유지되고 오류 없이 0행이다.
  - `ab_runs` 의 부분 UNIQUE(`status='CURRENT'`)는 ab0003 의 단일 CURRENT 트리거가 이미 INSERT·UPDATE 모두 거부한다.
- **남은 감사 자료 불변:**
  - `ab_accounts`(가상 계좌 설정·해시): UPDATE·DELETE 거부
  - `ab_eligibility`(판정 기록): UPDATE·DELETE 거부
  - `ab_scan_captures`(스캔 수집 이력): DELETE 거부. 수집 직후 채우는 `rows_captured`·`strict_rows` 외 열은 변경 거부
- **연결 설정 (이중 방어):** 제품 쓰기 연결(`store.connect_rw`)과 Migration 연결(`journal_migrations/env.py`)에서 `foreign_keys=ON`, `recursive_triggers=ON` 을 설정하고 다시 읽어 확인한다. 적용되지 않으면 쓰지 않는다.
- **외래키:** 모두 NO ACTION 이다 (CASCADE·SET NULL 없음, 테스트로 확인). 연쇄 변경 경로가 없고, 부모 행 삭제는 각 표의 삭제 거부가 막는다.
- **보호 대상이 아닌 표:** `ab_runtime`(운영 상태 kv, upsert 사용), `ab_alembic_version`(Alembic 관리), `sqlite_sequence`.
- **보호 범위의 한계 (구분):**
  - 막는 것: 제품 연결과 **설정 없는 별도 sqlite3 연결** 모두에서, 일반 DML(INSERT·UPDATE·DELETE 와 충돌 해소 변형)로 보호 행을 바꾸는 경로. 테스트로 확인했다.
  - 막지 못하는 것: DB 관리자나 파일 소유자가 하는 아래 작업. 이는 파일 권한(0600, 서비스 사용자 소유)·백업·운영 접근 통제의 영역이다.
    - 트리거 DROP 같은 임의 DDL
    - `PRAGMA writable_schema`
    - 파일 직접 편집·교체·삭제, 백업에서 덮어쓰기

**SQL 연산별 검증** (`tests/test_auto_buy_shadow_review6_ledger.py`, 제품 연결·별도 연결 각각):

| 연산 | c27d9d0 (ab0004)에서 막히지 않던 표 | 수정 후 |
|---|---|---|
| `INSERT OR REPLACE` / `REPLACE INTO` | `ab_events`·`ab_runs`(종료 run)·`ab_signal_snapshots`·`ab_bars`·`ab_day_inputs`·`ab_scan_captures`·`ab_accounts`·`ab_eligibility` | 모두 차단 (`ab_eligibility` 는 무시, 행 유지) |
| `INSERT … ON CONFLICT DO UPDATE` | `ab_day_inputs`·`ab_scan_captures`·`ab_accounts`·`ab_eligibility` | 차단 |
| `UPDATE OR REPLACE` (다른 행 키로 변경) | `ab_orders`·`ab_positions`·`ab_reservations`·`ab_day_inputs`·`ab_scan_captures`·`ab_accounts`·`ab_eligibility` | 차단 |
| PK 외 UNIQUE 충돌 교체 (새 PK + 같은 UNIQUE) | 활성 run 의 `ab_orders`·`ab_positions`, `ab_signal_snapshots` | 차단 |
| `UPDATE` | `ab_accounts`·`ab_eligibility` | 차단 |
| `DELETE` | `ab_accounts`·`ab_eligibility`·`ab_scan_captures` | 차단 |
| OLD/NEW run 이동, 종료 run 변경, `ab_events` 변경 | (ab0004 에서 차단) | 유지 |
| 외래키 연쇄 | CASCADE 없음 | 유지 |

시도 전후로 보호 대상 표 전체(행 단위) 덤프가 같다. 정상 경로는 그대로 동작한다: 새 감사 이벤트 추가, 활성 run 행 갱신, `INSERT OR IGNORE` 멱등 기록, 실패 후 재시도 replay 와 CURRENT 전환, 다음 job.

### P2 — `YFRateLimitError`

- **식별:** yfinance 는 HTTP 429 를 감지하면 응답 대신 `yfinance.exceptions.YFRateLimitError` 를 던진다. 이 예외는 `YF_RATE_LIMITED` 로 분류한다 (일반 `FETCH_ERROR` 가 아님).
- **즉시 중단:** 재시도하지 않고 job 상태를 `RATE_LIMITED` 로 둔다. 같은 job 에서 다른 종목·다른 거래일로 더 요청하지 않는다.
- **미수집 종목:** 결측(복구 대상)이며 근거(예외 이름·halted)가 남는다. 해당 거래일은 `DATA_INCOMPLETE` 이고 COMPLETE 로 처리하지 않는다.
- **후속 job 보류 정책:** 다음 job 은 다시 시도하되 첫 제한 응답에서 바로 멈춘다. 그래서 제한이 계속되는 동안 job 당 Yahoo 시도는 1회 남짓이다. 연속 횟수는 runtime `yahoo_rate_limit` 에 남는다. 3회 연속이면 WARN `yahoo_rate_limited_repeated` 를 한 번 남기고, 정상 수집되면 0 으로 돌아간다.
- **기존 정책 유지:** HTTP 429 응답·Retry-After·timeout·503.
- **요청 예산의 뜻:** `JOB_REQUEST_BUDGET`(300)과 운영 지표 `fetch.requests` 는 **애플리케이션 조회 시도 수**다. yfinance 세션이 쿠키·crumb 갱신 등으로 내부에서 보내는 추가 요청은 세지 못하므로 실제 네트워크 요청 수의 상한이 아니다.

### 수정 전 실패·수정 후 통과

- `tests/test_auto_buy_shadow_review6_repro.py` (6건, c27d9d0 와 현재 코드 공통 API): c27d9d0 에서 6건 모두 실패하고 수정 후 통과한다.
  - 1m 응답이 일봉으로 파싱됨
  - 1m `volume:0` 이 `UNFILLED`(ZERO_VOLUME_EXPIRED)로 처리됨
  - 감사 이벤트 `INSERT OR REPLACE` 교체 (제품 연결·별도 연결)
  - `YFRateLimitError` 가 `FETCH_ERROR` 로 분류되고 다른 종목 요청을 계속함
- 새 API 기준 테스트:
  - `review6_contract` 41건: c27d9d0 에서 36건 실패. 통과한 5건은 기존에도 지켜지던 경계(종목 불일치, 배열 부분, 원본 null/0 구분, 같은 날 여러 봉, 429·503 정책)의 회귀 확인이다.
  - `review6_ledger` 8건·`review6_migration` 7건: c27d9d0 에는 `ddl_v5` 모듈이 없어 수집 단계에서 중단된다 (새 API 부재). 결함 재현은 위 repro 와 연산별 표로 확인했다.

### Migration `ab0005`

격리된 임시 SQLite 에서 확인했다 (`tests/test_auto_buy_shadow_review6_migration.py`). 운영 Migration 은 실행하지 않았다.
- 신규 생성
- ab0001→ab0005, ab0004→ab0005 (원장·감사 이력 보존, 별도 연결에서 교체 차단)
- 실패 시 원본 무변경 (ab0004 유지)
- 재실행 `ALREADY_CURRENT`
- `--check` 읽기 전용
- Migration 연결 `recursive_triggers=1`
- 심볼릭 링크 거부
- OFF 파일·DDL 없음
- 0600·부속 파일·하드링크·S01 저널 보호는 기존 테스트(`review4_migration`·`review5_s5`)가 ab0005 head 에서 통과

### 운영 검증 체크리스트 (실제 VM 에서만 판단 가능, 활성화 전·첫 주)

- [ ] 실제 VM 의 Linux 배포판·아키텍처·서비스 사용자 권한 (저널 0600·소유자, `/proc` 식별자 읽기)
- [ ] 실제 Yahoo 네트워크 요청량 (`fetch.requests` 는 애플리케이션 시도 수, yfinance 내부 요청 제외)
- [ ] Scanner 실행과 429 경합 (Scanner 실행 중 보류 동작, `yahoo_rate_limit` 연속 횟수)
- [ ] 과거 결측 복구로 당일 요청 예산이 부족해지는지 (`BUDGET_EXHAUSTED` 빈도)
- [ ] 첫 주 CPU·메모리·디스크 사용량, SQLite 잠금 대기 시간 (트리거 추가에 따른 쓰기 시간 포함)
- [ ] 운영 저널 ab0007 사전 검사 결과 (`--check`, 불완전 수집 표시 건수), `signal_set_integrity` 발생 0
- [ ] 공통 무결성 검증(적격 판정 재계산 포함)의 job 당 소요 시간 — 동결 거래일 수에 비례해 늘어나는지

성능 최적화는 실제 VM 관측 뒤 별도로 판단한다 (이번 범위 밖).

## Codex 재리뷰 반영 (PR #14, 2026-11-27, head 9a315db)

재리뷰 결과: FAIL (P1 1건, P2 4건, 추가 운영 안전성). 이를 수용하며 SHADOW 활성화는 계속 보류다.
이번에는 지적 하나하나를 고치는 데 그치지 않고, 아래 **공통 안전 원칙**을 정하고 장애 주입 테스트로 확인했다. 새 전략·기능은 없다.
스키마는 새 리비전 `ab0004` 로만 늘렸고, `ab0001`~`ab0003` 은 바꾸지 않았다.

### 공통 안전 원칙 (테스트로 확인)

| 원칙 | 구현 | 확인 테스트 |
|---|---|---|
| 검증되지 않은 시장 데이터로 가상 체결하지 않는다 | 일봉으로 쓰는 응답은 HTTP 200 + 내용 검증 통과뿐. 그 밖의 응답은 `BarUnavailable`(수집 실패)이다 | `review5_http` 오류 응답 표 |
| 확인할 수 없는 상태를 정상으로 확정하지 않는다 | 수집 실패·예산 소진·연속 장애 중단은 마감 후 `MISSING`(근거 포함) → `FILL_UNKNOWN`·`DATA_INCOMPLETE` → 복구 대상. 정상 미체결 만료가 아니다 | `review5_http` 수집 실패 → 주문 → 일별 상태 → 복구 |
| 필수 거래일·입력이 누락되면 정상 평가하지 않는다 | S5 연속성은 저장된 날짜가 아니라 거래소 기대 거래일과 대조한다. 기록 전체 누락도 위반이다 | `review5_s5` 연속성 |
| 실패한 재계산은 기존 CURRENT 를 훼손하지 않는다 | 실패 run 은 FAILED 이고 CURRENT 와 재계산 요청은 그대로 남는다. 재시도는 1회만 된다 | `review5_replay` 실패 재시도·프로세스 종료 |
| 같은 요청을 중복 처리하지 않는다 | 재계산 요청 세대 번호, 기대 부모 run, 필요성 확인, 실행권 확보를 한 트랜잭션에서 처리한다 | `review5_replay` 멱등성 (동시·순차·처리 중 새 요청) |
| 종료 run 과 감사 이력은 변경하지 않는다 | `ab0004` 트리거 | `review5_replay` 불변성 표 |
| 오류가 복구된 뒤에도 관찰 기간 이력은 남는다 | 영속 이벤트로 센다: `replay_stalled`·`heartbeat_gap`·`uncaptured_scan` | `review5_replay` 정체 이력, `review5_s5` 이력 |
| SHADOW 는 실주문 경로를 부르지 않는다 | SHADOW 코드에서 주문 모듈을 import 하지 않는다. 전체 경로 테스트에서 `KiwoomOrderClient` 주문 함수를 호출 시 실패하도록 바꿔 확인한다 | `review5_http` 전체 경로 장애 주입 |

**테스트 구분:**
- **결함 재현 테스트** `tests/test_auto_buy_shadow_review5_repro.py` (6건): 9a315db 와 현재 코드 모두에 있는 API 만 쓴다. 9a315db 에서는 6건 모두 실패하고, 수정 후에는 통과한다.
- **수정 후 통과 테스트** `review5_http`·`review5_replay`·`review5_s5`: 새 API 기준이다. 9a315db 에서는 80건 중 78건이 실패한다. 통과한 2건(OFF 모드 무생성, S01 저널 거부)은 원래부터 지켜지던 보호의 회귀 확인이다.

### P1 — HTTP 오류 응답 (`market_data.fetch_chart`·`ChartFetcher`)

- **HTTP 200 만 일봉 후보로 해석한다.**
  - 상태 코드를 알 수 없으면 `HTTP_STATUS_UNKNOWN` 이며, 성공으로 보지 않는다.
  - 200 이 아니면(`HTTP_{코드}`) 본문을 파싱하지 않는다. 그래서 503 오류 본문의 `volume: 0` 이 확정 0 이 되지 않는다.
- **200 응답은 내용을 따로 검증한다.** 실패하면 그 사유가 실패 코드가 되며, 재시도하지 않는다.
  - 형식: `EMPTY_BODY`·`INVALID_JSON`·`CHART_ERROR`·`NO_RESULT`
  - 종목·날짜 불일치: `SYMBOL_MISMATCH`·`DAY_NOT_IN_RESPONSE`
  - 가격·거래량: OHLC 이상, 거래량 null·필드 없음 → 기존 거래량 정책(UNKNOWN)
- **실패 근거를 저장한다:** 오류 유형, HTTP 상태, 응답 sha256, 시도 횟수, Retry-After. 결측 행 `source_evidence` 에 남는다.
- **실패 처리:**
  - 데이터 마감 전에는 기다리고 다음 job 에서 다시 받는다.
  - 마감 후에는 `MISSING`(volume UNKNOWN)으로 저장한다. 해당 주문은 `FILL_UNKNOWN`, 그날은 `DATA_INCOMPLETE` 이다.
  - 정상 응답을 받으면 기존 결측 복구·재계산이 동작한다.

### 호출량 안전장치 (job 단위 `ChartFetcher`)

| 항목 | 값 |
|---|---|
| 같은 종목·거래일 | job 안에서 한 번만 요청한다 (실패 결과도 재사용) |
| 요청 예산 | job 당 300 회 (`JOB_REQUEST_BUDGET`, 애플리케이션 조회 시도 수 — 실제 네트워크 요청 상한 아님). 넘으면 `BUDGET_EXHAUSTED` 결측이며, 정상 확정하지 않는다 |
| timeout | 요청당 10초 |
| 재시도 | 최대 2회 시도. 재시도 대상은 429·5xx·timeout·연결 오류뿐 |
| backoff | Retry-After(초·HTTP 날짜)를 따른다. 30초를 넘으면 기다리지 않고 job 수집을 멈춘다(`RATE_LIMITED`). 없으면 지수 backoff(2초부터, 최대 2회 시도이므로 실제로는 1회 2초). job 당 대기 합계는 최대 60초 |
| 429·503 이 재시도 후에도 계속될 때 | 그 job 의 남은 요청을 보내지 않는다 (`RATE_LIMITED`) |
| 연속 장애 | 5회 연속 실패하면 차단한다 (`CIRCUIT_OPEN`) |
| Scanner 동시 실행 | 운영 스캔이 실행 중이면 그 job 에서는 Yahoo 요청(결측 복구·재계산·일봉 조회)을 미룬다. 결측으로 확정하지 않는다 |

job 결과의 `fetch` 에 요청 수, 대기 시간, 중단 사유를 남긴다. 실주문·키움 API 는 호출하지 않는다.

### P2 — S5 기대 거래일 연속성 (`report.continuity`)

- **검증 시작일을 고정한다.** runner 가 처음 정해 runtime `s5_validation_start:{버전}` 에 저장하고, 이후 바꾸지 않는다.
- **기대 거래일과 대조한다.** 시작일부터 마지막 마감 거래일까지 거래소 캘린더로 거래일을 만든다. 주말·휴장은 빼고, 조기폐장은 데이터 마감 시각에 반영한다.
- **처리 기록이 없는 거래일은 `DAY_NOT_PROCESSED` 위반이며, 연속 일수를 초기화한다.**
- **처리 기한 전에는 위반으로 보지 않는다.** 아직 처리 기한(데이터 마감 + 30분)이 지나지 않은 오늘은 `PENDING` 이다.
- **그날 처리 구간의 경고·오류는 그날 위반이다:** ERROR, 실행권 정체(`REPLAY_STALLED`), 미수집 스캔(`UNCAPTURED_SCAN`).
- **최초 5거래일과 그 뒤 연속 5거래일을 따로 평가한다:** `initial_window` PASS/FAIL/PENDING, `current_clean_streak`.
- 예: 11-16·17·19·20·23 이 COMPLETE 이고 11-18 기록이 없으면 PASS 가 아니다 (`initial_window=FAIL`, 연속 3일).

### P2 — S5 관찰 기간 이력 (현재 상태만 보지 않음)

| 항목 | 관찰 기간 누적 (영속 근거) | 현재 미해결 |
|---|---|---|
| 실행권 정체 | WARN `replay_stalled` 이벤트 수. 같은 장애는 한 번만 센다 (`replay_stall_active`). 회복되면 INFO `replay_stall_recovered` | `replay_state=STALLED` |
| 하트비트 공백 | runner 가 다음 실행 때 놓친 예정 실행을 WARN `heartbeat_gap` 으로 한 번 남긴다 | heartbeat `STALE` |
| 미수집 스캔 | `reconcile` 이 처음 확인한 스캔마다 WARN `uncaptured_scan` 을 남긴다 (`uncaptured_seen`) | 현재 대조 결과 |
| 수동 검토 대상 봉 | 기존 `manual_review_flagged` 와 현재 값 중 큰 값 | 현재 값 |

- **복구됨** = 관찰 누적 − 현재 미해결.
- STALLED → RUNNING 이 되어도 누적은 줄지 않는다.
- 통과 조건은 그대로다: 관찰 누적 0, 현재 미해결 0.

### P2 — 종료 run 원장 불변성 (`ab0004`, `journal_migrations/ddl_v4.py`)

- **run 단위 표 8개** (`ab_account_runs`·`ab_decisions`·`ab_orders`·`ab_reservations`·`ab_positions`·`ab_stop_events`·`ab_daily`·`ab_days`):
  - UPDATE 는 OLD 의 run 도 BUILDING·CURRENT 여야 한다. NEW 쪽은 ab0003 이 이미 검사한다.
  - `run_id` 는 변경할 수 없다. 종료 run 행을 활성 run 으로 옮기는 우회와 그 반대를 막는다.
  - DELETE 는 모두 거부한다.
- **`ab_fill_ledger`:**
  - 없는 주문을 가리키는 INSERT 를 거부한다. ab0003 의 `NOT IN` 검사에서 NULL 주문이 통과하던 틈을 막는다.
  - UPDATE 는 OLD·NEW 주문의 run 이 모두 활성일 때만 된다. `order_id` 는 변경할 수 없다.
  - DELETE 를 거부한다.
- **`ab_runs`:**
  - FAILED·SUPERSEDED run 은 어떤 열(감사 정보 포함)도 바꿀 수 없다.
  - `run_id` 변경과 DELETE 를 거부한다.
- **`ab_events`:** UPDATE·DELETE 를 거부한다.
- 없는 run 을 가리키는 쓰기는 ab0003 fencing 이 거부한다 (상태가 NULL).
- 정상 재계산(부모 → SUPERSEDED, 새 run → CURRENT)과 CURRENT 전환은 그대로 동작한다. 테스트로 확인했다.

### P2 — 재계산 멱등성

- 결측 복구는 재계산 요청을 남긴다: `replay_needed` = {사유, 봉, **세대 번호**, 요청 ID}.
- `replay(db, …, expected_parent, expected_generation)` 은 같은 트랜잭션 안에서 세 가지를 확인한 뒤 실행권을 잡는다.
  - 요청이 아직 필요한가
  - 이미 처리된 세대인가
  - 부모 run 이 바뀌었는가
- 해당하면 새 run 없이 건너뛴다: `NOT_NEEDED`·`ALREADY_APPLIED`·`PARENT_CHANGED`.
- 완료 시 `replay_done_generation` 을 기록한다. 처리 도중 더 새로운 요청이 들어왔으면 요청을 지우지 않는다 (INFO `replay_request_pending`). 다음 job 이 처리한다.
- 실패하거나 프로세스가 종료되면 요청과 CURRENT 는 남는다. 다음 job 이 1회 재시도한다.

### 수동 실행권 해제 (`inputs.release_stalled_lease`, 수동 조치 전용)

- **해제할 수 있는 경우:** 다른 호스트의 응답 없는 작업자(`REMOTE_STALE`)뿐이다. 이 프로세스가 사망을 확인할 수 없는 경우다.
- **거부하는 경우:**
  - 같은 호스트에서 살아 있는 작업자: 먼저 프로세스를 종료하면 자동 정리된다.
  - 정상 heartbeat 작업자
  - 이미 사망이 확인된 작업자: 자동 정리 대상이다.
- **필수 입력:** 예상 run ID, 실행권 토큰, 운영자, 사유, 종료 확인 근거(`host`·`pid`·`proc_start`·`confirmed_stopped_at`·`method`). host·pid·proc_start 는 실행권 기록과 일치해야 한다.
- **실행:** BUILDING → FAILED 갱신이 정확히 1행이어야 한다. 갱신, 실행권 해제, ERROR 감사 이벤트 `replay_released_manually` 를 한 트랜잭션에서 한다. 어느 하나라도 어긋나면 `ManualReleaseRefused` 로 롤백하고, 실행권은 유지된다.
- 해제 뒤 원래 작업자가 다시 쓰려 해도 fencing 트리거와 실행권 확인이 막는다.
- **운영 절차:**
  1. 원격 호스트에서 해당 pid·시작 식별자의 프로세스가 없는지 직접 확인한다.
  2. 근거를 기록한다.
  3. 별도 승인을 받는다.
  4. Python 셸에서 함수를 호출한다. 웹·CLI 진입점은 만들지 않았다.
- 종료를 확인할 수 없으면 해제하지 않고 재계산을 계속 대기한다.

### Migration 검증 순서·ab0004

- **SQLite 로 여는 어떤 동작(`schema_status` 포함)보다 먼저** 저널과 부속 파일(`-journal`·`-wal`·`-shm`)을 검증한다: 경로 유형, 심볼릭 링크, 하드링크, 소유자, 권한, 다른 저널 여부.
- 저널이 없는데 부속 파일만 있으면 중단한다.
- **확인한 경로** (테스트):
  - 신규 생성
  - ab0001 → ab0004, ab0003 → ab0004 (데이터·감사 이력 보존, 트리거 동작)
  - 실패 시 원본 무변경: 복사본에서 시험한다
  - 재실행 `ALREADY_CURRENT`
  - 0600 권한
  - `--check` 읽기 전용
  - S01·운영 주문 저널 거부
  - OFF 모드 파일·DDL 없음
- 운영 Migration 은 실행하지 않았다.

### 계좌 스냅샷 필드 (합성 fixture)

- `build_account_snapshot` 이 값을 구분해 저장한다.
  - `total_cash`: 총현금
  - `orderable_cash`(= 기존 `cash`): 주문가능현금
  - `holdings_total`: 보유 평가
  - `equity`: 총현금 + 원장 평가
- 신규 가상 매수의 현금 판단은 보수적으로 주문가능현금을 쓴다.
- 가상 포지션·예약금은 `risk.account_state` 가 가상 원장에서 따로 더한다.
- 테스트: 총현금 ≠ 주문가능현금인 합성 계좌가 `build_account_snapshot` → `store_account` → `account_state` 를 거친다. 실계좌 값은 없다.

### Linux 호환성

2026-11-27 격리 Docker 컨테이너에서 확인했다. 운영 VM 은 건드리지 않았다.
- **환경:** `python:3.11-slim`, Linux 7.0.14-linuxkit aarch64, Python 3.11.18. psutil 이 없어 `/proc` 경로를 쓴다.
- **식별자:**
  - boot_id 는 `/proc/sys/kernel/random/boot_id` 에서 읽힌다.
  - 프로세스 시작 식별자는 `/proc/<pid>/stat` 에서 자기·자식 프로세스 모두 읽힌다.
  - SIGKILL 된 자식의 시작 식별자는 None 이다 (사망 판정 근거).
- **테스트:**
  - 자동매수 SHADOW 전체 253건 통과
  - 실행권·재계산·fencing 테스트(`review3`·`review4_lease`·`review5_replay`, 68건)를 5회 반복해 모두 통과했다. 901초 이상 정상 작업자, pid 재사용(시작 식별자 불일치), 다른 호스트, SIGKILL 복구, 실행권 상실 후 쓰기 차단, 같은 요청 동시 처리가 포함된다.
  - S01 자동손절 회귀 156건 통과
- **한계:**
  - 운영 VM 과 같은 배포판·아키텍처(x86_64 여부)·systemd 환경이 아니다.
  - pid 재사용은 시작 식별자 불일치를 주입해 확인했다. 실제로 같은 pid 가 재할당되는 상황은 만들지 않았다.
  - VM 에서 같은 확인을 하는 것은 활성화 전 조건으로 남긴다 (배포 절차 2-1).

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
- **`ab0003`**: `ab_bars.volume_source`·`source_evidence` 열 추가(`ALTER TABLE … ADD COLUMN`)와 run fencing 트리거(위 '재계산 실행권 v2'). 시작 전에 대상이 정확히 `ab0002` 구조인지 확인한다. downgrade 는 지원하지 않는다. 기존 행의 출처는 NULL 이며, 그 행의 거래량 0 은 미확인으로 보고 원본 경로로 다시 확인한다.
- **`ab0004`**: 종료 run 원장·감사 이력 불변성 트리거만 추가한다 (위 'Codex 재리뷰 반영 (2026-11-27)'). 시작 전에 대상이 정확히 `ab0003` 구조인지 확인한다. 기존 행은 바꾸지 않는다. downgrade 는 지원하지 않는다.
- **`ab0005`**: 충돌 교체(REPLACE·UPSERT) 차단과 남은 감사 자료 불변 트리거만 추가한다 (위 'Codex 재리뷰 반영 (2026-11-27, head c27d9d0)'). 시작 전에 대상이 정확히 `ab0004` 구조인지 확인한다. 기존 행은 바꾸지 않는다. downgrade 는 지원하지 않는다.
- **`ab0006`**: 테이블별 데이터 불변성 계약 트리거를 추가하고, 정책이 바뀐 ab0005 트리거 1개(`ab_eligibility_no_replace_insert`)만 교체한다 (위 'Codex 재리뷰 반영 (2026-11-27, head 36e5d2f)'). 시작 전에 대상이 정확히 `ab0005` 구조이고 기존 데이터가 계약과 맞는지 확인한다. 기존 행은 바꾸지 않는다. downgrade 는 지원하지 않는다.
- **`ab0007`** (최종 목표 리비전): 봉인 표 `ab_capture_seals` 와 신호 집합·관계 단위 트리거를 추가하고, 기존 수집 기록마다 봉인 1행(`AB0007_BACKFILL`)을 기록한다 (위 'Codex 재리뷰 반영 (2026-11-28, head a183b99)'). 시작 전에 대상이 정확히 `ab0006` 구조이고 사전 검사(`ddl_v7.precheck`: 모든 수집 건수·소속·row_sha256·동결 해시)를 통과하는지 확인한다. 기존 행·동결 해시는 바꾸지 않는다. downgrade 는 지원하지 않는다. backfill 봉인은 이관 당시 저장 상태 기준이며, 과거 파생 필드의 정확성 증거가 아니다.
- **원본 정합성 사전 검사** (head d95beab 반영, 스키마 변경 없음): 어떤 리비전보다 먼저, 같은 트랜잭션에서 스냅샷 정규화 필드를 원본 `row_json` 과 대조한다 (`env.py` → `raw_contract.journal_issues`). 배포 스크립트도 기존 저널(이미 최신 포함)에 같은 검사를 읽기 전용으로 먼저 한다 (`RAW_CONTRACT_FAILED`, 종료 코드 2).
- 런타임은 `ab0007` 만 받아들인다. `ab0001`~`ab0006` 저널은 schema 미준비로 보고 처리하지 않는다 (Migration 실행은 별도 승인 — 이번 작업에서 실행하지 않음).
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
   - 결과의 `schema_version` 이 **`ab0007`**(최종 목표 리비전)인지 확인한다.
   - 기존 저널을 올리는 경우 `--check` 가 ab0007 사전 검사까지 통과하는지 먼저 본다. 거부되면 원본은 그대로이며, 자동 수정 없이 보고된 수집·거래일을 검토한다.
   - 생성 후 권한 확인: `stat -c '%a %U' <저널 경로>` → `600 <서비스 사용자>` (경로는 운영자가 직접 입력, 출력은 권한만).
   - 백업에서 복원한 저널이면 먼저 `--check` 로 권한을 확인한다. 0600 이 아니면 `CHECK_FAILED_PERMISSIONS`(exit=2)이다. 승인 후 적용 모드를 다시 실행하면 `ALREADY_CURRENT` 와 함께 0600 으로 맞춰진다.
2-1. **⛔ 활성화 전 조건 (모두 확인 후 별도 승인):**
   - 복원·이전한 저널(있다면)의 `--check` 가 권한 포함 통과 (exit=0, 0600)
   - 수집 경로 검증: 원본 응답 주입 회귀 테스트(`tests/test_auto_buy_shadow_review4.py`)가 배포 커밋에서 통과. 원본 `volume:null`·필드 없음은 `UNKNOWN`, `volume:0` 만 확정 0 인지 본다. 실제 Yahoo 호출 1회 점검은 별도 승인 시에만 하고, 결과 원문은 보고서에 붙이지 않는다.
   - 장시간 BUILDING 실행권 검증: `tests/test_auto_buy_shadow_review4_lease.py`(901초 이상 정상 작업자·pid 재사용·다른 호스트·시작 식별자 확인 불가·SIGKILL·실행권 상실 후 재개·동시 replay)가 배포 커밋에서 통과
   - VM 에서 프로세스 시작 식별자와 부트 식별자를 읽을 수 있는지 확인. 못 읽으면 `ALIVE_UNVERIFIED` 로 보수적으로 보호된다 (pid 재사용 판정 없음).
   - HTTP 오류·호출량 장애 주입(`tests/test_auto_buy_shadow_review5_http.py`), 멱등성·불변성(`review5_replay`), S5 연속성·Migration(`review5_s5`), 결함 재현(`review5_repro`)이 배포 커밋에서 통과
   - **Linux 검증 (활성화 전 필수):** 운영 VM 과 같은 계열 Linux(격리 환경)에서 `/proc` 시작 식별자·boot_id·pid 재사용·장시간 실행권 보호·SIGKILL 복구·fencing 테스트가 통과해야 한다 (아래 'Linux 호환성'). VM 자체에서 실행하지 않는다.
   - 수동 실행권 해제는 위 '수동 실행권 해제' 절차와 별도 승인으로만 한다
   - 일봉 응답 계약·호출 제한(`review6_contract`), 원장 충돌 교체 차단(`review6_ledger`), ab0005(`review6_migration`), 결함 재현(`review6_repro`)이 배포 커밋에서 통과
   - 위 '운영 검증 체크리스트'를 첫 주 관측 항목으로 등록
   - 테이블별 불변성 계약(`review7_contract`), 세션·호출 제한 카운터(`review7_session`), ab0006(`review7_migration`), 결함 재현(`review7_repro`)이 배포 커밋에서 통과
   - 동결 신호 집합(`review8_signal_set`), ab0007 사전 검증(`review8_migration`), 429 원자성(`review8_rate_limit`), 결함 재현(`review8_repro`)이 배포 커밋에서 통과
   - AUTO BUY MONITOR 에 '동결 신호 집합 무결성 오류' 표시가 없고 S5 `signal_set_integrity` = 0
   - 적격 판정 완전성·계좌 트랜잭션 재검증·원본 계약(`review9_contract`), 결함 재현(`review9_repro`)이 배포 커밋에서 통과
   - 기존 저널(있다면)의 `--check` 가 원본 계약까지 통과 (`RAW_CONTRACT_FAILED` 아님)
   - S5 `integrity_day_block` = 0, runtime `integrity_day_block` 없음
   - P7-OPS 실데이터 대조(로컬 캐시)를 최신 커밋으로 다시 실행해 기준값과 일치하고 `raw_contract_issues` = 0
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
| S2 | SHADOW 전용 SQLite·Migration | 구현 완료 (`ab0001`~`ab0006` 동결 + `ab0007` 신호 집합·관계 계약, 최종 목표 `ab0007`), **Migration 실행 미승인** |
| S3 | 스위치·SHADOW job 배포 (기본 OFF) | 구현 완료, Codex 재리뷰 대기 (2026-11-27 재리뷰 반영분) |
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
- 다른 호스트의 실행권은 사망을 확인할 수 없어 자동 정리하지 않는다. 응답 없는 작업자는 수동 확인 후 해제해야 하며 (위 '수동 실행권 해제'), 그동안 재계산은 대기한다.
- Yahoo 요청 예산·backoff 는 job 단위다. 여러 job 이 동시에 돌지 않는 단일 프로세스 스케줄러를 전제로 한다. 제한이 길게 이어지면 그날 일봉은 결측(복구 대상)으로 남는다.
- OPS 계좌의 과거 손익은 당시 계좌 스냅샷이 없어 역사 데이터로 검증할 수 없다.
