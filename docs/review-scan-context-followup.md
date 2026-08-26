# 재리뷰 — `c4cccd2 fix: preserve legacy scanner contracts`

**범위:** `git diff 2cebf36..c4cccd2` (13개 파일, +565/−83)
**기준선 비교:** `06f1237` (브랜치 분기점)

**검증:** 전체 테스트 **251개 통과**. legacy 회귀 여부는 `06f1237`과 `c4cccd2`를
각각 worktree로 띄워 동일 합성 데이터로 대조했고, `quote_status` 판정은
실제 스케줄 시각(09:00/14:00/22:00 KST)으로 시뮬레이션했습니다.

---

## 직전 지적 9건 처리 결과

| # | 항목 | 결과 |
|---|---|---|
| 1 | 거래량 게이트 AND→OR가 legacy까지 변경 | **해결 (실측 확인)** |
| 2 | 09:00 KST 스캔에서 KR 보유 전량 `CHECK_FAILED` | **해결 (실측 확인)** |
| 3 | `WEINSTEIN_V1_MODE=` 공백값 `ValueError` | **해결** (`or "off"`) |
| 4 | 벤치마크 STALE → 시장 전체 RS 소실 | **해결** (시리즈 유지 + `BENCHMARK_STALE` 경고) |
| 5 | 지수 STALE 시 조용히 drop (fail-open) | **방향은 해결**, 단 아래 A항 참조 |
| 6 | `INSUFFICIENT_DATA` 계측 부재 | **절반만 해결** — 아래 B항 참조 |
| 7 | `weekly_df` 이중 계산 | **해결** (`_matches_scan_context`) |
| 8 | 감시목록/보유종목 STALE 처리 비대칭 | **해결** (양쪽 `quote_status` 전달) |
| 9 | `for_session()` 죽은 코드 | **해결** (제거) |

### 1번 — legacy 파리티 실측

`use_volume_or`를 `strategy_version == "weinstein_breakout_v1"`로 분기하고,
legacy AND 게이트에는 평가 봉이 속한 **진행 주의 누적 거래량**(`progressing_week_volume_ratio`)을
따로 계산해 넘기는 방식입니다. 분모는 평가 주 **이전**의 완료 주만 쓰므로 look-ahead도 없습니다.
`compute_weekly_indicators`의 `vol.shift(1).rolling(10, min_periods=5)`와
`iloc[-10:]` + `len >= 5` 가드가 정확히 대응합니다.

동일 데이터 4종으로 대조한 결과 세 경로가 완전히 일치합니다.

```
                                  06f1237      c4cccd2      c4cccd2
                                  legacy       legacy       PIT
Mon 돌파, 일봉 5배, 주봉 약함      None         None         None      (rule=AND)
Thu 돌파, 일봉 5배, 주간 3배       BREAKOUT     BREAKOUT     BREAKOUT  (rule=AND)
Fri 돌파, 일봉 5배, 주간 3배       BREAKOUT     BREAKOUT     BREAKOUT  (rule=AND)
Wed 돌파, 일봉 2배(기준 미달)      None         None         None
```

기준선 동작이 복원됐고, 설정만으로 되돌릴 수 있는 상태가 됐습니다.

### 2번 — `quote_status` 실측

예외를 던지는 대신 `INTRADAY` / `FINAL` / `FINAL_FALLBACK`으로 분류하도록 바뀌었습니다.

```
09:00 KST  session_date=08-24  expected=08-25  quote=08-24 → FINAL_FALLBACK  (예외 없음)
14:00 KST  session_date=08-24  expected=08-25  quote=08-25 → INTRADAY
22:00 KST  session_date=08-25  expected=08-25  quote=08-25 → FINAL
```

아침 스캔이 더 이상 보유종목을 실패 처리하지 않습니다. 다만 라벨링에 문제가 있습니다(C항).

---

## 남은 문제

### A. (중요) KR 지수가 1종목뿐이라 `069500` 하나로 KR 스캔 전체가 조용히 0건이 됩니다

**위치:** `scanner/market_analysis.py:33` (`KR_INDICES`), `scanner/market_analysis.py:249` (`_condition`),
`scanner/scan_engine.py:77` (`_get_market_filter_decision`), `scanner/scan_engine.py:283` (`_process_signal`)

`_condition`이 유효 Stage가 아닌 값이 하나라도 있으면 `UNKNOWN`을 반환하고,
`_get_market_filter_decision`이 `UNKNOWN`을 `(False, "시장 지수 데이터 부족")`으로 차단합니다.
fail-closed 자체는 맞는 방향입니다. 문제는 규모입니다.

```python
KR_INDICES = [
    {"ticker": "069500", "name": "KOSPI200"},   # 단 1종목
]
```

`069500`은 **동시에 KR 벤치마크**(`get_benchmark_close`)이기도 합니다.
이 한 종목이 하루 늦거나 조회에 실패하면:

1. `_analyze_index` → `_unavailable_row(stage="INSUFFICIENT_DATA")`
2. `_condition` → `UNKNOWN`
3. 모든 KR BUY 신호가 `_process_signal`에서 차단

그리고 `_process_signal`의 차단 경로는

```python
if not allow and not STRICT_PERSIST_REJECTED:
    logger.debug(...)      # debug 레벨
    return False
```

이라서 기본 설정에서는 **DEBUG 로그 한 줄 외에 아무 흔적도 남지 않습니다.**
스캔은 `status: done`, `signals_found: 0`으로 정상 종료합니다. 4번 지적(벤치마크 SPOF)이
해소된 게 아니라 경로만 옮겨간 셈입니다. US도 `SPY`/`QQQ` 중 하나만 STALE이면 동일합니다.

