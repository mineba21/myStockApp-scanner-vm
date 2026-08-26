# 재리뷰 — `2cebf36 fix: address point-in-time scanner review`

**범위:** `git diff 32e8d3d..2cebf36` (13개 파일, +714/−151)
**기준선 비교:** `06f1237` (브랜치 분기점, 1단계 이전 상태)

**검증 방법:** `apscheduler`, `httpx` 설치 후 전체 테스트 **241개 전부 통과**.
아래 항목 중 1·2·3번은 합성 데이터로 직접 재현했고, 성능 수치는 실측값입니다.

> 참고: 직전 리뷰 문서는 `32e8d3d`(수정 이전 커밋)를 대상으로 작성되어 내용이
> 실제 코드와 맞지 않았습니다. 이 문서가 이를 대체합니다.

---

## 잘 고쳐진 부분 (먼저)

직전 리뷰의 주요 지적 대부분이 제대로 해결됐습니다.

| 항목 | 결과 |
|---|---|
| PIT 루프 성능 | **75.4ms → 24.0ms** (종목당, 실측). `_prepare_daily_indicators` + `weekly_cache`로 rolling·리샘플 재계산 제거 |
| `include_latest` no-op | `_scan_offsets()`로 통일, `abs_i = n-1-i`로 인덱싱 정상화. 기본값을 `True`로 뒤집어 기존 동작과 등가 유지 |
| 신호 유형 우선순위 | 감지기 바깥/후보 안쪽 이중 루프로 "유형 우선 → 최신 우선" 계약 복원 |
| naive `as_of` | `_as_utc()`가 tz 없는 값을 `ValueError`로 거부 — 6단계 백테스트 재현성 확보 |
| 데이터 신선도 | `_freshness_metadata()` + `staleness_sessions` / `last_bar_date` 도입 |
| 보유종목 현재가 | `_fetch_position_frames()`가 전략용 확정봉과 장중 관측가를 분리, `check_sell_signal(current_price=)` 추가 |
| `market_analysis` 캐시 | `(market, session_date, strategy_version)` 키의 `_context_cache` 도입 |
| `us_stocks`/`kr_stocks` 길이 가드 | 정규화 **후** 재검사로 두 경로 계약 일치 |

`_detect_signal_point_in_time`의 `weekly_cache` 슬라이싱(`weekly_all.index <= week_label`)은
검토 결과 정확합니다 — 잘린 구간은 항상 완결된 주이며, `completed_week_label()`이
`session_date`와 `market`만 사용하므로 `for_session()` → `dataclasses.replace()` 치환도 등가입니다.

---

## 1. (심각) 거래량 게이트 AND→OR 변경이 **legacy 경로까지** 바꿨습니다

**위치:** `scanner/weinstein.py:526` (`detect_stage2_breakout`), `scanner/strict_filter.py:284` (`_check_volume`)

직전 리뷰에서 "월~목 BREAKOUT이 막힌다"고 지적한 문제를, 주봉 거래량 hard block을
**일봉 OR 주봉**으로 바꿔 해결했습니다. 문제는 이 분기에 `scan_context` 조건이 없어서
**`scan_context=None`인 legacy 경로에도 그대로 적용**된다는 점입니다.

동일 데이터(월요일 돌파, 일봉 거래량 5배, 주봉 비율 1.0)로 `analyze_stock()`을
`scan_context` 없이 호출한 결과:

```
06f1237 (기준선)   weekly_volume_ratio=1.0  →  analyze_stock: None
2cebf36 (현재)     weekly_volume_ratio=1.0  →  analyze_stock: ('BREAKOUT', '2026-08-24', dvr=5.00)
```

기준선이 거부하던 신호를 지금은 BREAKOUT으로 내보냅니다. 이것은:

- **1단계 완료 조건 위반** — "현재 스캐너 출력이 변경되지 않는다"
- **2단계 완료 조건 위반** — "매수 규칙 자체는 유지"
- **3단계 명세의 선취** — `일봉 3배 OR 완성 주봉 2배`는 3단계 `weinstein_breakout_v1` 규칙입니다
- **롤백 불능** — `WEINSTEIN_V1_MODE=off` / `STRATEGY_VERSION=legacy_v4`로 되돌려도
  기준선 동작이 복원되지 않습니다. 7단계의 "설정만으로 즉시 복귀" 요건이 지금 깨져 있습니다.

`strict_filter._check_volume`도 같은 방식으로 완화되어 `_grade`(S/A/B)와 CORE 판정까지
기준선과 달라집니다.

**권장 조치**
`scan_context`(또는 `WEINSTEIN_V1_MODE`)로 분기해서 legacy는 AND, 신규 엔진만 OR를
쓰도록 하십시오. 월~목 BREAKOUT 억제 문제는 OR로 우회하지 말고, PIT 경로에서
평가 대상 주(週)의 **진행 중 주봉 거래량을 별도로 계산**해서 푸는 것이 2단계 취지에 맞습니다
(2단계는 "평균에서 평가 봉 제외"이지 "서지 봉 자체를 제외"가 아닙니다).

