# 검출 결과의 Duquesne 13F 표시

13F는 `/api/results` 응답과 결과 카드의 부가 정보다. 스캔 대상, 기술적 검출,
Strict 통과 여부, 기존 등급, 주문 및 알림 판정에는 관여하지 않는다.
같은 신호 날짜 안에서 정상/legacy 결과를 먼저 두고, NEW +3 / INCREASED +2 /
HELD 0 / DECREASED -1 순으로 표시한다. 이 점수는 검증된 투자 성과 점수가
아닌 화면 정렬값이다. 기술 점수에 더하지 않는다.

화면의 `13F 우선`을 끄거나 API에 `prioritize_13f=false`를 전달하면 기존 순서다.
기존 조회 필터와 limit을 먼저 적용하므로 13F 때문에 응답의 종목이 추가·제거되지 않는다.
다른 대시보드에서도 API의 13F 필드를 사용할 수 있다.

## 필드

| 필드 | 의미 |
|---|---|
| `13F_NEW` / `13F_INCREASED` | 분기 대비 신규 / 보고 주식 수 증가. 미확인일 때 null |
| `13F_WEIGHT` | 비옵션 롱 보유액 대비 분기말 비중, 퍼센트 단위 |
| `13F_FILED_AT` | 시간대가 포함된 SEC 접수/공개 시각 |
| `13F_REPORT_DATE` / `13F_FIRST_TRADABLE` | 보유 기준일 / 공개 후 첫 거래일 |
| `13F_CHANGE_TYPE` | NEW, INCREASED, HELD, DECREASED |
| `13F_PRIORITY` | 표시 정렬값. 미확인/오래된 자료는 0 |
| `13F_STATUS` | AVAILABLE, STALE, NO_MATCH, UNAVAILABLE, NOT_APPLICABLE |
| `13F_CUSIP` / `13F_ACCESSION` / `13F_SOURCE_URL` | 증권 식별자와 SEC 원본 근거 |
| `13F_SHARES` / `13F_PREV_SHARES` / `13F_SHARE_CHANGE_PCT` | 보고 수량과 변화율. 신규의 변화율은 null |

NEW는 역사상 최초 매수가 아니라 직전 분기 미보유/재편입이다. INCREASED는
평가액이 아닌 수량 기준이며 분할/합병을 보정하지 않은 잠정 정보다.
NO_MATCH는 연결된 보유정보가 없다는 뜻으로, 확정적 미보유를 의미하지 않는다.

## 데이터와 시점

기본 파일 `data/duquesne_13f.json`에는 조사에서 확보한 2026 Q2 SEC 원본 기반
스냅샷을 넣었다. 티커 연결은 조사 보조자료를 사용했으며 미연결 종목은 제외한다.
이 초기 버전은 자동 SEC 수집기를 포함하지 않는다. 분기별 검증 자료를 갱신해야 한다.
2026-08-17 이전 신호에는 이 스냅샷을 붙이지 않는다. 신호 날짜와 현재 날짜 모두
공개 후 첫 거래일 이상이어야 한다. 최초 적용일부터 120일이 지난 신호에는
`STALE` 표시를 하고 우선순위를 부여하지 않는다.

환경변수 `FORM13F_DATA_PATH`로 운영 데이터 파일을 지정할 수 있다. 정상 파일을
교체하면 다음 조회에서 반영된다. 파일 누락/오류 시 기존 결과는 그대로 제공한다.
`FORM13F_MAX_AGE_DAYS`, `FORM13F_NEW_PRIORITY`, `FORM13F_INCREASED_PRIORITY`,
`FORM13F_DECREASED_PRIORITY`로 표시 정책을 설정한다.

기존과 동일한 형식의 원본 대조 연구 결과를 가져오는 명령:

```sh
.venv/bin/python scripts/import_13f_research.py ../research/audited_results.json
```

원본 URL·공개일·첫 거래일·티커/CUSIP 연결을 대조한 후 실행한다. JSON 구조 및
수량 변화 일관성은 검증하지만, 가져오기 자체가 SEC 원본의 진위를 검증하지는 않는다.
다음 분기의 조회 결과에서 빠진 종목에 과거 보유정보를 다시 붙이지 않으며, 기존
스냅샷을 보존해 과거 신호에는 그때 이용 가능했던 분기를 선택한다.

로컬 코드와 기본 HTML 결과 화면에 적용되는 변경이다. 운영 서버나 별도 프런트엔드
배포는 별도 작업이며, DB 마이그레이션은 필요하지 않다.
