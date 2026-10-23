# B 실계좌 보유 관측 연결

2026-10-23, 기준 9bafd32. 웹의 키움 보유현황과 내부 Holding은 별도 원천이다.
기존 B가 내부 Holding만 읽어 실제 계좌에 종목이 있어도 관측 0으로 남았다.
이는 조회 연결 누락이며 매매 전략이나 신규 알림 정책 변경이 아니다.

## 구현 계약

- Runtime ON + KIWOOM_WEB_ENABLED일 때 매 스캔 완료 과정에서 신선한
  account1 US / account4 KR 잔고를 조회한다. 기존 웹 Weinstein 매도 분석 대상과
  일치하며 ETF account2는 해당 전략 범위 밖이다. 시장별 스캔 때도 두 계좌를
  관측하여 기존 전체 보유 관측의 의미를 유지한다.
- 조회 전용 KiwoomReadOnlyClient의 필요한 잔고 API만 사용한다. 주문 경로,
  웹 캐시, 내부 Holding/Transaction을 만들거나 수정하지 않는다. 성공 빈 잔고와
  설정 누락·권한 오류·조회 실패를 구분한다. 잔고 조회 응답의 complete 플래그로
  페이지 제한·연속조회 누락·잘못된 행 형식을 검출하고, 수량·평단 원문도 검사한다. 완전한 두 계좌 조회 실패는
  durable missing manifest로 남겨 gap 및 replay 장벽이 된다.
- 기존 수동 보유의 알림 선정·상태 저장은 그대로 유지한다. broker 관측은
  동일한 웹 _analyze_one을 사용하며 캐시 없이 판정을 수집한다. 기존 수동
  알림 목록에 broker 신호를 추가하지 않는다. ShadowStore가 delivery를 강제 OFF한다.
- 전체 key(source/profile/market/ticker)와 확인된 잔고 부재로 episode를 구분한다.
  수량·평단 변경은 input_version 변경이다. 잔고 조회 실패는 부재 근거가 아니다.
  실제 거래 내역이 없어 관측 사이 청산·재진입은 식별할 수 없음을 명시한다.
- 관측 정수 account_id/holding_id는 source 범위의 진단용 참조이며 수동 포트폴리오
  FK가 아니다. episode/id는 전체 key 해시를 사용한다. holding_id 해시 충돌은
  manifest 이력과 현재 snapshot을 검사해 gap으로 차단한다. 수동 부재 판정은
  source=manual만 사용한다. 기존 manifest에는 source가 없어 manual로 해석한다.
- expected manifest에 broker_snapshot 성공 여부와 key 목록을 함께 저장한다.
  시세 조회 실패는 status=null / UNAVAILABLE 관측, 저장 실패는 gap이다.
  주봉·벤치마크 부족은 DEGRADED이며 일봉 날짜는 봉 확정이 아닌 feed label이다.
- 실계좌에는 기존 영속 알림 선정 경로가 없으므로 comparison.eligible=false,
  matches=null로 저장한다. report의 broker_observations와 comparison_unavailable로
  구분한다. 불일치 0을 알림 검증 성공으로 보고하지 않는다.
- DDL 변경 없음. 원 스캔 DONE 후 broker 평가와 관측 저장을 수행한다.
  따라서 ScanLog 완료 시각은 관측 완료보다 앞설 수 있다. 강제 종료 시 manifest
  누락을 감지하며 이후 잔고로 과거 자료를 조용히 덮어쓰지 않는다.

## 완료 판단

이 수정의 통과 기준은 격리 합성 잔고에 대한 수집·실패 가시화·수동 경로 회귀·
무발송 및 SQLite/PostgreSQL 저장 검증이다. 운영 반영 및 실제 자료 약 2주의
상태·품질 변화 검증은 별도다. 실계좌 관측 연결만으로 기존 알림 선정/실발송
신뢰성 검증을 완료하지 않는다. 실제 매수는 테스트 조건이 아니다.

## 2026-10-23 검증 결과

- 최종 전체 회귀: 869 passed (기존 SQLAlchemy/FastAPI/pykrx 경고 7개).
- 최종 로컬 PostgreSQL 16: 93 passed. 임시 Unix socket 전용 서버와 테스트별
  임시 스키마를 사용했고 운영 자격증명·운영 DB를 사용하지 않았다.
- 보완 후 집중 회귀: 79 passed. 빈 내부 보유에서 broker 관측 생성, KR/US 범위,
  소수점 수량, 증액·청산 후 재진입, 조회 장애·시세 장애·회복, 누락 장벽과
  명시적 reset, 잘못된 잔고·불완전 페이지, 수동 알림 유지, delivery 0건 확인.
- 신규 DDL 없음. 실계좌 조회 및 운영 스캔으로 변경 코드를 실행하지 않았다.
- 운영 배포·GitHub 반영 미실행. 배포 후 첫 정규 스캔에서 대상 실계좌 보유 수와
  broker expected/observations, 품질 및 comparison_unavailable을 대조해야 한다.
  내부 Holding 수가 0이어도 실계좌 관측을 0으로 간주하지 않는다.