**권장** — 지수 목록을 늘리거나(KOSPI/KOSDAQ 2종 이상), `UNKNOWN` 차단 시
`logger.warning`으로 승격하고 스캔 결과에 차단 사유와 건수를 명시적으로 남기십시오.

### B. (중요) `data_quality`를 계산해 놓고 아무도 받지 않습니다

**위치:** `scanner/scan_engine.py:228`, `web/app.py:106`, `scheduler.py:19`

`_new_scan_quality`, `_series_data_quality`, `_data_quality`로 STALE/부족 종목을
집계하는 것까지는 좋은데, 이 값은 `run_scan()`의 **반환 dict에만** 담깁니다.

- `ScanLog`에 기록되는 것은 여전히 `total_scanned` / `signals_found` / `status`뿐입니다.
- `web/app.py:106`과 `scheduler.py:19` 두 호출부 모두 **반환값을 버립니다.**

즉 정기 스캔(=운영상 유일하게 중요한 경로)에서는 집계 결과가 그대로 소멸합니다.
6번 지적의 취지("몇 종목이 왜 빠졌는지 알 수 있어야 한다")는 아직 충족되지 않았고,
7단계 운영 지표로도 쓸 수 없습니다. `ScanLog` 컬럼 추가가 부담이면 최소한
`logger.info`로라도 남겨야 합니다.

### C. (경미) `FINAL_FALLBACK`이 매일 아침 전 종목에 붙어 신호 역할을 못 합니다

**위치:** `scanner/scan_engine.py:451`

```python
is_current_session = quote_date >= expected_quote_date   # expected = last_started_session
```

09:00 KST에는 `quote_date=08-24`, `session_date=08-24`, `expected=08-25`입니다.
즉 **가장 최신 확정봉과 정확히 일치하는데도** `FINAL_FALLBACK`으로 분류되고
`counts["QUOTE_FALLBACK"]`에 집계됩니다. 장 시작 직후에는 어떤 공급자도 당일 봉을
줄 수 없으므로 이 카운터는 **매일 아침 KR 보유 전 종목에 대해 무조건 발화**합니다.
"오래된 가격을 쓰고 있다"는 경고가 상시 켜져 있으면 경고가 아닙니다.

기준을 `session_date`로 바꾸면 의도한 3분류가 됩니다.

```python
quote_status = ("INTRADAY"       if quote_date > scan_context.session_date
                else "FINAL"     if quote_date >= scan_context.session_date
                else "FINAL_FALLBACK")   # 확정 세션보다도 오래된 경우에만
```

### D. (경미) 반올림 차이로 주봉 게이트 경계값이 미세하게 어긋납니다

legacy `weekly_volume_ratio`는 `round(cur_vol / cur_volavg, 2)`인 반면
`progressing_week_volume_ratio`는 반올림 없이 게이트에 들어갑니다.
실제 비율이 1.996이면 legacy는 `2.0`으로 반올림돼 통과하고 PIT는 탈락합니다.
경계에 정확히 걸리는 경우에만 발생하지만, 파리티를 주장하는 코드이므로
`round(progressing_wvr, 2)`로 맞춰두는 편이 안전합니다.

### E. (경미) `STRATEGY_VERSION`은 여전히 검증되지 않는 반쪽 스위치입니다

`config.py:14`의 `STRATEGY_VERSION`에는 허용값 검증이 없습니다.
지금 `STRATEGY_VERSION=weinstein_breakout_v1`로 두면 3단계 엔진은 존재하지 않는데
**legacy 엔진의 거래량 규칙만 OR로 바뀝니다**(`weinstein.py:1162`, `strict_filter.py:292`).
반대로 `weinstein-breakout-v1`처럼 오타가 나면 조용히 legacy로 남습니다.
`WEINSTEIN_V1_MODE`처럼 허용값 집합 검증을 붙이거나, 3단계 엔진이 실제로 붙기 전까지는
`WEINSTEIN_V1_MODE`만 스위치로 쓰는 편이 낫습니다.

### F. (경미) 지표 정의가 조용히 바뀐 곳 2군데

- `scan_engine.py:322,357` — `count += 1`이 STALE 스킵 **앞**으로 이동해
  `total_scanned`가 이제 "분석한 종목"이 아니라 "조회에 성공한 종목"(STALE 포함)입니다.
  `ScanLog.total_scanned` 시계열이 이 커밋을 기점으로 불연속이 됩니다.
- `scan_engine.py:530` — `counts` dict에 `quote_fallback_tickers`(리스트)가 들어가
  기존의 "전부 int" 형태가 깨졌습니다. 현재 소비자가 없어 문제는 없지만,
  `holding_checks`로 그대로 반환되므로 화면/알림을 붙일 때 주의가 필요합니다.

---

## 정리

1번·2번·3번이 모두 해결되어 **3단계 진행을 막을 이유는 없습니다.**
A·B는 3단계 전에 처리하는 편이 좋습니다 — A는 스캔이 통째로 비어도 아무도 모르는 상태이고,
B는 4단계(결과 저장)와 7단계(운영 지표)가 전제로 삼는 계측입니다.
C~F는 3단계 작업 중 함께 정리해도 무방합니다.

여전히 남아 있는 진행 원칙상의 문제는 1·2단계가 하나의 브랜치에 뭉쳐 있어
"legacy 무변경이 검증된 1단계 커밋"이 없다는 점입니다. 다만 이번 커밋으로
**동작상으로는** 기준선이 복원됐으므로, 실질적인 롤백 리스크는 해소됐습니다.
