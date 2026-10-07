# 실제 PostgreSQL 검증 — 2026-10-08

실제 로컬 PostgreSQL 16.15에서 A단계 테스트 **42개 통과, 경고 1개** (1.91초).
이 중 DB를 쓰는 테스트는 PostgreSQL에 직접 연결했고, 설정/입력 검증과 기존 offline SQL 생성 테스트도 함께 실행했다.
SQLite 재검증도 **42개 통과**. 경고는 기존 SQLAlchemy API의 사용 중단 예정 안내다.
운영 코드 수정은 필요하지 않았으며 테스트 환경 선택 기능만 추가했다.

## 확인한 범위

- 기존 Alembic 리비전 7b21d9c4e6a0에서 신규 head로 실제 업그레이드 및 반복 실행.
- 기존 계좌 데이터 보존, 모델과 DB 스키마 일치, 제약조건 확인.
- 테스트 DB에서 downgrade 후 기존 데이터 보존 및 재업그레이드.
- 동일 관측 동시 저장, 동시 상태 갱신의 단일 성공, 발송 작업의 중복 확보 방지.
- 상태·이벤트·발송 저장 실패 시 트랜잭션 롤백.
- 저장 객체 재생성 후 재시도, 만료된 처리 권한 회수, 이전 처리자의 응답 차단, 재시도 소진.

DB 서버 프로세스의 강제 종료/복구, 운영 부하 성능, 실제 외부 발송은 이번 검증 범위에 포함하지 않았다.
로컬 서버의 격리 수준은 READ COMMITTED다. 운영 서비스의 버전·확장·권한·연결 풀러까지 재현한 검증은 아니다.

## 격리와 정리

Homebrew PostgreSQL 16과 필요한 의존성을 설치했다. 자동 시작 서비스는 등록하지 않았다.
기본 설치 클러스터 대신 `/private/tmp/scanner-runtime-pg-20261008/data`에 별도 임시 클러스터를 만들었다.
TCP 수신을 끄고 사용자 전용 디렉터리의 Unix socket만 사용했다.
`runtime_foundation_test` DB 안에서 테스트마다 임의 이름의 스키마를 생성하고 삭제했다.
잔여 테스트 스키마 **0개**, 임시 PostgreSQL 서버 **종료 완료**.
설치 프로그램과 종료된 임시 클러스터 파일은 로컬에 남아 있다.
운영 DB 접속·배포·실제 메시지·주문은 수행하지 않았다.

## 재현

`tests/test_agent_events.py`는 `RUNTIME_TEST_POSTGRES_URL`이 없으면 기존 SQLite를 사용한다.
이 변수를 지정할 때는 전용 임시 소켓 경로와 `runtime_foundation_test` DB만 허용한다.
기존 전체 테스트용 운영 자격증명 차단은 유지했다.
아래 명령은 해당 임시 서버가 실행 중일 때 저장소 루트에서 사용할 수 있다.

```sh
RUNTIME_TEST_POSTGRES_URL='postgresql+psycopg2://soohan@:55438/runtime_foundation_test?host=/private/tmp/scanner-runtime-pg-20261008' PYTHONPATH=../audit/runtime-phase0/test-dependencies ../myStockApp-scanner-vm/.venv/bin/python -m pytest tests/test_agent_events.py -q
```

원본 결과는 workspace의 `audit/runtime-postgresql/`에 있다:
`pytest.txt`, `results.xml`, `server-verification.txt`, `sqlite-regression.txt`, `initdb.txt`.
실제 PostgreSQL 검증 공백은 해소했다. A단계는 여전히 기본 OFF이며 스캐너 연결은 후속 B단계다.
