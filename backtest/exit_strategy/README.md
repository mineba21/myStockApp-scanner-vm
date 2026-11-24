# Exit Strategy Backtest

운영 스캐너의 **매수 신호는 그대로 두고** 매도 규칙(S00~S09)을 비교하는 연구용 백테스트다.
운영 코드·운영 DB·주문 경로와 분리되어 있다.

- 실제 주문 없음. 운영 DB 연결 없음 (Scanner MCP 읽기 덤프만 사용).
- 스캐너 모듈(`weinstein`, `entry_control`, `strict_filter`)은 `DATABASE_URL` 을 임시 SQLite 로 고정하고
  주문·알림 스위치를 끈 상태에서만 import 한다 (`src/common.py::isolate_scanner_imports`).

## 실행

```bash
# 1) 과거 일봉 확보 (US: yfinance 수정주가, KR: FinanceDataReader) → data/cache/*.pkl
.venv/bin/python -m backtest.exit_strategy.run fetch --market US
.venv/bin/python -m backtest.exit_strategy.run fetch --market KR

# 2) Replay scan: 현재 스캐너 로직을 2021-01 이후 매 거래일에 적용해 신호 재생성 (8코어 기준 US ~15분, KR ~1시간)
.venv/bin/python -m backtest.exit_strategy.run replay --market US
.venv/bin/python -m backtest.exit_strategy.run replay --market KR

# 3) 백테스트 · 선택 · 검증 · 포트폴리오 · 사례 · 권장 규칙
.venv/bin/python -m backtest.exit_strategy.run backtest
.venv/bin/python -m backtest.exit_strategy.run stage2     # (선택) 저장된 Stage 1 결과로 Hybrid 조립·권장 규칙만 재계산

# 4) 데이터 감사 · HTML 보고서
.venv/bin/python -m backtest.exit_strategy.run audit
.venv/bin/python -m backtest.exit_strategy.run report

# 테스트
.venv/bin/python -m pytest backtest/exit_strategy/tests -q
```

`all` 은 위 단계를 순서대로 실행한다. 스냅샷 신호는 Scanner MCP `scanner_signals(after=2026-08-01)` 응답을
`data/raw/page1.json` 으로 저장해 쓴다 (운영 DB 직접 접근 없음).

## 구조

| 파일 | 역할 |
|---|---|
| `configs/exit_backtest.yaml` | 기간·비용(가정값 표시)·포트폴리오·검증 설정, 운영에서 역확인한 스캐너 임계값 고정 |
| `src/data_audit.py` | 저장 구조 사실(코드 근거) · 스냅샷 품질 집계 |
| `src/signal_loader.py` | SNAPSHOT_ONLY 적재, RECONSTRUCTED replay (병렬) |
| `src/market_data_loader.py` | OHLCV 확보·캐시·실패 사유 기록 |
| `src/indicators.py` | MA10/21/50/150/200, ATR14, 직전 20일 거래량, RS 약화, 완료 주봉 MA30W·Stage |
| `src/entry_builder.py` | 탐지 다음 거래일 시가 진입, 스냅샷 수정주가 정규화, upthrust D+N 판정 |
| `src/exit_rules.py` | S00~S09 규칙·파라미터 grid (505개 설정) |
| `src/execution_simulator.py` | 보수적 일봉 체결 (갭, 다음날 시가, 전일 기준 trailing, 동일 봉 불확실성, 정지·하한가) |
| `src/backtest_engine.py` | 병렬 실행 오케스트레이션 |
| `src/analysis.py`, `src/stage2.py` | 집계, 시간순 분리+purge, family 선택, Hybrid 조립, Validation 선택, Test 평가, walk-forward, bootstrap, 포트폴리오 |
| `src/portfolio_simulator.py` | 시장별 자금 제약 포트폴리오 (동일 종목 중복 금지) |
| `src/performance_metrics.py` | 거래 단위 / 포트폴리오 단위 지표 분리 |
| `src/parameter_search.py` | split · purge · objective · walk-forward · block bootstrap |
| `src/case_analyzer.py` | CASE A~H 자동 선정 + 차트 데이터 |
| `src/recommendation.py` | 권장 규칙 JSON (`RECOMMENDED_FOR_PAPER` / `NEED_MORE_DATA` / `NOT_RECOMMENDED`) |
| `src/report_generator.py` + `report_template.html` | 단일 파일 Interactive HTML |

## 산출물 (`output/`)

`data_audit.json`, `historical_signals.csv`, `trade_results.csv`(핵심 전략), `daily_equity.csv`,
`strategy_summary.csv`, `parameter_results.csv`, `exit_reason_summary.csv`, `portfolio_summary.csv`,
`case_study.json`, `recommended_exit_rules.json`, `exit_strategy_backtest_report.html`

## 데이터 품질 원칙

| 라벨 | 의미 | 이번 데이터 |
|---|---|---|
| VERIFIED_HISTORICAL | 당시 기록이 검증된 신호 | 0건 — 불변 이벤트 이력 없음 |
| SNAPSHOT_ONLY | MCP 현재 행 (재스캔 시 값 덮어씀, 삭제 가능, strict 거부 행 비노출) | 참고용 별도 집계 |
| RECONSTRUCTED | 현재 스캐너 로직 replay | 주 분석 대상 |

세 출처를 하나의 확정 성과로 합산하지 않는다. 자세한 내용은 `docs/STEP01_DATA_AUDIT.md`, `docs/STEP_LOG.md`.
