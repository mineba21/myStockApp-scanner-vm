# 재리뷰 — `2292f02 fix: expose scan data quality failures`

**범위:** `git diff c4cccd2..2292f02` (8개 파일, +212/−32)

**검증:** 전체 테스트 **255개 통과**. `quote_status` 분류와 `STRATEGY_VERSION` 검증은
실제로 실행해 확인했고, 로그 크기는 현실적인 장애 시나리오로 측정했습니다.

---

## 직전 지적 처리 결과

| # | 항목 | 결과 |
|---|---|---|
| A | 지수 STALE 시 KR 스캔이 조용히 0건 | **침묵만 해소** — 아래 1항 |
| B | `data_quality`를 계산만 하고 버림 | **해결** (`_log_scan_data_quality`) |
| C | `FINAL_FALLBACK`이 매일 아침 오발화 | **해결 (실측 확인)** |
| D | 주봉 게이트 반올림 불일치 | **해결** (`round(x, 2)`) |
| E | `STRATEGY_VERSION` 미검증 | **해결 (실측 확인)** |
| F | `total_scanned` 정의 변경 / `counts` 타입 오염 | **둘 다 해결** |

### C — `quote_status` 실측

기준을 `expected_quote_date`에서 `session_date`로 바꾼 결과가 의도대로 나옵니다.

```
09:00 KST  sess=08-24  quote=08-24 → FINAL            (기존: FINAL_FALLBACK 오발화)
09:00 KST  sess=08-24  quote=08-25 → INTRADAY
09:00 KST  sess=08-24  quote=08-21 → FINAL_FALLBACK   (진짜 지연만 발화)
22:00 KST  sess=08-25  quote=08-24 → FINAL_FALLBACK
```

### E — `STRATEGY_VERSION` 검증 실측

```
'legacy_v4'             → ok
'weinstein_breakout_v1' → ok
'weinstein-breakout-v1' → ValueError (오타가 조용히 legacy로 남지 않음)
''                      → ok legacy_v4 (빈 값 fallback 정상)
```

### F — `count` 위치 복원

`count += 1`이 STALE 스킵 **뒤**로 옮겨져 `total_scanned`가 다시 "분석한 종목"을
의미합니다. `requested_count = scanned + stale + insufficient`로 정합성도 맞습니다.

---

## 남은 문제

### 1. (권장) A항의 근본 원인은 그대로입니다 — `KR_INDICES`는 여전히 1종목

`scanner/market_analysis.py`는 이번 커밋에서 **변경되지 않았습니다.**

```python
KR_INDICES = [
    {"ticker": "069500", "name": "KOSPI200"},   # 여전히 1종목
]
```

`_process_signal`의 로그를 `UNKNOWN`일 때 `logger.warning`으로 승격하고
`run_scan`이 시장 단위 경고를 추가한 덕분에 **"아무도 모르게 0건"은 해소**됐습니다.
하지만 `069500`(= KR 벤치마크이기도 함) 하나가 STALE이면 KR BUY 전량이 차단되는
구조 자체는 그대로입니다. 관측은 되지만 여전히 하루치 스캔이 통째로 날아갑니다.

지수를 2종목 이상으로 늘리거나(KOSDAQ 추가), `_condition`이 일부 결측을 허용하도록
정족수 방식으로 바꾸는 것을 권합니다. 관측이 붙었으니 3단계 이후로 미뤄도 됩니다.

### 2. (경미) 데이터 장애일에 스캔 1회당 로그 34KB가 쌓입니다

**위치:** `scanner/scan_engine.py:445` (`_log_scan_data_quality`)

`json.dumps(data_quality)`가 `stale_tickers` / `insufficient_tickers` 전체 목록을
INFO 한 줄에 담고, 바로 아래 WARNING이 **같은 목록을 한 번 더** 출력합니다.
공급자 장애로 KOSPI+KOSDAQ 2800종목 중 1700종목이 빠진 날을 가정해 측정하면:

```
INFO  한 줄 길이 : 17,268 bytes
WARNING 중복 포함 : 약 34 KB / 스캔  (하루 3회 → 약 100 KB/일)
```

정상일에는 목록이 비어 문제없지만, **장애일에만 로그가 폭증**하는 형태라
정작 필요할 때 로그를 읽기 어려워집니다. 목록은 상한을 두고(예: 앞 20개 + 총건수),
전체 목록이 필요하면 별도 파일이나 4단계 DB 컬럼으로 넘기는 편이 낫습니다.

### 3. (경미) `_market_filter_quality`가 `"BREAKOUT"`을 하드코딩해 차단 범위를 과장합니다

**위치:** `scanner/scan_engine.py:432`

```python
allow, reason = _get_market_filter_decision(condition, "BREAKOUT")
return {..., "buy_blocked": not allow, ...}
```

`CAUTION_MODE="block_breakout"`이면 CAUTION 장세에서 BREAKOUT만 차단되고
REBOUND·RE_BREAKOUT은 통과하는데, 이 함수는 `buy_blocked: True`로 보고하고
`run_scan`은 `"[KR] BUY 스캔 차단"`이라는 경고를 냅니다. 실제로는 일부만 차단됩니다.
기본값이 `allow_with_flag`라 지금 당장 발생하지는 않지만, 필드 이름과 경고 문구가
사실과 어긋나므로 신호 유형별로 계산하거나 `breakout_blocked`로 이름을 좁히십시오.

### 4. (경미) `UNKNOWN`일 때 경고가 종목 수만큼 반복됩니다

**위치:** `scanner/scan_engine.py:294`

```python
log_fn = logger.warning if market_condition == "UNKNOWN" else logger.debug
log_fn("[%s] %s BUY 후보 차단: %s", market_label, ticker, flag)
```

`run_scan`(`scan_engine.py:178`)이 이미 시장 단위로 한 번 경고하므로,
후보가 잡힌 종목마다 WARNING을 또 남기는 것은 중복입니다. 종목 단위는 DEBUG로 두고
집계값(`market_blocked_count`)만 상위에서 보고하면 충분합니다.

### 5. (참고) `ScanLog`에는 아직 기록되지 않습니다

로그로는 남지만 `ScanLog` 컬럼은 여전히 `total_scanned` / `signals_found` / `status`
뿐입니다. 로그 회전 후에는 조회할 수 없으므로, 7단계 운영 지표로 쓰려면 4단계
마이그레이션에서 컬럼을 추가해야 합니다. 지금 단계의 결함은 아니고 다음 단계 과제입니다.

---

## 정리

이번 커밋으로 직전 지적 6건 중 5건이 완전히 해결됐고, 나머지 1건(A)도
관측 가능성은 확보됐습니다. **3단계 진행에 막히는 항목은 없습니다.**

2~4번은 모두 로그·보고 정확도 문제로 코드 위험은 없으니 3단계 작업 중 함께 정리하면 되고,
1번은 지수 목록을 늘릴지 정족수 방식으로 갈지 결정만 남겨두면 됩니다.
