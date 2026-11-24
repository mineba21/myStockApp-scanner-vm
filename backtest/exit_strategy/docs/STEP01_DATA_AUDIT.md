# STEP 01 — Scanner 데이터 감사 (Data Audit)

작성: 2026-11-24 · 근거: 저장소 코드, Scanner MCP(`scanner_signals`, 읽기 전용) 덤프, 로컬 SQLite 사본.
운영 DB(Neon)는 직접 열지 않았다 (`.env` 미열람). 수치 원본: `output/data_audit.json`.

## 1. 결론 요약

| 질문 | 답 |
|---|---|
| 과거 신호가 불변 이력으로 저장되는가? | **아니오.** `scan_results` 1개 테이블, 신호당 1행. 재스캔 시 값 덮어씀. |
| 최초 탐지 시점이 보존되는가? | `first_detected_at` 만 보존 (2026-10 마이그레이션 이후 생성 행). 지표값은 보존되지 않음. |
| 삭제·탈락 이력이 남는가? | 아니오. `DELETE /api/results` 로 지울 수 있고 기록이 남지 않는다. 후보 탈락 여부는 저장되지 않는다. |
| Historical OHLCV 저장소가 있는가? | 없다. 스캔마다 yfinance / FinanceDataReader 로 2년치를 새로 받는다. |
| 기존 백테스트 코드? | 없다. |
| 실제 체결 기록? | 스캐너 신호와 연결된 체결 이력은 없다 (transactions 는 수동 일지). |
| VERIFIED_HISTORICAL 신호 | **0건** |

→ 성과 분석은 **RECONSTRUCTED**(현재 스캐너 코드를 과거 일봉에 날짜별로 재실행) 신호로 하고,
MCP 현재 행(**SNAPSHOT_ONLY**)은 별도로 참고 집계한다. 두 출처를 합산하지 않는다.

## 2. 저장 구조 (코드 확인)

- DB: 운영 Neon(Postgres), 로컬/테스트 SQLite. 스키마는 Alembic 소유 (`database/models.py`).
- `scan_results`: 키 = (ticker, signal_date, signal_type). `scanner/scan_engine.py::_save`
  - 신규: `scan_time = first_detected_at = now`.
  - **기존 행 갱신**: price, ma150, pivot_price, stop_loss, grade, signal_quality, RS, entry 관측값,
    sizing, strict 판정, filter_reasons 를 최신 스캔 값으로 **덮어쓰고** scan_time 을 갱신.
- `scan_logs`: 실행 단위 집계만 (총 스캔 수·신호 수·상태).
- `upthrust_cooldowns`: 돌파 실패 쿨다운만 별도 보존.
- 행에 스캐너 버전 필드 없음 → 과거 행이 어떤 로직으로 판정됐는지 행 단위로 알 수 없다.
- Scanner MCP / 조회 API 는 `strict_filter_passed IS NULL OR TRUE` 행만 노출 (`web/scanner_read_api.py::visible`).
  운영은 `STRICT_PERSIST_REJECTED=true` 로 거부 행도 저장하지만 MCP 로는 보이지 않는다.
- 로컬 `stock_scanner.db` 는 2026-08-19~08-28 의 60행짜리 오래된 사본 (운영 아님).

## 3. 스냅샷(MCP 현재 행) 품질

