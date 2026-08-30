# Step 2 리뷰 체크리스트 — 2단 Base 구조 + 시장별 임계값

외부 리뷰어용. 각 항목은 **스크립트를 돌려 참/거짓이 나오는 형태**다.
"코드를 읽고 확인" 항목은 없다.

## 실행 환경

```bash
cd myStockApp-scanner-vm
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt "setuptools<81"
export PY=.venv/bin/python
```

아래 스크립트는 모두 레포 루트에서 실행한다.

## 변경 대상 커밋 범위

```
config.py                BASE_MODE / market_param / BASE_LOOKBACK_DAYS 등
                          / BREAKOUT_WEEKLY_VOL_RATIO(2.0→0.5) /
                          BREAKOUT_DAILY_VOL_RATIO(3.0→1.5) /
                          BREAKOUT_MAX_EXTENDED_PCT(15.0→25.0)
scanner/weinstein.py     detect_base_pivot_v2 / _stage2_breakout_loop_v1|v2 /
                          detect_stage2_breakout / compute_stop_loss /
                          analyze_stock / _REJECT_RANK
scanner/scan_engine.py   _new_funnel / _funnel_record / _finalize_funnel
                          (base_mode, base_stats, stop_pct_stats,
                          weekly_gate_cut_but_would_pass_daily)
tests/test_weinstein.py  _make_stage2_base(진폭 수축) + 임계값 의존 테스트 3개
```

## 전체 테스트

```bash
$PY -m pytest tests/ -q
```

**기대**: `271 passed`. (Step 1 종료 시점 baseline 242 + Step 2 신규 20 +
Codex 리뷰 대응 신규 9 — 아래 "8. Codex 리뷰 대응" 참고)

---

## 1. look-ahead — 신호일 bar 가 base/tight 계산에서 제외되는지

신호일 High 를 극단값(99,999)으로 바꿔도 `pivot_price`/`stop_ref` 가 불변이어야
한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weinstein import _make_df, _make_stage2_base
from scanner.weinstein import (compute_weekly_indicators, to_weekly_ohlcv,
                               _build_indicators, detect_stage2_breakout)

prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
prices[-1], volumes[-1] = 104.0, 6_000_000
df_normal = _make_df(prices, volumes)
df_extreme = df_normal.copy()
df_extreme.iloc[-1, df_extreme.columns.get_loc("High")] = 99_999.0

w_n = compute_weekly_indicators(to_weekly_ohlcv(df_normal),  df_normal)
w_e = compute_weekly_indicators(to_weekly_ohlcv(df_extreme), df_extreme)
sig_n = detect_stage2_breakout(df_normal,  w_n, _build_indicators(df_normal),  market="US")
sig_e = detect_stage2_breakout(df_extreme, w_e, _build_indicators(df_extreme), market="US")

print("pivot_price 동일:", sig_n["pivot_price"] == sig_e["pivot_price"],
      sig_n["pivot_price"], sig_e["pivot_price"])
print("stop_ref 동일   :", sig_n["stop_ref"] == sig_e["stop_ref"])
EOF
```

**기대**
```
pivot_price 동일: True 100.9731 100.9731
stop_ref 동일   : True
```

pytest 노드: `tests/test_base_pivot_v2.py::TestLookAheadExclusion` (3개 —
직접 `detect_base_pivot_v2` 호출로 High/Low 각각 검증 + 위 end-to-end 재현)

---

## 2. v1 회귀 — 이번 변경 전과 반환값이 완전 동일한지

`BASE_MODE=v1` 일 때 `detect_stage2_breakout` 의 출력이 Step 2 이전(원본)
코드와 **동일 입력**에 대해 완전히 같아야 한다. 원본은 아직 커밋되지 않은
작업 트리이므로 `git worktree`로 pristine `HEAD`를 꺼내 비교한다. fixture 는
Step 2가 손댄 `_make_stage2_base`(진폭 수축)가 아니라, 원본 그대로의
고정폭 sine 을 스크립트에 인라인으로 재현해 **입력 자체도 완전히 고정**한다.

```bash
git worktree add --detach /tmp/step2_baseline HEAD

