# B단계 구현·검증 결과

2026-10-08. 기준 커밋 d3f83a5, 작업 브랜치 `codex/runtime-shadow`.
로컬 구현과 검증 완료. 운영 배포·GitHub push·설정 활성화는 하지 않았다.

## 구현 결과

- HOLD, 반복 억제된 경고, 실제 알림 대상, 판정 실패를 모두 별도 관측으로 기록한다.
- 기존 Weinstein 판단을 그대로 받아 상태 변화와 품질 변화를 구분한다. 장애 전 마지막 유효 상태·시점을 보존하고, 입력/전략 변경을 시장 변화와 구분한다.
- 기존 알림 상태의 복사본과 동일한 알림 정책 함수로 선정 결과를 비교한다. 이는 수집·연결의 일치성 검증이며 전략 자체의 독립 검증은 아니다. 손절 미등록의 개별 반복 억제와 묶음 포함 여부도 비교한다.
- 관측에 보유 입력, 이전 알림 상태, 반복 주기, 판정 이유, 데이터 시점, 버전, 기존 선정 결과와 비교 결과를 저장한다.
- 거래 기록의 청산·재매수와 관측된 보유항목 부재로 에피소드를 구분한다. 계좌가 다르면 같은 티커도 분리한다.
- 예상 관측 목록을 먼저 커밋하고, 기존 스캔 DONE 커밋 후 관측을 별도 저장한다. 추가 쿼리도 별도 세션을 사용하여 PostgreSQL 오류가 기존 스캔 트랜잭션을 훼손하지 않는다.
- 관측 저장 후 완료 표시 전에 중단되면 영속 관측을 재생해 복구한다. 원본이 없는 누락은 gap으로 남겨 이후 처리를 막으며, 명시적 사유로 기준선을 재설정할 수 있다.
- 늦은 과거 관측은 감사 기록에 남기고 현재 상태를 되돌리지 않는다. A의 스캔 시작 ID/개정 커서를 유지하며 데이터 시점 역행도 거절한다.
- B 경로는 발송 설정과 관계없이 delivery를 강제로 비활성화한다. 기존 알림 발송 경로는 유지된다.

## 파일과 DB 영향

- `agent_runtime/holding_capture.py`: 기존 판정 관측 수집, 입력·에피소드 식별, 선정 비교.
- `agent_runtime/shadow.py`: 완료 목록, 재생, 누락·지연 관측 감사, 상태 변화 기록.
- `agent_runtime/shadow_cli.py`: 현황 조회·재생·명시적 gap 재설정.
- `scanner/scan_engine.py`: 기본 OFF인 선택적 연결. 전략·알림 정책은 변경하지 않았다.
- `database/models.py`와 migration `b62f8d0e3c71`: `agent_scan_captures` 테이블 하나 추가. 부모 리비전은 `a41e7c9d2b60`.
- `tests/test_agent_shadow.py`: 신규 34개 테스트. `tests/conftest.py`는 테스트 기본 runtime/delivery OFF를 강제한다.

## 검증 결과

| 검증 | 결과 |
|---|---|
| 기존 테스트 포함 전체 SQLite 회귀 | **693 passed**, 6 warnings, 19.95초 |
| PostgreSQL 16.15 A+B 테스트 | **76 passed**, 1 warning, 4.33초 |
| ON/OFF의 반환 알림·기존 보유 상태 동일성 | HOLD/LOW/MEDIUM/HIGH × 손절 등록/미등록 8조합 통과 |
| 중간 관측 누락·영속 기록 재생·동시 처리·재시작 경계 | 통과 |
| 실제 PostgreSQL 추가 조회 오류의 트랜잭션 격리 | 통과 |
| A 데이터 유지한 B migration·반복 실행·B만 downgrade·재적용 | 통과 |
| Python 컴파일 및 diff 공백 검사 | 통과 |

경고는 기존 SQLAlchemy/FastAPI 등의 사용 중단 예정 안내다.
격리 PostgreSQL 테스트 스키마를 정리하고 임시 서버를 종료했다. 운영 DB/알림/주문은 사용하지 않았다.
원본 결과는 workspace의 `audit/runtime-shadow/`에 보관했다.

## 후속 운영 절차

배포 시 migration이 테이블을 추가한다. 수집을 시작할 때는 `AGENT_RUNTIME_ENABLED=true`,
`AGENT_DELIVERY_ENABLED=false`로 두고 서비스를 재시작한다. B는 실수로 delivery가 true여도 발송 작업을 생성하지 않는다.
이는 실행한 배포 기록이 아니라 후속 활성화 절차다.

```sh
python -m agent_runtime.shadow_cli status
python -m agent_runtime.shadow_cli replay --limit 200
python -m agent_runtime.shadow_cli reset-gap SCAN_ID --reason '원본 복구 불가 사유와 기준선 재설정 근거'
```

`status`에서 pending, mismatches, gaps, stale_audit를 확인한다. 새 스캔 완료 때도 최대 200개 관측을 재생한다.
gap reset은 누락 이력을 지우지 않으며 다음 관측의 BASELINE_RESET에 gap scan ID와 중간 변화 누락 가능성을 남긴다.
RUNNING 스캔은 reset할 수 없다. 프로세스 중단으로 남은 RUNNING은 실제 실행 여부 확인과 스캔 로그 상태 정리가 먼저다.
기능 OFF로 롤백하면 기존 스캔 동작으로 돌아가며 신규 기록은 보존한다.

## 남은 한계

- 운영 shadow 데이터는 아직 수집하지 않았다. 2주 관찰·실제 불일치율·유용성 검토는 활성화 이후다.
- 목록 자체 저장 실패 또는 목록 작성 전 종료는 완전한 gap 탐지가 불가능하다. 추가 기록 매체도 실패하면 로그조차 보장하지 않는다.
- 기록 없는 청산/재매수가 스캔 사이에 발생하면 구분할 수 없다. 관측되지 않은 중간 판단을 추론하지 않는다.
- 데이터 시점은 일봉 피드 라벨이다. 봉 확정은 UNKNOWN이며 AVAILABLE은 입력 공급 여부이지 모든 전략 분기의 충분한 이력/봉 확정을 보장하지 않는다. 폴백 여부를 제공하지 않는 피드에서 임의로 폴백/확정을 추정하지 않는다.
- 알려진 앞선 capture는 순서를 막지만, 아직 목록이 등록되지 않은 늦은 스캔은 이후 도착 시 감사 대상으로 남는다. 완전한 시간순 역사 재구성은 하지 않는다.
- 재생 상한은 처리 건수 제한이며, 파일럿 조회는 보존된 목록·후보를 메모리로 읽는다. 대규모 이력 성능과 실제 부하 검증은 별도다. 자동 삭제/타이머는 추가하지 않았다.
- 기존 발송 실패 뒤 재알림 억제 문제는 아직 C단계 전환 대상이다. B는 기존 발송을 교체하지 않는다.
