# 키움 국내 모의투자 매수·매도·취소 검증

## 설계 판단

현재 웹의 국내 종목 주문 기능은 구현되어 있지 않다. `web/kiwoom_sizing.py`에서는
국내 스캐너 후보의 대상 계좌를 `account4`(ISA)로 정하지만, 모의투자 키의 프로필
이름은 실제 계좌 구성과 다를 수 있다. 따라서 진단 도구는 프로필을 명령에서 반드시
지정하며 운영 화면이나 실제 주문 스위치에는 연결하지 않는다.

- **의도된 동작:** 국내 후보의 기본 대상은 account4이며 실제 주문 전 사용자 확인이 필요하다.
- **구현 오류 보완:** 국내 주문·취소 클라이언트와 체결 증거가 없어 응답 유실 및 중복 주문을 안전하게 시험할 수 없었다.
- **전략 변경 제안 아님:** 종목 선정, 매수·매도 조건, 주문 가격 결정 방식은 이번 구현에서 정하지 않았다.

## 구현 범위

- 지정가 매수 `kt10000`, 지정가 매도 `kt10001`, 취소 `kt10003`
- 주문가능 현금 `kt00001.ord_alow_amt`
- 전체 미체결 `ka10075`, 주문·체결 상세 `kt00007`
- 국내 모의투자는 KRX만 사용
- 수량은 1주로 고정
- 매수는 주문가능 현금과 1% 여유금을 확인
- 매도는 해당 종목의 매도가능수량을 확인
- 주문과 취소 전송 전에 SQLite 상태를 영속 저장
- 타임아웃은 `ORDER_UNKNOWN` 또는 `CANCEL_UNKNOWN`으로 저장하고 자동 재전송 금지
- 취소 완료는 원주문이 미체결 목록에서 사라진 사실만으로 판단하지 않는다. 취소 주문번호,
  원주문번호, 취소 표시가 일치하는 주문 상세 행도 함께 요구한다.
- 매수 취소 뒤 현금, 매도 취소 뒤 매도가능수량이 기준값 이상 복구되어야 완료한다.
- 조회 전용 API의 HTTP 429는 2·4·8·16초 간격으로 제한해 재시도한다. 주문·취소
  전송에는 이 재시도를 적용하지 않는다.
- 체결 완료는 주문 상세의 완전체결과 계좌 보유수량 변화가 함께 일치해야 인정한다.
- 키움 국내 모의투자는 시간외 거래를 지원하지 않는다는 실응답을 확인했다. 모의계좌
  체결 검증은 정규장에 진행한다.

## 격리 테스트 명령

아래 명령은 `prepare`까지만 조회 전용이다. `submit`과 `cancel`은 키움 모의계좌에
실제 모의 주문을 전송하므로 각 단계의 JSON 결과를 확인한 뒤 따로 실행한다.

```bash
python -m scripts.kiwoom_domestic_mock_order_test \
  --journal /tmp/kiwoom-domestic-mock.sqlite3 \
  --profile account4 \
  prepare --test-key 2026-09-14-005930-buy \
  --side BUY --ticker 005930 --limit-price 50000
```

준비 결과의 `details.confirmation`을 그대로 전달한다.

```bash
python -m scripts.kiwoom_domestic_mock_order_test \
  --journal /tmp/kiwoom-domestic-mock.sqlite3 --profile account4 \
  submit --test-id TEST_ID --confirm 'BUY:005930:1@50000'

python -m scripts.kiwoom_domestic_mock_order_test \
  --journal /tmp/kiwoom-domestic-mock.sqlite3 --profile account4 \
  verify-open --test-id TEST_ID

python -m scripts.kiwoom_domestic_mock_order_test \
  --journal /tmp/kiwoom-domestic-mock.sqlite3 --profile account4 \
  cancel --test-id TEST_ID --confirm 'CANCEL:0000123'

python -m scripts.kiwoom_domestic_mock_order_test \
  --journal /tmp/kiwoom-domestic-mock.sqlite3 --profile account4 \
  verify-cancel --test-id TEST_ID
```

매도 테스트는 `--side SELL`을 사용하며 모의계좌에 매도 가능한 해당 종목 1주가
있어야 한다. 지정가는 예시를 그대로 사용하지 않고 테스트 시점의 호가와 가격 단위를
확인해 정한다.

## 공식 계약 근거

키움증권 공식 REST API 저장소의 `kiwoom/_data/kiwoom_api_spec.json`을 기준으로
요청 필드와 응답 필드를 구현했다.

- 주문 경로: `POST /api/dostk/ordr`
- 국내 모의투자 지원 거래소: KRX
- 지정가 주문: `trde_tp=0`, 정수 원화 `ord_uv`
- 전량 취소는 `cncl_qty=0`이지만 이 진단은 정확히 1주만 취소한다.

이 기능은 향후 국내 실주문을 자동 허용하지 않는다. 모의 서버에서 실제 상태 변화를
검증하고, 계좌 범위·호가 유효시간·가격 단위·수수료와 예약금 의미를 확정한 뒤 운영
미리보기와 사용자 확인 흐름을 별도로 설계해야 한다.
