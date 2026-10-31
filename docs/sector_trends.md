# 종목별 섹터와 최근 추세

## 조사한 현재 구현 (main 0e36a43)
- `us_stocks.get_sp500_tickers`: Wikipedia S&P500 GICS Sector → 유니버스 메타데이터.
- `scan_engine._evaluate_strict_filter`: GICS → SPDR 11섹터 ETF의 주봉 Stage → Strict Gate 2.
- `market_analysis`: yfinance/FDR/pykrx 가격, 프로세스별 60분 지연 갱신 캐시.
- SQLAlchemy `ScanResult.sector_name/sector_stage`, Alembic 스키마 관리, FastAPI `/api/results`, Alpine/Tailwind 카드.
- 국내는 분류 미구현. 기존 config의 KRX-DESC Industry 설명과 달리 현재 FDR 소스에서 Sector=업종, Industry=주요제품이다.

## 이번 설계 선택 (투자 전략 검증 결과가 아님)
- 최근 추세 v1: 최근 20거래일 종가 수익률. +5% 이상 STRONG_UP(강한 상승), +1% 이상 UP(상승), -1% 초과 +1% 미만 FLAT(횡보), -5% 초과 -1% 이하 DOWN(하락), -5% 이하 STRONG_DOWN(강한 하락). 반올림 전 판정.
- 미국: 기존 GICS 우선. 분류 없는 **신호 종목만** Yahoo get_info sector를 GICS와 명시적으로 대응시켜 표시용으로 보완. 기존 Gate 2 입력/토글/Stage에는 반영하지 않는다. 성과는 해당 SPDR ETF의 auto_adjust 종가(대용치, 전체 섹터 수익률 아님).
- 국내: FDR KRX-DESC의 Sector 원문. 이번에 선택한 스캔 유니버스(시총/가격 필터 후)의 동일 업종 종목만 사용. 수집 가격을 재사용해 추가 가격 조회 없이 종목별 20거래일 수익률의 동일가중 평균을 구한다. 공식 업종지수/GICS와 다르므로 UI에 '스캔 업종 평균', 표본 수/분모/기준일을 표시한다. 유니버스 선택에 따라 값이 달라진다.
- 국내 표본 최소 3종목, 분류된 업종 내 대상의 70% 이상, 전체 수집 가격의 최신 거래일과 동일한 21개 거래일 가격이 있어야 한다. 결측/0/음수/무한/중복 날짜와 미래 날짜는 제외하며 forward-fill하지 않는다.
- 최근 가격이 달력일 7일 초과 과거이면 STALE, 이력 부족 INSUFFICIENT_DATA, 가격 조회 실패 UNAVAILABLE, 분류 없음 UNMAPPED. 어떤 경우에도 횡보로 대체하지 않는다. 긴 휴장 중에도 7일 컷은 보수적으로 적용한다.
- 분류 성공 캐시 24시간, 실패 캐시 60분(프로세스별). 미국 ETF는 기존 60분 캐시. 국내 추세는 기존 정규장 마감 후/수동 스캔마다 계산. 조회 API는 외부 호출 없이 저장된 스캔 시점 snapshot을 반환한다. 최초 조회/재시작 때 지연 수집한다.
- `sector_context` nullable JSON text에 버전, 이름, 분류 소스, 성과 소스/대용치, 상태, 기준일, 20일 수익률, 추세, 표본을 함께 저장한다. 기존 행은 NULL, 다음 재스캔에서 갱신되며 과거 성과를 임의 역산하지 않는다.
- 기존 sector_stage는 주봉 Strict 판정용으로 유지한다. 새 단기 추세는 표시만 하며 매수/매도/알림/계좌 권한을 바꾸지 않는다.

## 소스 확인
- https://github.com/FinanceData/FinanceDataReader/blob/master/src/FinanceDataReader/krx/listing.py (`업종`→`Sector`, `주요제품`→`Industry`)
- https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.get_info.html
- 기존 저장소 `scanner/market_analysis.py`의 11개 GICS→ETF 대응표를 재사용.

## 배포 및 한계
앱의 기존 `init_db`가 Alembic upgrade head를 실행한다. nullable 컬럼 하나를 추가하며 기존 행은 보존한다. 운영 배포/실제 공급자 가용성과 데이터 품질은 별도 확인이 필요하다. 테스트에는 합성/모의 공급자와 임시 SQLite만 사용한다. Yahoo 보완 분류는 Wikipedia GICS와 동등한 공식 GICS 데이터라고 주장하지 않는다. 캐시는 프로세스 간 공유하지 않으며 신규 공급자 호출 실패는 해당 표시만 미확인으로 처리한다.

## 검증 결과 (2026-10-31)
- Python 3.11, 저장소 requirements 기반 격리 환경: `python -m pytest tests/ -q` → **896 passed**, 기존 의존성 경고 10건.
- 경계값 5단계, 21개 종가/20거래일, 날짜 정렬·중복·미래·결측·0·음수·무한값, 7일 초과 오래된 가격, 국내 최소 표본/커버리지/동일 날짜 정렬을 모의 데이터로 검증.
- 공급자 분류 컬럼·캐시 성공/실패 TTL, 미국 유니버스 24시간 갱신, KR/US 스캔 주입, 기존 Gate 2 불변, snapshot 신규 저장·재스캔 갱신·legacy NULL API 응답을 검증.
- 기존 Alembic 전체 마이그레이션과 SQLAlchemy 모델 스키마 일치 테스트 통과(SQLite). 실제 운영 PostgreSQL에서 migration을 실행한 것은 아니다.
- Playwright 실제 브라우저에서 저장소 템플릿 + 모의 API로 5단계/미분류/미확인 표시와 tooltip을 확인. 데스크톱 및 390px 모바일에서 줄바꿈·기준일·표본 표시 확인, 콘솔 오류 0개(기존 Tailwind CDN 개발용 경고만 있음).
- 운영 DB, 실주문, 실제 시장 스캔, 공급자 실제 응답 품질은 테스트하지 않았다. 기본 브랜치/운영 배포는 변경하지 않는다.

가격은 기존 공급자의 Close를 그대로 사용한다. 미국 ETF는 기존 auto_adjust=True이며 국내는 기존 FDR/pykrx 조정 정책을 따른다. 수동 장중 스캔은 미완료 당일 봉을 포함할 수 있다. snapshot은 스캔 당시 값으로 유지되며 API 조회 시 최신 추세로 다시 계산하지 않는다. 국내는 업종 평균을 위해 가격/분석 수집 후 저장·필터 처리를 진행하므로 개별 카드 저장 시점이 스캔 후반으로 이동한다.
