# 키움 REST API 실계좌 잔고 조회 전용 설정

이 프로젝트의 `trading.kiwoom_readonly` 모듈은 다음 세 요청만 구현한다.

- OAuth 접근 토큰 발급: `POST /oauth2/token` (`au10001`)
- 국내주식 계좌평가잔고 조회: `POST /api/dostk/acnt` (`kt00018`)
- 미국주식 원장잔고 조회: `POST /api/us/acnt` (`ust21070`)

주문·정정·취소 메서드와 임의 API 경로 호출 기능은 포함하지 않는다.

## 1. 키움에서 REST API 신청

1. [키움 REST API 포털](https://openapi.kiwoom.com/)에 로그인한다.
2. REST API 사용을 신청한다.
3. Oracle VM의 **고정 공인 IPv4**를 허용 IP로 등록한다. VM 내부 사설 IP가 아니다.
4. 조회할 실계좌를 등록하고 SMS 인증을 마친다.
5. `계좌 APP KEY 관리`에서 실전투자 App Key와 App Secret을 다운로드한다.

실전투자와 모의투자의 키는 서로 다르다. App Key와 App Secret 다운로드는 1회만 가능하므로 Oracle VM의 비밀 저장소에 옮긴 뒤 원본 파일을 안전하게 보관한다.

## 2. Oracle VM 환경변수 설정

프로젝트의 `.env` 파일에 아래 값을 넣는다. `.env`는 Git에서 제외되어 있지만 파일 권한도 제한한다.

```bash
KIWOOM_MODE=real
KIWOOM_APP_KEY=발급받은_실전_APP_KEY
KIWOOM_APP_SECRET=발급받은_실전_APP_SECRET
KIWOOM_TIMEOUT_SECONDS=15
```

```bash
chmod 600 .env
```

계좌번호는 요청 본문에 넣지 않는다. 포털에서 App Key에 등록·연결한 계좌의 잔고가 조회된다.

## 3. 토큰 발급 확인

프로젝트의 가상환경과 의존성이 준비된 상태에서 실행한다.

```bash
python3 -m trading.kiwoom_readonly --token-only
```

성공하면 토큰 원문은 노출하지 않고 마스킹된 값과 만료시각만 출력한다. 접근 토큰은 공식 문서 기준 24시간 유효하다.

## 4. 실제 계좌 잔고 조회

한국거래소 합산 잔고 조회:

```bash
python3 -m trading.kiwoom_readonly
```

NXT 잔고 조회:

```bash
python3 -m trading.kiwoom_readonly --exchange NXT
```

출력의 `summary`에는 총매입금액·총평가금액·총평가손익·추정예탁자산 등이, `holdings`에는 종목별 보유수량·평가금액·수익률 등이 키움 원본 필드명으로 표시된다. 연속조회 응답이 있으면 최대 10페이지까지 자동으로 합친다.

## 5. 운영 점검

- 반드시 먼저 모의 App Key와 `KIWOOM_MODE=mock`으로 연결을 점검한다.
- 실전 전환 시 `.env`의 키와 모드를 함께 바꾼다. 서로 다른 환경의 키를 섞으면 인증되지 않는다.
- 인증 실패 시 Oracle VM의 현재 공인 IP가 포털 허용 IP와 같은지 먼저 확인한다.
- 키나 토큰을 로그, Slack, Git, 터미널 캡처에 남기지 않는다.
- 이 모듈에 주문 API를 추가하지 말고, 필요 시 별도 모듈·별도 키·별도 승인 절차로 격리한다.

여기서 "조회 전용"은 이 Python 모듈이 주문 경로를 전혀 제공하지 않는다는 뜻이다. App Key 자체에 키움 서버가 별도의 조회 전용 권한을 부여한다는 의미는 아니므로, 키가 유출되지 않도록 실계좌 자격 증명으로 취급해야 한다.

## 6. WEB 보유현황 연결

다계좌 프로필 파일이 준비된 서버에서 다음 값을 설정하면 `/api/holdings`가 기존 수동 보유분과 키움 실계좌 보유분을 함께 반환한다.

```bash
KIWOOM_WEB_ENABLED=true
KIWOOM_BALANCE_CACHE_SECONDS=60
```

키움 행은 `source=kiwoom`, `read_only=true`로 제공된다. 국내주식은 `market=KR`, 미국주식은 `market=US`로 각각 표시된다. 미국주식 조회는 거래소를 비워 나스닥·뉴욕·아멕스 잔고를 함께 가져온다. WEB에서는 `키움 실계좌` 출처 필터로 따로 볼 수 있으며 편집·삭제 버튼은 표시하지 않는다. App Key, Secret, OAuth 토큰, 실제 계좌번호는 API 응답과 브라우저로 전달하지 않는다. 키움 장애가 발생해도 기존 수동 보유현황은 계속 반환된다.

국내전용 계좌가 미국주식 API에서 `508540`(해외증권주문 가능 계좌 아님)을 반환하면 해당 계좌의 해외 잔고만 건너뛰고 국내 잔고는 계속 표시한다.