cat > /tmp/v1_regress_probe.py <<'EOF'
import sys, os, inspect, json
sys.path.insert(0, os.getcwd())
import numpy as np, pandas as pd
from datetime import date, timedelta
from scanner.weinstein import (compute_weekly_indicators, to_weekly_ohlcv,
                               _build_indicators, detect_stage2_breakout)

def make_df(prices, volumes):
    n = len(prices)
    dates = [date(2022, 1, 1) + timedelta(days=i) for i in range(n)]
    close = [float(p) for p in prices]
    return pd.DataFrame({"Open": [p*0.998 for p in close], "High": [p*1.005 for p in close],
                         "Low": [p*0.995 for p in close], "Close": close,
                         "Volume": [float(v) for v in volumes]}, index=pd.DatetimeIndex(dates))

def original_stage2_base(n_total=260, base_price=100.0):
    prices, volumes = [], []
    for i in range(150):
        prices.append(50.0 + (base_price - 5 - 50) * i / 149); volumes.append(500_000)
    for i in range(n_total - 150):
        prices.append(base_price + 2 * np.sin(i * np.pi / 5)); volumes.append(500_000)
    return prices, volumes

prices, volumes = original_stage2_base(n_total=230, base_price=100.0)
prices[-1], volumes[-1] = 104.0, 6_000_000
df = make_df(prices, volumes)
di = _build_indicators(df)
wi = compute_weekly_indicators(to_weekly_ohlcv(df), df) \
     if "daily_df" in inspect.signature(compute_weekly_indicators).parameters \
     else compute_weekly_indicators(to_weekly_ohlcv(df))
kwargs = {"market": "US"} if "market" in inspect.signature(detect_stage2_breakout).parameters else {}
print(json.dumps(detect_stage2_breakout(df, wi, di, **kwargs), ensure_ascii=False, sort_keys=True, default=str))
EOF

echo "=== 원본(HEAD) ===" && (cd /tmp/step2_baseline && $PY /tmp/v1_regress_probe.py) > /tmp/v1_before.json && cat /tmp/v1_before.json
echo "=== 현재 BASE_MODE=v1 ===" && BASE_MODE=v1 $PY /tmp/v1_regress_probe.py > /tmp/v1_after.json && cat /tmp/v1_after.json

$PY - <<'EOF'
import json
a = json.load(open("/tmp/v1_before.json"))
b = json.load(open("/tmp/v1_after.json"))
diffs = {k: (a[k], b[k]) for k in (set(a) & set(b)) if a[k] != b[k]}
print("추가된 키(허용 — additive):", set(b) - set(a))
print("공통 키 값 차이(있으면 회귀):", diffs)
print("PASS:", not diffs)
EOF

git worktree remove --force /tmp/step2_baseline
```

**기대**
```
추가된 키(허용 — additive): {'base_mode'}
공통 키 값 차이(있으면 회귀): {}
PASS: True
```

> `base_mode` 키 하나만 새로 추가됐고(v1 경로도 이제 `"base_mode": "v1"` 을
> 명시), 그 외 모든 필드(`pivot_price`, `base_low`, `base_width_pct`,
> `base_quality`, `base_quality_v4`, `base_weeks`, `vol_ratio`, ...) 는
> 원본과 값까지 완전히 동일해야 한다.

pytest 노드: `tests/test_base_pivot_v2.py::TestBaseModeToggle::test_v1_toggle_uses_single_expanding_base`

---

## 3. 손절폭 — v2 가 v1 보다 유의하게 작은지

5개 합성 종목(base_price/breakout 크기 다양화)에 대해 v1/v2 각각의
`(진입가-손절가)/진입가` 를 계산하고 중앙값을 비교한다.

```bash
$PY - <<'EOF'
import sys, os, statistics; sys.path.insert(0, os.getcwd())
from scanner import weinstein
from scanner.weinstein import analyze_stock
from tests.test_weinstein import _make_df, _make_stage2_base

def stop_pct(mode, n_total, base_price, breakout_price, breakout_vol):
    weinstein.BASE_MODE = mode
    prices, volumes = _make_stage2_base(n_total=n_total, base_price=base_price)
    prices[-1], volumes[-1] = breakout_price, breakout_vol
    df = _make_df(prices, volumes)
    res = analyze_stock(df, "T", "T", "US")
    if res is None or res.get("stop_loss") is None:
        return None
    return (res["price"] - res["stop_loss"]) / res["price"]

