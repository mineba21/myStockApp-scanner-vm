# 키움 해외 모의투자 1주 주문·취소 검증

## 목적

STEP1의 주문 접수, 미체결 조회, 취소 완료, 주문가능 현금 복구를 해외
모의투자 계좌에서 한 단계씩 확인한다. 이 진단 도구는 ETF 자산배분 주문을
활성화하지 않으며 웹 애플리케이션에서도 호출하지 않는다.

## 안전 원칙

- `mode=mock`과 `https://mockapi.kiwoom.com`이 모두 맞아야 실행한다.
- 수량은 코드와 SQLite 제약조건에서 1주로 고정한다.
- 기존 미체결 주문이 하나라도 있으면 새 테스트 계획을 만들지 않는다.
- 주문 전 `PREPARED`, 전송 직전 `BUY_SENDING` 또는 `CANCEL_SENDING`을 SQLite에 먼저 저장한다.
- 타임아웃이나 응답 손실은 `UNKNOWN`으로 남긴다. 같은 주문을 자동 재전송하지 않는다.
- 매수와 취소는 각각 준비 결과에 표시된 확인 문구를 직접 전달해야 한다.
- 체결되면 취소 테스트를 진행하지 않고 `FILLED_UNEXPECTED`로 중단한다.
- `validated_for_execution=true`인 주문가능 현금 증거가 없으면 `prepare`에서 차단한다.
  현재 키움 모의 서버는 종목별 주문가능수량 `ust31490`을 지원하지 않으므로 실제
  해외 모의 주문 단계는 진행할 수 없다.
- 사용자가 이 제한을 확인하고 1주 모의 진단을 명시적으로 요청한 경우에만 전용 CLI의
  `--allow-unvalidated-capacity-for-mock-test`를 사용할 수 있다. 이때 확인 문구에는
  `UNVALIDATED_CAPACITY`가 추가되고 기록에도 제한 사유를 영속 저장한다. 웹·ETF 주문
  경로에는 이 예외를 제공하지 않으며 실행 검증 완료로 승격하지 않는다.

## 실행 순서

아래 경로와 가격은 예시다. 장 운영시간과 현재 가격을 확인한 뒤 실제 테스트
값을 정한다. 각 명령의 JSON 결과에서 `id`, `state`, `confirmation`을 확인한다.

```bash
python -m scripts.kiwoom_mock_order_test \
  --journal /tmp/kiwoom-overseas-mock-test.sqlite3 \
  prepare --test-key 2026-09-12-SPY --ticker SPY --exchange NY --limit-price 100.00
```

`PREPARED` 상태의 `details.confirmation` 값을 그대로 사용해야 매수 주문이 전송된다.

```bash
python -m scripts.kiwoom_mock_order_test \
  --journal /tmp/kiwoom-overseas-mock-test.sqlite3 \
  buy --test-id TEST_ID --confirm 'BUY:SPY:1@100.00'
```

`BUY_SUBMITTED`이면 증권사 주문번호가 저장된 것이다. 접수와 체결은 다르므로
다음 조회에서 `OPEN_VERIFIED`가 확인되어야 한다.

```bash
python -m scripts.kiwoom_mock_order_test \
  --journal /tmp/kiwoom-overseas-mock-test.sqlite3 \
  verify-open --test-id TEST_ID
```

미체결 확인 뒤 `CANCEL:원주문번호`를 전달한다.

```bash
python -m scripts.kiwoom_mock_order_test \
  --journal /tmp/kiwoom-overseas-mock-test.sqlite3 \
  cancel --test-id TEST_ID --confirm 'CANCEL:123456789'

python -m scripts.kiwoom_mock_order_test \
  --journal /tmp/kiwoom-overseas-mock-test.sqlite3 \
  verify-cancel --test-id TEST_ID
```

최종 `COMPLETE`는 원주문의 `CANCELLED`, 잔여수량 0, 주문가능 현금의 기준값
이상 복구가 모두 확인된 상태다. `CASH_RESTORE_PENDING`이면 주문은 다시 보내지
않고 현금 조회만 나중에 반복한다.

