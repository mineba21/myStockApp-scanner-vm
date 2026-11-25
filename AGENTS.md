# 저장소 작업 지침

코드를 바꾸기 전에 관련 설계 MD를 먼저 읽고 코드와 대조한다.
의도된 동작, 구현 오류, 전략 변경 제안을 구분한다.

- ETF/account2 작업: `docs/etf_asset_allocation.md`, `docs/etf_rebalance_review.md`를 먼저 읽는다.
- 스캐너 작업: `docs/step1_review_checklist.md`, `docs/step2_review_checklist.md`를 먼저 읽는다.
- 섹터 표시·최근 추세: `docs/sector_trends.md`를 먼저 읽는다. 표시 전용이며 Gate 2·매매 판정에 섞지 않는다.
- 스캐너 MCP/조회 연결: `docs/scanner_read_mcp.md`를 먼저 읽는다. 조회키의 계좌·주문 권한을 확대하지 않는다.
- 스캐너 후보 매수(account1 미국, account4 국내): `docs/scanner_buy_orders.md`를 먼저 읽는다. 미검증 항목과 시장별 전송 스위치를 유지한다.
- account1 미국 S01 자동 손절: `docs/auto_stop_sell.md`를 먼저 읽는다. 기본 OFF, 자동 취소·재주문 기본 꺼짐을 유지한다. 저널 스키마는 저널 전용 Migration(`scripts.auto_stop_journal_migrate`)으로만 만들고 런타임(스케줄러·API·store)에서 DDL을 실행하지 않는다.
- account1 미국 Scanner 자동매수 SHADOW: `docs/auto_buy_shadow.md`를 먼저 읽는다. 기본 OFF, LIVE 미구현·거부, 가상 계좌만. SHADOW 저널은 전용 Migration으로만 만들고 런타임에서 DDL·파일 생성을 하지 않는다. 운영 DB에 표를 추가하지 않는다.
- 계좌 현황 MCP: `docs/portfolio_read_mcp.md`를 먼저 읽는다. `portfolio:read`는 스캐너·사이트·주문 자격 증명과 분리한다.
- ETF는 최신 사용자 결정에 따라 차액 재분배한다. 전량매도 방식으로 되돌리지 않는다.
- 모의 응답과 격리 테스트를 사용한다. 테스트를 위해 실주문이나 운영 DB를 사용하지 않는다.
- 배포 성공과 실제 주문 기능 완성을 구분해 보고한다. 미검증 체결·현금을 성공으로 간주하지 않는다.
- 사용자 기존 변경은 보존하며 현재 요청과 무관한 개별주 전략 변경을 섞지 않는다.

이 지침은 2026-09-11에 추가했다. 이전 설계의 근거로 소급하지 않는다.

## 현재 구현에서 유지할 계약 (2026-11-22 갱신)

- 스캐너 매수는 `strict_filter_passed is True`인 후보만 허용한다. 거부(`false`)뿐 아니라 미평가(`null`·누락)도 시세 조회·미리보기와 화면 진입에서 차단한다.
- 계좌 잔고가 불완전하면 누락 종목을 0주로 추정해 리밸런싱하지 않는다. 평가 합계를 확인하지 못한 계좌는 다른 TR이나 0으로 대체하지 않고 오류를 표시한다.
- 기존 보유의 손절가·현재가가 없어 위험을 계산할 수 없으면 제안 수량을 숨긴다. `live_sizing`은 대상 키움 계좌·시장 기준, 스캔 `suggested_qty`는 활성 수동 보유의 시장 기준이다. 두 기준을 임의로 합치거나 수량 입력 기본값을 바꾸지 않는다. 상세는 `docs/scanner_buy_orders.md`를 따른다.
- account2 ETF 주문은 `asset_allocation_api`의 영속 재분배 사이클을 사용한다. 제거된 `/api/kiwoom/orders/*`는 404를 유지하며 옛 개별 실행 경로를 복원하지 않는다. 스캐너 주문은 별도 경로다.
- 영속 주문 저널과 OAuth 상태 DB를 삭제·초기화하거나 과거 사본으로 덮어쓰지 않는다. 접수 불명 주문은 자동 재전송하지 않는다.
- 제한적 예외 (2026-11-24 사용자 승인): account1 미국주식 S01 자동 손절(`trading/auto_stop/`, `docs/auto_stop_sell.md`)에 한해, 사람 확인 없는 손절 지정가 매도와 미체결 시 **취소 확정 후 잔여 수량 재주문**(세션 3회·이벤트 6회 한도, `AUTO_STOP_US_REPLACE_ENABLED`)을 허용한다. 전송·취소 결과 불명은 자동 재시도하지 않고 증권사 주문내역 증거로만 해제한다. 스캐너 매수, 수동 주문, account2 리밸런싱, account4·국내·다른 계좌에는 적용하지 않는다. 이 승인은 개발 승인이며 LIVE 활성화는 운영자가 SHADOW 검증 후 별도로 결정한다.

## 검증과 배포

- 기본 화면(`web/templates/index.html`)과 별도 Sites 소스·프록시는 따로 관리된다. VM 변경만으로 Sites가 바뀌었다고 보고하지 않는다. API 제거 전에는 배포된 Sites 버전의 호출도 확인하고 필요한 Sites 정리를 먼저 배포한다.
- 블로킹 증권사·외부 조회는 FastAPI 이벤트 루프에서 직접 실행하지 않는다. 스캔·자산배분의 실행권 선점과 오류 시 해제를 보존하며, 스캔 실행 상태는 DB 정리 완료 후 한 곳에서 해제한다.
- 앱 테스트는 `.venv/bin/python -m pytest`로 실행한다. `tests/conftest.py`의 임시 SQLite·자격 증명·알림 격리를 유지한다. 날짜 신선도 테스트는 기준시각을 고정하거나 실행일에 맞는 입력을 사용한다.
- MCP OAuth·stdio 검증은 별도 MCP 가상환경에서 `remote_smoke.py`, `protocol_smoke.py`를 실행한다. 운영 API 대신 가짜 응답과 localhost 서버를 사용한다.
- MCP 인증 변경 전에는 `docs/scanner_remote_mcp.md`도 읽는다. 서비스는 저장소가 아닌 `/opt` 설치본을 실행하므로 `git pull`만으로 배포됐다고 판단하지 않는다. `remote.py`와 `owner_password.py`를 함께 설치하고 해당 서비스를 확인한다.
- MCP 승인 암호는 VM 로그인·SSH·키움 암호와 별개다. 새 검증값은 솔트 scrypt를 사용하며 원문·토큰을 Git·채팅·로그에 남기지 않는다. 암호 재발급과 코드 배포를 구분하고 기존 OAuth 연결을 임의로 폐기하지 않는다.
- 배포 전 원격 작업 트리·진행 중 스캔·대상 커밋을 확인하고, 배포 후 실제 파일·서비스·헬스와 변경된 API 계약을 확인한다. 계좌·주문 API를 단순 헬스 확인용으로 호출하지 않는다.
- 현재 배포 커밋·일시·테스트 개수는 이 지침에 고정하지 않고 작업 결과로 보고한다. DB 스키마 변경은 Alembic을 사용한다.
