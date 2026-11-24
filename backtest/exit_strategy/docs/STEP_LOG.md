# STEP 진행 기록

2026-11-24 실행. 실주문·운영 DB 쓰기 없음. 운영 코드 변경 없음 (`backtest/` 하위 신규 파일만).
모든 성과 수치는 **RECONSTRUCTED** 신호 기준이다. VERIFIED_HISTORICAL 0건.

| STEP | 수행 내용 | 산출 파일 | 데이터 수 | 테스트 | 문제·한계 | 다음 |
|---|---|---|---|---|---|---|
| 1 감사 | 저장 구조·덮어쓰기·삭제 경로 코드 확인, MCP 덤프 품질 집계, replay 검증 | `docs/STEP01_DATA_AUDIT.md`, `output/data_audit.json` | MCP 96행 (최대 ID 481) | — | 불변 신호 이력 없음, 갱신 80/80 행 | Replay 로 대체 |
| 2 데이터셋 | 운영 `analyze_stock`+`strict_filter` 를 2021-01-04 이후 매 거래일 재실행, 최초 탐지·최초 strict 통과 스냅샷 보존, 그룹 A–H 구성 | `src/signal_loader.py`, `output/historical_signals.csv` | US 7,935행 / KR 31,187행 (strict 2,805 / 3,152), 스냅샷 96 | 진입 전 미래 데이터 차단, 중복 표시 | 생존 편향, VM .env 미확인(`BREAKOUT_WEEKLY_VOL_RATIO=2.0` 만 역확인) | — |
| 3 시장 데이터 | yfinance(auto_adjust) / FDR 일봉, 운영과 같은 가격 기준, 지표는 t-1 까지·완성 주봉만 | `src/market_data_loader.py`, `src/indicators.py` | US 518/521, KR 2,655/2,680 | 미래 지표·주봉 완성·분할 정규화 | KR pykrx 시총 필터 로컬 실패 | — |
| 4 매도 전략 | S00–S09 (505 설정) + Train 구성요소로 조립한 S09A/B_OPT | `src/exit_rules.py`, `src/stage2.py` | 505 + 4 | 카탈로그 고유성, BF 유형 제한 | — | — |
| 5 체결 | 다음 날 시가 진입, 장중 stop 은 t-1 정보, 종가 신호 → 다음 시가, 갭 체결, 동일 봉 보수/낙관, KR 하한가·정지 | `src/execution_simulator.py` | — | 갭·하한가·정지·동일 봉·부분매도 1회 등 16건 | 일봉만으로 장중 순서 불명 → 보수 기본 | — |
| 6 지표 | 거래·꼬리·MFE/MAE·giveback·사후 추적, 포트폴리오(10종목×10%) | `src/performance_metrics.py`, `src/portfolio_simulator.py` | — | 비용, 미청산 평가, 중복 차단 | 비용은 **가정값** | — |
| 7 검증 | 시간순 60/20/20 + purge, 목표 = 평균 − 0.25·|최악 5% 평균|, 순위 안정성, walk-forward 24/6, block bootstrap | `src/parameter_search.py`, `output/parameter_results.csv` | US Train 1,468 · Val 539 · Test 511 | train/test purge | 후보 비교 판단 기준 2개는 결과 확인 후 추가(아래) | — |
| 8 사례 | CASE A–H 자동 선택 + 캔들 차트 | `src/case_analyzer.py`, `output/case_study.json` | 시장별 최대 16건 | — | CASE G 는 upthrust 판정 대리 지표 | — |
| 9 보고서 | 8탭 단일 HTML, 필터, 라이트/다크 | `output/exit_strategy_backtest_report.html` (11.5MB) | — | 브라우저 렌더링·콘솔 오류 0 확인 | — | — |
| 10 CLI | `run.py audit|fetch|replay|backtest|stage2|report|all` | `run.py`, `README.md` | — | — | KR replay ≈ 2시간 | — |
| 12 권장 규칙 | 시장×유형별 상태 판정 | `output/recommended_exit_rules.json` | 6 규칙 | — | 최고 상태도 PAPER(모의) | 모의 검증 |
| 13 테스트 | `backtest/exit_strategy/tests` 26건 + 기존 저장소 999건 | — | — | **1,025 passed** | — | — |

## 실행 중 발생한 문제와 조치

1. **KR 백테스트 swap 고갈 (exit 137)** — fork 워커가 3만 건 경로 리스트 전체를 순회해 copy-on-write 로
   워커마다 사본(≈2.4GB×8) 발생. → 거래를 3,000건 chunk 로 나눠 워커가 chunk 객체만 건드리게 하고,
   결과를 작업별 float32 DataFrame 으로 반환. Stage 2 는 60일 기본 슬라이스만 받도록 변경하고
   `run.py stage2` 재개 명령 추가. 최대 RSS 2.5GB, 전체 시뮬레이션 5분.
2. **운영 테스트 오염** — 백테스트 테스트의 환경변수 격리가 같은 pytest 세션의 운영 테스트 1건을 실패시킴.
   → upthrust 판정 import 에서 격리 제거(entry_control 은 pandas 만 사용), `tests/conftest.py` 에서
   환경변수 복원. 전체 1,025 통과.
3. **trade_results.csv 309MB** → 핵심 전략·strict(+S00/S01 전체) 행과 필요한 열만 남겨 124MB.

## 결과 확인 후 추가한 판정 기준 (투명성)

결과를 본 뒤 `recommendation.py` 상태 판정에 두 조건을 추가했다. 선택(Validation)과 Test 수치 자체는 바꾸지 않았다.

- **기대수익 허용폭**: 선택 전략의 거래당 평균이 S01 보다 0.5%p 넘게 낮으면 권장하지 않는다
  (US RE_BREAKOUT S02_fix7: 손실 꼬리 20.7%→1.2% 이지만 Test 평균 −0.87%p → NOT_RECOMMENDED, 교환 관계 메모).
- **전체 기간 쌍대 bootstrap**: S01 대비 쌍대 차이 중앙값이 −0.5%p 미만이면 NEED_MORE_DATA
  (KR BREAKOUT S09A_OPT: Test 41건에서는 개선, 전체 기간 중앙값 −0.55%p · 개선 확률 26%).

두 조건 모두 권장을 **줄이는** 방향이다.