---

## 2. (심각) 09:00 KST 스캔에서 KR 보유종목이 전부 `CHECK_FAILED`가 됩니다

**위치:** `scanner/scan_engine.py:396` (`_fetch_position_frames`), `scanner/scan_engine.py:482` (`_check_holdings`)

`current_price`는 `quote_date >= last_started_session(market, as_of)`일 때만 인정되고,
아니면 `None` → `_check_holdings`가 `ValueError`를 던져 해당 종목의 모든 보유행이
`CHECK_FAILED` / "가격 데이터를 확인하지 못했습니다"로 기록됩니다.

`last_started_session`을 실제로 호출해 보면:

```
09:00 KST  →  started = 2026-08-25 (당일)   completed = 2026-08-24
09:01 KST  →  started = 2026-08-25 (당일)
14:00 KST  →  started = 2026-08-25 (당일)
22:00 KST  →  started = 2026-08-25 (당일)
```

`cal.session_open(session) > utc_as_of` 비교가 `>`(초과)라서 **09:00:00 정각에는
당일 세션이 이미 "시작됨"으로 판정**됩니다. 그런데 그 시각은 KRX 시가 단일가가 막
체결된 순간이라 어떤 공급자도 당일 일봉을 아직 내주지 않습니다.

스케줄러는 `KST 09:00 / 14:00 / 22:00`로 돌아가므로 **매일 아침 스캔은 KR 보유종목
전량을 CHECK_FAILED로 만들고**, `current_price` / `price_updated_at`도 갱신되지 않습니다.
`test_missing_intraday_quote_does_not_overwrite_price_as_current`가 이 동작을 고정하고
있어 테스트로는 잡히지 않습니다.

**권장 조치**
장 시작 직후 유예를 두십시오. 예를 들어 `last_started_session`에
`session_open + N분` 기준을 적용하거나, `quote_date >= last_completed_session`을
하한으로 삼고 당일 봉은 있으면 쓰는 방식(있으면 장중가, 없으면 전일 종가 + `is_intraday=False`
플래그)으로 바꾸면 됩니다. 지금처럼 예외를 던지면 매도 판정 자체가 스킵되어
**장 초반 손절 도달을 놓칩니다.**

---

## 3. (심각) `WEINSTEIN_V1_MODE=` 공백값이 여전히 import 시점에 죽습니다 — 미반영

**위치:** `config.py:15`

직전 리뷰에서 지적했으나 이번 커밋에 포함되지 않았습니다.

```python
STRATEGY_VERSION   = os.getenv("STRATEGY_VERSION", "legacy_v4").strip() or "legacy_v4"   # ← fallback 있음
WEINSTEIN_V1_MODE  = os.getenv("WEINSTEIN_V1_MODE", "off").strip().lower()               # ← 없음

if WEINSTEIN_V1_MODE not in {"off", "shadow", "primary"}:
    raise ValueError(...)
```

재현 확인:

```
$ WEINSTEIN_V1_MODE="" python -c "import config"
ValueError: WEINSTEIN_V1_MODE must be one of: off, shadow, primary
```

`.env`에 `WEINSTEIN_V1_MODE=`(값 없이) 한 줄만 있어도 웹 앱·스케줄러·스캐너가 전부
기동 실패합니다. 윗줄과 동일하게 `or "off"`를 붙이면 끝입니다.

---

## 4. (중요) 벤치마크가 STALE이면 시장 전체 RS가 사라집니다

**위치:** `scanner/market_analysis.py:132` (`get_benchmark_close`)

`data_status != "FINAL"`이면 경고 로그 후 `None`을 반환합니다. 그러면 해당 시장의
**모든 종목**이 `rs_value=None` → `strict_filter._check_rs`에서 `rs_benchmark_missing`
(`strict_filter.py:322`) → 전량 탈락합니다.

즉 벤치마크 ETF(`069500` / `SPY`) 하나가 하루 늦으면 그 시장 스캔에서 CORE 후보가
0건이 됩니다. 기존에는 (오래됐더라도) 값이 있어서 이런 전면 중단은 없었습니다.

**권장 조치** — 마지막 확정 봉까지의 시리즈를 그대로 쓰되 `benchmark_stale` 경고 플래그를
신호에 실어 보내는 편이 낫습니다. 차단은 하더라도 종목 단위가 아니라 스캔 단위로
명시적으로 보고되어야 합니다.

---

## 5. (중요) 지수가 STALE이면 조용히 빠져서 `market_condition`이 왜곡됩니다 (fail-open)

**위치:** `scanner/market_analysis.py:158` (`_analyze_index`), `scanner/market_analysis.py:188` (`_condition`)

`_analyze_index`는 STALE이면 **로그 없이** `return`합니다. 그 결과:

