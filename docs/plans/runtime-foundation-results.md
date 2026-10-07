# A단계 완료 기록

> 2026-10-08 후속 검증: 실제 로컬 PostgreSQL 16.15에서 42개 테스트가 통과했다. 아래 PostgreSQL 미검증 문구는 최초 완료 시점의 기록이며, 상세 결과는 [PostgreSQL 검증 기록](runtime-postgresql-results.md)을 참조한다.

2026-10-07. 운영 코드 e2bf7be 기준, 분리된 로컬 브랜치
`codex/runtime-foundation`에서 구현했다. 운영 배포·DB 변경·실제 발송은 하지 않았다.

## 구현

- 관측, 현재 상태, 이벤트, 발송 작업을 저장하는 테이블 4개를 추가했다.
- 동일 관측 재처리, 내용이 다른 ID 재사용, 오래된 관측 덮어쓰기와 동시 상태 갱신을 제어한다.
- 상태·이벤트·발송 작업은 한 트랜잭션으로 확정한다. 발송 작업에는 만료 가능한 처리 권한과 제한된 재시도를 둔다.
- 런타임과 발송 설정은 기본 OFF다. 기존 스캐너·알림 선택·스케줄·웹·주문 경로는 수정하지 않았다.
- 원본 작업 폴더의 기존 수정 파일 5개는 그대로 보존했다.

## 검증

- 신규 테스트: **42 passed**.
- 전체 테스트: **659 passed, 6 warnings, 20.31s**. 경고는 기존 라이브러리 사용 중단 예정 안내다.
- Python 컴파일 검사와 `git diff --check` 통과.
- SQLite에서 마이그레이션·기존 데이터 보존·스키마 일치·되돌리기·재적용 확인.
- 중복 처리, 동시 상태 갱신/발송 확보, 트랜잭션 실패 시 롤백, 재시작, 권한 만료와 오래된 응답, 재시도 소진을 검증했다.
- PostgreSQL SQL 생성은 통과했다. 로컬 PostgreSQL과 실행 중인 Docker 서버가 없어 실제 PostgreSQL 마이그레이션 및 동시 실행 검증은 남아 있다.

재현 명령(이 저장소 폴더에서 실행):

```sh
PYTHONPATH=../audit/runtime-phase0/test-dependencies ../myStockApp-scanner-vm/.venv/bin/python -m pytest -q
../myStockApp-scanner-vm/.venv/bin/python -m compileall -q agent_runtime database alembic/versions/a41e7c9d2b60_agent_runtime_foundation.py
git diff --check
```

## 다음 단계와 제한

A는 저장 기능만 제공한다. 이벤트 판단, 스캐너 연결, 실제 발송 처리는 아직 없다.
관측·이벤트 변경을 제공하지 않는 저장 API이며, DB 관리자에 의한 직접 수정을 금지하는 트리거는 없다.
외부 서비스의 정확히 한 번 발송은 보장하지 않는다. 발송 직후 프로세스가 종료되는 경우의 중복과 부분 발송은 후속 발송 계층에서 다뤄야 한다.

운영의 기존 ‘발송 실패 후 재알림 억제’ 문제는 아직 남아 있으며 후속 연동에서 해결해야 한다.
B단계에서는 전체 관측 수집과 상태 변화 판단을 발송 없이 비교 검증한다.
실제 적용 전 PostgreSQL 검증이 필요하다. 기능 OFF여도 앱 시작 시 신규 테이블 마이그레이션은 실행되므로 배포 자체가 DB 변경을 포함한다.
운영 롤백은 기능 OFF와 테이블 보존을 기본으로 하며, 데이터 삭제를 수반하는 downgrade는 테스트에서만 수행했다.