cases = [(230,100.0,104.0,6_000_000), (220,90.0,95.0,5_000_000),
        (260,120.0,126.0,7_000_000), (280,80.0,85.0,6_500_000),
        (300,150.0,158.0,8_000_000)]
v1s = [stop_pct("v1", *c) for c in cases]
v2s = [stop_pct("v2", *c) for c in cases]
for c, a, b in zip(cases, v1s, v2s):
    print(f"n_total={c[0]:4} v1={a} v2={b}")
v1med, v2med = statistics.median(v1s), statistics.median(v2s)
print("v1 median:", v1med, "| v2 median:", v2med, "| v2 < v1:", v2med < v1med)
weinstein.BASE_MODE = "v2"
EOF
```

**기대** — 5건 전부 유효값이 나오고, v2 중앙값이 v1보다 뚜렷이 작아야 한다
(합성 데이터 기준 16.6% → 6.7% 수준; 실측 S&P500 은 14.5%→8.3%).
```
v1 median: 0.1659... | v2 median: 0.0668... | v2 < v1: True
```

pytest 노드: 직접 대응하는 노드는 없음 — `tests/test_base_pivot_v2.py::TestBaseModeToggle::test_v1_and_v2_agree_on_gate_but_differ_on_base_fields`
가 단일 케이스에서 `base_low`(≈손절 기준)가 달라짐을 확인한다.

---

## 4. 수축 조건 — 껐을 때와 켰을 때 신호 수 차이

`TIGHT_CONTRACTION_RATIO=1.0`(사실상 무효 — 어떤 tight_width 도 통과)과
기본값 `0.85` 를 같은 "거의 수축하지 않는" fixture 에 적용해 신호 유무가
갈리는지 확인한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
import numpy as np, config
from scanner.weinstein import analyze_stock
from tests.test_weinstein import _make_df

prices, volumes = [], []
for i in range(150):
    prices.append(50.0 + 95.0 * i / 149); volumes.append(500_000)
for i in range(80):
    frac = i / 79
    amp = 2.5 + (2.1 - 2.5) * frac   # 2.5 -> 2.1, 거의 수축 안 함
    prices.append(100.0 + amp * np.sin(i * np.pi / 5)); volumes.append(500_000)
prices[-1], volumes[-1] = 104.0, 6_000_000
df = _make_df(prices, volumes)

for ratio, label in ((0.85, "기본 0.85"), (1.0, "무효화 1.0")):
    config.TIGHT_CONTRACTION_RATIO = ratio
    diag = {}
    res = analyze_stock(df, "T", "T", "US", diag=diag)
    print(f"{label}: signal={res and res['signal_type']} "
          f"reject={diag['detectors']['BREAKOUT'].get('reject')}")
config.TIGHT_CONTRACTION_RATIO = 0.85
EOF
```

**기대**
```
기본 0.85: signal=None reject=no_contraction
무효화 1.0: signal=BREAKOUT reject=None
```

pytest 노드: `tests/test_base_pivot_v2.py::TestDetectBasePivotV2Rejects::test_no_contraction`
(별도로 `test_passes_when_width_and_contraction_both_satisfied` 가 대조군)

---

## 5. 시장별 파라미터 — KR_BASE_MAX_WIDTH_PCT=30 이 실제 적용되는지

**동일한 종목 데이터**(자격 구간 폭 정확히 27%)를 시장만 바꿔 넣으면
US(25%) 는 거부, KR(30%) 는 통과해야 한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner.weinstein import detect_base_pivot_v2
from tests.test_base_pivot_v2 import _outer_inner_bounds, _v2_df
import config

print("KR_BASE_MAX_WIDTH_PCT:", config.KR_BASE_MAX_WIDTH_PCT,
      "US_BASE_MAX_WIDTH_PCT:", config.US_BASE_MAX_WIDTH_PCT)

oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=27, inner_width_pct=5)
df = _v2_df(oh, ol, ih, il)   # 동일 데이터, market 인자만 바꿔 호출