- 지수 3개 중 2개가 STALE이고 남은 1개가 STAGE4면 → `all(s == "STAGE4")` 성립 →
  **지수 하나로 시장 전체를 `BEAR`로 판정**합니다.
- 전부 STALE이면 `_condition([])` → `"UNKNOWN"` → `analyze_stock`의
  `market_condition == "BEAR"` Stage4 차단(`weinstein.py:1218`)과 scan_engine의
  BEAR 필터가 **둘 다 무력화**됩니다.

커밋 전반이 fail-closed 기조인데 이 지점만 fail-open입니다. `get_benchmark_close`는
경고를 남기는데 여기는 남기지 않아 일관성도 없습니다.

부수적으로, `cache_complete`가 "전 종목 성공"을 요구하므로 섹터 ETF 하나만 STALE이어도
`_context_cache`에 영원히 적재되지 않아 매 스캔 12종목을 다시 받습니다.

---

## 6. (중요) STALE 종목이 `None`으로 사라져 `INSUFFICIENT_DATA` 계측이 없습니다

**위치:** `scanner/weinstein.py:1201` (`analyze_stock`), `scanner/weinstein.py:1459` (`check_sell_signal`)

```python
if df.attrs.get("data_status") != "FINAL":
    return None
```

반환값이 "신호 없음"과 구분되지 않습니다. 2단계 명세는 *"데이터 부족을 STAGE1로 대체하지
않고 `INSUFFICIENT_DATA`로 반환한다"*이고, 7단계 운영 지표는 *"데이터 신선도"*를 요구합니다.
지금은 `_freshness_metadata()`가 상태를 계산해 놓고도 스캔 레벨에서 버려서, 몇 종목이
왜 빠졌는지 알 방법이 없습니다. 거래정지 종목이 조용히 사라져도 아무도 모릅니다.

최소한 `_scan_kr` / `_scan_us`에서 드롭 카운터와 티커 목록을 집계해 스캔 결과에 남기십시오.

---

## 7. (경미) 이전 리뷰 미반영 — `weekly_df` 이중 계산

**위치:** `scanner/weinstein.py:1457`

```python
weekly_df = to_weekly_ohlcv(df, scan_context)   # 인자로 받은 weekly_df를 무조건 덮어씀
```

`_check_watchlist`(`scan_engine.py:421`)와 `_check_holdings`(`scan_engine.py:487`)가 계산해
넘긴 `weekly_df`가 그대로 버려집니다. 게다가 `_fetch_position_frames`가 이미 정규화한
`df`를 `check_sell_signal`이 `normalize_ohlcv`로 **다시** 정규화합니다. 보유·감시 종목당
리샘플 2회 + 정규화 2회가 중복됩니다.

## 8. (경미) 감시목록과 보유종목의 STALE 처리가 비대칭입니다

`_check_holdings`는 `current_price is None`이면 예외를 던지는데,
`_check_watchlist`(`scan_engine.py:434`)는 `None`을 그대로 넘기고
`check_sell_signal`이 조용히 확정 종가로 대체합니다(`weinstein.py:1469`).
같은 상황에서 한쪽은 실패로, 한쪽은 전일 종가 기준 손절 알림으로 처리됩니다.

## 9. (경미) `ScanContext.for_session()`이 죽은 코드가 됐습니다

**위치:** `scanner/time_context.py:147` — 유일한 호출부였던 `_detect_signal_point_in_time`이
`dataclasses.replace`로 바뀌면서 호출부가 사라졌습니다(테스트 포함 0건).
6단계 백테스트에서 쓸 계획이면 주석으로 의도를 남기고, 아니면 제거하십시오.

---

## 진행 원칙 관련

1·2단계가 여전히 **한 커밋**(`32e8d3d`) + 수정 커밋(`2cebf36`)으로 남아 있고,
"legacy 동작 무변경이 검증된 시점"에 해당하는 커밋이 없습니다. 1단계 롤백 지점이
존재하지 않는 상태이며, **1번 항목 때문에 지금은 코드로도 기준선 복원이 안 됩니다.**

1단계 산출물 중 다음은 아직 확인되지 않습니다.
- PostgreSQL 마이그레이션 조기 종료 등 구조적 결함 **기록**
- 기존 출력 보존용 KR·US 합성 fixture (`tests/test_phase1_baseline.py`는 달력일 기준
  인덱스를 써서 `normalize_ohlcv`를 통과하지 못하고, 이번 diff가 실제로 바꾼 필드를
  검증하지 않습니다)

---

## 정리 — 3단계 진행 전 처리 순서

1. **1번** — legacy 거래량 게이트 원복 (기준선 동작 복원, 롤백 가능성 회복)
2. **3번** — `config.py` 한 줄 (`or "off"`)
3. **2번** — 09:00 KST 유예 처리
4. **4·5번** — 벤치마크·지수 STALE 시 fail-open/전면차단 정리
5. **6번** — `INSUFFICIENT_DATA` 계측 노출
6. 7·8·9번은 3단계 작업 중 함께 정리 가능
