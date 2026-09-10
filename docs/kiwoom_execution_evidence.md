# STEP1: 키움 체결 및 현금 검증 연결

2026-09-11 구현. 실주문·실계좌 조회 없이 공식 명세와 모의 HTTP 응답으로 검증했다.
이번 STEP1은 조회와 대조 구현을 진전시켰으나 **실제 재분배 주문을 허용하는 전체 연결은 미완료**다.

## 확인한 공식 명세

기존 query-string 링크는 OAuth 기본 문서를 반환했다. 실제 API별 문서는 아래 경로다.

- [ust21150 일별 주문체결](https://openapi.kiwoom.com/m/guide/apiguide/32/ust21150)
- [ust21050 원장 미체결](https://openapi.kiwoom.com/m/guide/apiguide/32/ust21050)
- [ust21110 해외 예수금](https://openapi.kiwoom.com/m/guide/apiguide/32/ust21110)
- [ust21160 예수금 상세](https://openapi.kiwoom.com/m/guide/apiguide/32/ust21160)
- [ust31490 종목별 주문가능수량](https://openapi.kiwoom.com/m/guide/apiguide/38/ust31490)

계좌 조회는 POST `/api/us/acnt`. 주문내역 `ord_dt`와 `ord_time`은 KST다.
`ust31490`은 조회 기능이지만 경로는 POST `/api/us/ordr`이다. 실제 주문 TR은 호출하지 않는다.
일별 내역은 `query_tp=1`, `slby_tp=0`으로 전체 방향의 주문을 조회한다.
`cntr_qty`, `ord_remnq`, `cncl_qty`, `mdfy_qty`, `ord_stat_nm`을 함께 대조한다.
`fc_ord_alowa`는 해당 통화의 외화주문가능금액이며, 예수금·출금가능금과 다르다.
명세는 이 필드의 미체결 예약금 및 수수료 반영 식을 설명하지 않는다.
`ust21160`의 결제예정 금액을 주문가능 현금 대신 사용하지 않는다.

## 구현

- `trading/kiwoom_execution_evidence.py`: 전용 허용 목록 기반 조회 클라이언트.
  성공 코드·목록·연속조회 키·페이지 제한을 검사한다. 중간 누락을 빈 결과로 처리하지 않는다.
  체결 상태를 FILLED/PARTIAL/OPEN/CANCELLED/REJECTED/MODIFIED/UNKNOWN으로 구분한다.
- 주문번호·KST 일자·종목·방향·수량·지정가·주문시각을 영속 의도와 대조한다.
  동일 주문번호가 여러 번 나오면 대조 실패. 주문번호가 없는 UNKNOWN은 자동 연결하지 않는다.
  수동 주문과 같은 조건의 주문을 구분할 근거가 부족하기 때문이다.
- `EvidenceBroker.lookup()`을 기존 재분배 서비스에 연결했다. snapshot/quotes/submit은
  검증되지 않은 기존 차단을 유지한다. 체결 대조 성공만으로 매수 단계로 넘어가지 않는다.
- `GET /api/asset-allocation/rebalance/evidence?order_day=YYYYMMDD`:
  account2 전용 인증 조회. 주문 상태와 증권사 현금, 미체결 건수, 검증 제한을 반환한다.
  기존 API 인증 적용. 계좌 선택이나 주문 기능을 노출하지 않는다.
  이 결과는 다른 전략 주문도 조회할 수 있으나 그 주문을 수정하거나 재분배 범위에 넣지 않는다.
- `capabilities`는 조회 구현 여부와 현금 의미 검증 여부를 분리한다.
  이것은 연결 성공/실계좌 검증 완료 표시가 아니다.
- `GET /api/asset-allocation/rebalance/buy-capacity?ticker=SPY&exchange=NY&limit_price=100.00`:
  종목·지정가별 `ust31490` 조회. USD `ord_alowa`와 `min_ord_alowa` 중 작은 금액에
  설정 여유금을 적용한 예산, `min_ord_alowq`와 예산 내 정수주 중 작은 수량을 반환한다.
  50% 증거금 및 원화주문 한도를 사용하지 않는다. 다른 ETF별 조회 한도는 같은 현금을
  공유하므로 합산해서 사용하면 안 된다. 이것만으로 실행 검증 완료를 의미하지 않는다.

## 현재 남은 제한

`validated_for_execution=false`를 유지한다. 이유:

1. 주문가능금액의 예약금·수수료 반영 규칙을 공식 자료/증권사 답변으로 확인해야 한다.
2. 날짜별 미체결 조회가 이전 날짜의 유효 주문까지 포함하는지 확인해야 한다.
3. 시세 원시각과 전략 전용 소유범위 검증이 별도로 필요하다.

현금 조회값에 추정 매도대금을 더하지 않고, 의미가 불명확한 예약금을 다시 빼지도 않는다.
상기 근거가 확보될 때까지 안전하게 실행 가능한 현금으로 승격하지 않는다.
새 API는 서버 라우터에 구현했으며 Sites 화면/프록시에는 아직 연결하지 않았다.
실계좌 접근과 운영 배포는 이번 STEP1 변경에서 수행하지 않았다.

## 테스트

모의 HTTP 세션으로 체결·부분체결·미체결·취소·거절·정정, 잘못된 코드/목록,
중복 주문번호, 주문 조건 불일치, 응답 유실, 연속조회·누락·페이지 초과,
USD 누락·중복·NaN·음수, 날짜 오류와 API 라우팅을 검증한다.
기존 재분배 테스트가 중복 클릭·재시작·다른 전략 보유·다종목 예산 상한을 검증한다.