diag_us = {}
r_us = detect_base_pivot_v2(df, market="US", diag=diag_us)
r_kr = detect_base_pivot_v2(df, market="KR")
print("market=US:", r_us, "| reject:", diag_us.get("reject"))
print("market=KR:", "성공" if r_kr else "실패", r_kr and r_kr["base_width"])
EOF
```

**기대**
```
KR_BASE_MAX_WIDTH_PCT: 30.0 US_BASE_MAX_WIDTH_PCT: 25.0
market=US: None | reject: base_too_wide
market=KR: 성공 27.0
```

pytest 노드:
`tests/test_base_pivot_v2.py::TestMarketParam::test_kr_wider_base_width_lets_wider_candidates_through`
(`market_param()` 자체의 3단계 조회 순서는 `test_market_specific_override_wins` /
`test_falls_back_to_common_when_no_market_override` /
`test_falls_back_to_default_when_nothing_defined` 로 별도 검증)

---

## 6. 주봉 0.5 컷 — weekly_gate_cut_but_would_pass_daily 가 0인지

두 단계로 확인한다: **(a) 카운터 메커니즘 자체가 작동하는지**(작동하지
않으면 "0" 이 무의미), **(b) 합성 유니버스에서 실측값이 0인지**.

### (a) 메커니즘 확인 — 의도적으로 걸리는 시나리오

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner.weinstein import analyze_stock, BREAKOUT_WEEKLY_VOL_RATIO, BREAKOUT_DAILY_VOL_RATIO
from tests.test_weinstein import _make_df, _make_stage2_base

print("thresholds: weekly", BREAKOUT_WEEKLY_VOL_RATIO, "daily", BREAKOUT_DAILY_VOL_RATIO)

# 돌파일 거래량은 높지만(일봉 조건 충족), 그 주의 다른 날들은 낮게 눌러
# "그 주" 합계만 미달로 만든다 (일봉 20일 rolling 평균은 주 경계를 안 탐)
prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
for i in range(len(volumes)):
    volumes[i] = 500_000
for k in range(1, 6):
    volumes[-1 - k] = 20_000
prices[-1], volumes[-1] = 104.0, 900_000
df = _make_df(prices, volumes)

diag = {}
res = analyze_stock(df, "T", "테스트", "US", diag=diag)
bdiag = diag["detectors"]["BREAKOUT"]
print("reject:", bdiag.get("reject"), "| wvr:", bdiag.get("wvr"),
      "| would_pass_daily_volume:", bdiag.get("would_pass_daily_volume"))
EOF
```

**기대**
```
thresholds: weekly 0.5 daily 1.5
reject: weekly_volume_insufficient | wvr: 0.36 | would_pass_daily_volume: True
```

> 이 시나리오는 메커니즘이 살아있음을 보이기 위해 **일부러** 만든 극단
> 케이스다. `would_pass_daily_volume: True` 가 나와야 카운터가 이런 종목을
> 놓치지 않는다는 뜻이고, 아래 (b)에서 실제 스캔이 이런 종목을 만들지
> 않는지를 본다.

pytest 노드: `tests/test_base_pivot_v2.py::TestFunnelStep2Fields::test_weekly_gate_cut_but_would_pass_daily_counts_only_flagged_stocks`
(unit 레벨 — `_funnel_record` 만 직접 검증)

### (b) 합성 유니버스 집계 — 실제 funnel 값

```bash
$PY - <<'EOF'
import sys, os, types; sys.path.insert(0, os.getcwd())
from tests.test_weinstein import _make_df, _make_stage2_base
from scanner import scan_engine

def sig_df():
    p, v = _make_stage2_base(n_total=230, base_price=100.0)
    p[-1], v[-1] = 104.0, 6_000_000
    return _make_df(p, v)

rows = [({"ticker": "SIG", "name": "시그널"}, sig_df())]
rows += [({"ticker": f"F{i}", "name": f"평범{i}"},
          _make_df(*_make_stage2_base(n_total=230, base_price=70.0 + i * 3)))
         for i in range(15)]

fake = types.ModuleType("scanner.us_stocks")
fake.get_all_us_tickers = lambda u: [r[0] for r in rows]
fake.get_us_batch = lambda t, progress_callback=None: rows
sys.modules["scanner.us_stocks"] = fake
scan_engine._save = lambda db, s: None
class DB:
    def add(self, *a): pass
    def commit(self): pass

funnel = scan_engine._new_funnel()
scan_engine._scan_us(DB(), "t", None, "BULL", funnel=funnel)
print("weekly_gate_cut_but_would_pass_daily:", funnel["weekly_gate_cut_but_would_pass_daily"])
for f in os.listdir("logs"): os.unlink(os.path.join("logs", f))
EOF
```