## 키움 API 계약

- 지정가 매수: `POST /api/us/ordr`, `api-id: ust20000`
- 취소: `POST /api/us/ordr`, `api-id: ust20003`
- 취소 요청: `orig_ord_no`, `stex_tp`, `stk_cd`
- 주문 내역: `ust21150`
- 원장 미체결: `ust21050`
- 해외주식 예수금: `ust21110.fc_ord_alowa`

취소 요청 필드는 키움 공식 API 가이드의 `ust20003` 계약을 따른다. 주문가능
현금의 예약금·수수료 의미는 이 테스트 결과로 검증하기 전까지 ETF 자동 실행의
차단 사유로 유지한다.

## 2026-09-15 실연결 결과

해외 전용 모의계좌에서 SPY 1주를 당시 현재가보다 낮은 600 USD 지정가로 접수했다.
유효한 주문번호를 받은 뒤 원장과 주문내역에서 체결 0주·잔여 1주를 확인하고,
해당 원주문만 취소했다. 취소 주문번호도 별도로 발급됐으며 원주문 취소수량 1주,
잔여 0주, 미체결 목록 소멸을 확인했다. 주문가능 현금은 100,000 USD에서
99,394 USD로 감소했다가 취소 후 100,000 USD로 복구됐다.

실제 모의 응답에서 `ord_time`은 KST였지만 `ord_dt` 조회값은 미국 거래일인 전일을
사용했다. 자정 이후 KST 주문 대조는 현재 KST 날짜와 전일 미국 거래일을 모두 조회하고,
주문번호·종목·방향·수량·가격·실제 시각이 일치하는 행만 채택한다. 새 진단 계획의
미체결 사전 검사도 두 날짜를 함께 확인한다.

취소된 원주문은 `ord_stat_nm=무효주문`이면서 `cncl_qty=ord_qty`, `ord_remnq=0`으로
반환됐다. 따라서 명시적인 전량 취소수량과 잔여 0을 거절 문구보다 강한 취소 증거로
판정한다. `cncl_qty=0`인 실제 무효주문은 계속 거절로 분류한다.

이번 결과는 1주 모의 진단의 접수·미체결·취소·현금 복구 검증이다. `ust31490`은
모의 서버에서 계속 미지원이며, 웹/ETF 실행의 `validated_for_execution=false`는 유지한다.

### 체결·매도 왕복과 다종목 예약금

같은 장에서 SPY 1주를 770 USD 매수 지정가로 전송해 761.640 USD에 전량 체결하고,
`ust21070.sell_alowq=1`을 확인한 뒤 750 USD 매도 지정가로 정확히 1주를 전송했다.
매도도 761.640 USD에 전량 체결됐고 이후 매도가능수량은 0주였다. 왕복 전 현금
100,000 USD와 비교해 완료 후 현금은 99,984.767 USD였다. 약 15.233 USD 차이는
이 모의계좌에서 관찰한 매수·매도 비용이며 실계좌 수수료율로 일반화하지 않는다.

다종목은 SPY 600 USD와 QQQ 500 USD를 각각 1주 미체결로 만들었다. 시작 현금
99,984.767 USD에서 SPY 접수 후 99,378.767 USD, QQQ 추가 접수 후
98,873.767 USD로 변했다. 즉 각 지정가의 101%인 606 USD와 505 USD가 순서대로
주문가능 현금에서 빠졌다. 두 주문을 모두 취소한 뒤 미체결 0건과 시작 현금
99,984.767 USD 복구를 확인했다.

현금 응답은 거래 후 소수 셋째 자리까지 반환됐다. 진단기는 주문가격의 센트 단위
검증과 현금 정밀도 검증을 분리했다. 연속 조회의 HTTP 429는 조회에만 제한적으로
재시도하며 주문·취소는 재전송하지 않는다.