| 항목 | 값 |
|---|---|
| 가시 행 | 96 (US 76, KR 20) |
| 최대 result_id | 481 → **385개 ID 가 보이지 않음** (strict 거부 행 또는 삭제) |
| 신호일 범위 | 2026-08-13 ~ 2026-11-19 (약 3개월) |
| 고유 종목 / signal_key | 74 / 96 (중복 signal_key 0, 복수 신호 종목 19) |
| 유형 | US RE_BREAKOUT 57 · REBOUND 19 / KR RE_BREAKOUT 12 · REBOUND 8 · **BREAKOUT 0** |
| Grade / Quality | B 76 · A 20 / MODERATE 54 · STRONG 34 · WEAK 8 |
| strict_assessment | passed 96 (legacy_unassessed 0) |
| first_detected_at 없음 | 16 (마이그레이션 이전 행) |
| **최초 저장 후 값이 갱신된 행** | **80 / 80** (first_detected_at 가 있는 행 전부 scan_time > first_detected_at) |
| 탐지 지연 (first_detected − signal_date) | 중앙 0일, 최대 10일 |
| entry_warnings 있는 행 | 57 |
| 결측률 | pivot 28% (REBOUND 는 구조적으로 100%), sector_stage 26%, upthrust_failed 58%, pivot_ext/cur_* 41%, suggested_qty 39% |
| OHLCV 매칭 | 97.9% (KVUE, WBD 조회 실패 — 기업 이벤트로 티커 소멸 추정) |

예: AFL RE_BREAKOUT 2026-11-04 의 운영 pivot 은 124.59 (11-12 재스캔 값),
최초 탐지일 기준 replay 값은 123.99 — 저장값이 최초 판정값이 아님을 보여준다.

## 4. 대안: Replay Scan (RECONSTRUCTED)

- `weinstein.analyze_stock` → `entry_control.annotate_alert_freshness` → `strict_filter.apply_strict_filter` 를
  2021-01-04 이후 **매 거래일** d 에 대해 d 까지의 2년 창(운영과 동일)으로 호출. 시장 상태(BULL/CAUTION/BEAR)도
  같은 날짜 기준으로 SPY·QQQ / KODEX200 에서 재계산.
- signal_key 별 **최초 탐지일**과 **최초 strict 통과일**의 스냅샷을 각각 보존 → 덮어쓰기 문제 없음.
- 운영 `.env` 는 열람하지 않았다. 운영 경고 문구 "주봉 거래량 미달 — 0.73x (돌파 기준 2x)" 로
  `BREAKOUT_WEEKLY_VOL_RATIO=2.0` 을 역확인해 고정 (로컬 기본값 0.5 사용 시 BREAKOUT 이 과다 생성됨).
- **검증**: 운영 가시 행 96건 중 **90건(93.8%) 재현** — US 72/76, KR 18/20.
  재현 행의 pivot·stop 은 **100% ±2% 이내**, 탐지일 일치 65건.
  미재현 6건: US WBD·KVUE(OHLCV 소멸), MMM, EVRG / KR 031510, 042500 (운영 .env 미확인 임계값 추정).
- Replay 규모: US 7,888 신호(strict 통과 2,805, 506 종목) · KR 31,000 신호(strict 통과 3,152).
  KR 연도별 strict 통과: 2021 974 · 2022 13 · 2023 966 · 2024 814 · 2025 348 · 2026 37.
- 한계: 현재 구성종목 유니버스(생존 편향), 수정주가 재계산, 과거 로직 변경 이력 미반영,
  Sector Stage(Gate 2) 과거값 재구성 불가(운영에서도 KR 면제·US 비활성), upthrust 쿨다운 DB 상태 미반영
  (해당 게이트 기본 꺼짐).

## 5. OHLCV

| 시장 | 유니버스 | 확보 | 실패 | 가격 기준 |
|---|---|---|---|---|
| US | 517 (+벤치마크) | 518 | 3 | yfinance `auto_adjust=True` (분할·배당 수정) — 운영과 동일 |
| KR | 2,679 | 2,655 | 25 | FinanceDataReader (분할 수정, 배당 미수정) — 운영과 동일. 거래정지일은 가격 0 행 |

KR 유니버스: 로컬에서 pykrx 시가총액 필터가 `pkg_resources` 부재로 실패해 필터 없이 2,679 종목
(운영 최근 스캔 2,654 종목). 스냅샷 pivot·stop 은 저장 시점 종가 대비 현재 수정주가 비율로 정규화.