**기대**: `weekly_gate_cut_but_would_pass_daily: 0`

> **합성 데이터에서 0이라는 것이 실제 KR/US 유니버스에서도 0이라는 보장은
> 아니다.** 이 항목의 진짜 검증은 다음 실스캔의 funnel JSON에서 이 필드를
> 읽는 것이다 — 0이 아니면 0.5 도 낮춰야 한다는 뜻이므로 Step 2 완료
> 보고에 실측치를 반드시 포함해야 한다.

---

## 7. `BREAKOUT_MAX_EXTENDED_PCT` 공유 확인 (부수 발견)

이 값은 legacy `_find_breakout_signal`(v3) 과 v4 `detect_stage2_breakout`
양쪽이 같은 config 상수를 공유한다 — v4 전용 오버라이드가 없다. 15.0→25.0
변경이 v3 쪽에도 그대로 적용된다는 뜻이므로, 의도한 대로 "공통" 변경인지
확인한다.

```bash
grep -n "BREAKOUT_MAX_EXTENDED_PCT" config.py scanner/weinstein.py
```

**기대**: `config.py` 에 `"25.0"` 기본값 하나만 있고, `scanner/weinstein.py`
의 legacy `_find_breakout_signal`(981번째 줄 부근)과 v4 `detect_stage2_breakout`
양쪽 모두 이 동일한 이름을 참조해야 한다(별도 `V4_` 접두사 상수 없음).

> `tests/test_strict_filter.py` 의 Gate 7 테스트들은 `_force_strict_flag`
> 로 이 값을 15.0 으로 직접 고정하므로 config 기본값 변경과 무관하게
> 통과한다 — strict_filter 의 과열 판정 자체는 이번에 바뀌지 않았다.

---

## 8. Codex 리뷰 대응 (필수 2건 + 선택 2건)

### 8-1. `.env.example` 동기화 (P1)

`load_dotenv()` 가 배포자의 실제 `.env` 를 읽으므로, `.env.example` 에 구
임계값이 남아 있으면 `cp .env.example .env` 하는 순간 코드 기본값을
덮어써 버린다. 실효값(코드 기본값 vs `.env.example` 적용값)을 서브프로세스
2개로 비교하는 스크립트를 만들었다.

```bash
$PY scripts/verify_env_defaults.py
```

**기대**: 13개 파라미터 전부 `OK`, 마지막 줄 `PASS`.

> 스크립트 자체가 실제로 불일치를 잡아내는지도 확인하려면, `.env.example`
> 의 `BREAKOUT_WEEKLY_VOL_RATIO=0.5` 를 일시적으로 `2.0` 으로 바꾸고
> 재실행 — `MISMATCH` + `exit=1` 이 나와야 한다(원복 잊지 말 것).

이 저장소에는 `.env` 파일이 없다(레포 루트부터 6단계까지 검색해도 없음) —
지금까지의 스캔은 이 이슈의 영향을 받지 않았다. 실제 배포 시 `.env` 가
있다면 위 스크립트를 `.env` 를 실제 로드한 상태(`cwd` 를 레포 루트로)로
재실행해 확인할 것.

### 8-2. `stop_pct_stats` 를 signal-date 가격 기준으로

`res["price"]`(최신 종가)와 `stop_loss`(signal_date 시점 계산)를 섞으면
최대 7일 전 신호에서 손절폭이 왜곡된다. `strict_price`(signal_date 시점
종가) 기준으로 바꾸고, `strict_price` 가 없는 신호는 표본에서 제외했다.
참고용으로 `stop_pct_at_current_price`(현재가 기준, 추격 리스크 표시용)
를 병렬로 남겼다.

```bash
$PY -m pytest tests/test_base_pivot_v2.py::TestFunnelStep2Fields::test_base_and_stop_pct_stats_computed_from_breakout_signals_only -v
```

