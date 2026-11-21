# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 작업 시작점 (필수)

먼저 `AGENTS.md`를 읽고 적용한다. 코드를 바꾸기 전에 관련 설계 MD를 읽고 코드와 대조하며, 의도된 동작 / 구현 오류 / 전략 변경 제안을 구분한다.

- ETF 자산배분(account2): `docs/etf_asset_allocation.md`, `docs/etf_rebalance_review.md`를 읽은 뒤 수정한다. 현재 방식은 관리 ETF에 대한 **차액 재분배**이며 전량매도 방식으로 되돌리지 않는다.
- 증권사 실연결의 미완료 사항은 문서에서 확인한다. 주문 차단을 단순 제거하지 않는다.
- 스캐너 로직: `docs/step1_review_checklist.md`, `docs/step2_review_checklist.md`. 섹터 표시는 `docs/sector_trends.md`(표시 전용, Gate 2·매매 판정에 섞지 않음).
- 스캐너 조회 MCP: `docs/scanner_read_mcp.md`. 계좌 현황 MCP: `docs/portfolio_read_mcp.md` — 스캐너·사이트·주문 자격 증명과 분리된 조회 범위를 유지한다.
- 스캐너 후보 매수(account1 미국, account4 국내): `docs/scanner_buy_orders.md`. 시장별 전송 스위치를 유지한다.
- 모의투자 준비: `docs/kiwoom_step1_research.md`.
- 테스트를 위해 실주문이나 운영 DB를 쓰지 않는다. 배포 성공과 실제 주문 기능 완성을 구분해 보고하고, 미검증 체결·현금을 성공으로 간주하지 않는다.

## 명령어

로컬 가상환경은 `.venv/`다 (`start_server.sh`/`.command`의 `/Users/mac/...` 경로는 다른 머신용 하드코딩).

```bash
.venv/bin/python main.py                      # 서버 실행 (127.0.0.1:8000, uvicorn web.app:app)
.venv/bin/python -m pytest                    # 전체 테스트
.venv/bin/python -m pytest tests/test_sizing.py -q            # 파일 하나
.venv/bin/python -m pytest tests/test_sizing.py::test_name    # 테스트 하나
alembic upgrade head                          # 마이그레이션 (앱 기동 시 init_db()가 자동 실행)
```

린터/빌드 설정은 없다. `.env`는 `.env.example`을 복사해 만든다.

## 아키텍처

FastAPI 앱(`web/app.py`) + APScheduler(`scheduler.py`) 단일 프로세스. Weinstein Stage 분석 스캐너가 KR/US 종목을 스캔해 DB에 저장하고, 키움 증권 REST로 계좌 조회·주문을 한다.

- `scanner/` — 스캔 파이프라인. `scan_engine.run_scan()`이 시장별 유니버스(`us_stocks.py`, `kr_stocks.py`)를 돌며 `weinstein.analyze_stock`(주봉 MA·Base/Pivot·RS 등), `strict_filter`, `entry_control`, `market_analysis`(시장 필터), `sizing`을 적용한다. 임계값은 거의 전부 `config.py`의 환경변수.
- `scheduler.py` — 시장별 정규장 마감 후 자동 스캔(`KR_/US_SCHEDULE_TIMES`)과 월간 자산배분 계산.
- `database/models.py` — SQLAlchemy 1.x 모델. 스키마는 **Alembic 소유**(`alembic/versions/`); 손으로 ALTER 하지 않는다. 운영은 Neon(Postgres), 로컬/테스트는 SQLite.
- `trading/` — 키움 연동. `kiwoom_readonly`(조회), `kiwoom_orders`, 스캐너 후보 매수(`scanner_buy_orders`), ETF 차액 재분배(`allocation_rebalance` + `kiwoom_allocation_{mock,live}_broker`, 영속 주문 저널 `ALLOCATION_JOURNAL_PATH`), 체결 근거(`kiwoom_*execution_evidence`). 모든 주문 경로는 환경변수 스위치(`KIWOOM_TRADING_ENABLED`, `ACCOUNT{1,4}_ORDER_ENABLED`, `ALLOCATION_EXECUTION_MODE=disabled|mock|real`)로 차단되며 기본은 꺼짐.
- `web/` — 라우터들(`asset_allocation_api`, `holding_order_api`, `scanner_order_api`, `scanner_read_api`, `portfolio_read_api`)과 키움 보유/사이징/매도분석 보조 모듈, 정적 프런트·템플릿.
- `integrations/scanner_mcp`, `integrations/portfolio_mcp` — 별도 venv/의존성의 MCP 서버(stdio + 원격 HTTPS). 각각 읽기 전용이며 서로 다른 토큰/scope(`scanner:read` vs `portfolio:read`)를 쓴다. `deploy/`에 systemd·nginx 설정.
- `agent_runtime/` — 이벤트/섀도 에이전트 런타임(`AGENT_RUNTIME_ENABLED`로 제어).
- 계좌 매핑: account1=자유투자(미국), account2=퀀트/ETF 배분 전용(미국), account4=ISA(국내).

## 테스트 격리

`tests/conftest.py`가 import 순서에 의존해 운영 `.env` 값을 덮어쓴다(임시 SQLite, 알림·주문·토큰 비활성). 운영 DB에 바인딩되면 세션 픽스처가 실패한다. 새 테스트에서 `config`/`database.models`를 conftest보다 먼저 import하지 말고, 외부 호출(키움, 섹터 공급자, Slack)은 모킹한다. 섹터 공급자 자체를 테스트할 때만 `@pytest.mark.sector_providers`를 쓴다.