**기대**: PASS. signal_date 가격(100)과 최신 가격(110)이 다른 케이스에서
`stop_pct_stats`(0.08 기준 반영, median 0.09)와
`stop_pct_at_current_price`(median 0.10)가 서로 다르게 나온다.

### 8-3. (선택) 공통 오버라이드 상속 — `BASE_MAX_WIDTH_PCT`/`TIGHT_MAX_WIDTH_PCT`

기존엔 `US_BASE_MAX_WIDTH_PCT`/`KR_BASE_MAX_WIDTH_PCT` 가 항상 독립된
하드코딩 기본값(25.0/30.0)으로 채워져 있어, 공통 `BASE_MAX_WIDTH_PCT` 만
바꿔도 시장별 값엔 반영되지 않았다. 조회 순서를 "시장별 env → **공통 env
가 명시적으로 설정된 경우만** 그 값 → 시장별 하드코딩 기본값" 으로 바꿨다
— 공통값의 자체 기본값(25.0)은 폴백 대상에서 제외해, 아무것도 설정하지
않은 배포에서는 KR=30/US=25 의 의도적 차이가 그대로 유지된다.

```bash
$PY -m pytest tests/test_base_pivot_v2.py::TestMarketParamEnvInheritance -v
```

**기대**: 4개 전부 PASS —
- 아무것도 설정 안 하면 US=25/KR=30 유지
- `BASE_MAX_WIDTH_PCT=20` 만 설정하면 US/KR 둘 다 20
- `KR_BASE_MAX_WIDTH_PCT=35` 를 추가로 설정하면 KR 은 35(시장별 env 우선), US 는 여전히 공통값(20)
- `TIGHT_MAX_WIDTH_PCT` 도 동일 패턴

### 8-4. (선택) v2 시그널의 `BASE_MIN_WEEKS` 검사 분리

`strict_filter._check_base` 가 v2 신호에도 `base_weeks < BASE_MIN_WEEKS`
를 그대로 적용했다. v2 의 `base_weeks` 는 `BASE_LOOKBACK_DAYS` 로 고정된
상수(예: 25일→5.0주)라, `BASE_LOOKBACK_DAYS` 를 5주 미만으로 줄인 배포에서
는 이 검사를 절대 통과할 수 없어 모든 v2 BREAKOUT 이 차단됐다. `base_mode
== "v2"` 인 시그널은 이 비교를 건너뛰도록 바꿨다(필드 존재 검사와
`base_too_wide` 검사는 그대로 유지).

```bash
$PY -m pytest "tests/test_strict_filter.py::TestBaseGate" -v
```

**기대**: 11개 전부 PASS(기존 6 + 신규 5) — 특히
`test_v2_short_base_weeks_does_not_block`(base_weeks=3.0 이어도 v2 는
통과)와 `test_v1_short_base_weeks_still_blocks_regardless_of_base_mode_field`
(v1 은 여전히 차단)이 대조를 이뤄야 한다.

---

## 리뷰 판단이 필요한 지점 요약

| # | 사안 | 상태 |
|---|---|---|
| — | **작업지시서 내부 모순**: "제약: 임계값 2.0은 여전히 변경 금지" vs 작업4의 명시적 지시 "BREAKOUT_WEEKLY_VOL_RATIO 2.0→0.5" | 작업4를 따랐음(데이터 근거 명시), 제약 문구는 Step1 템플릿의 잔존으로 판단. 리뷰 시 의도 재확인 필요 |
| 7 | `BREAKOUT_MAX_EXTENDED_PCT` 를 legacy v3 와 공유 | v3 도 15%→25% 로 완화됨 (의도 여부 확인 필요) |
| 2 | v1 회귀 검증에 pristine 원본(git worktree)이 아니라 `BASE_MODE=v1` 스위치를 신뢰 | 두 경로 모두 제공했으나, 실제 배포 시엔 워크트리 비교만 신뢰할 것 |
| 3 | "유의하게 작은지"를 통계 검정 없이 5개 표본 중앙값 비교로만 확인 | 표본 수가 적음 — 실스캔 `stop_pct_stats` 로 재검증 권장 |
