# Step 1 리뷰 체크리스트 — 주봉 거래량 산출 기준

외부 리뷰어용. 각 항목은 **스크립트를 돌려 참/거짓이 나오는 형태**다.
"코드를 읽고 확인" 항목은 없다.

## 실행 환경

레포에 venv가 없다. pandas는 `<2.0`, numpy는 `<2.0`이 필요하고 pykrx가
`pkg_resources`를 쓰므로 `setuptools<81`도 필요하다.

```bash
cd myStockApp-scanner-vm
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt "setuptools<81"
export PY=.venv/bin/python
```

아래 스크립트는 모두 레포 루트에서 실행한다. 인라인 스크립트는
`$PY - <<'EOF' ... EOF` 로 붙여넣으면 그대로 돈다.

## 변경 대상 커밋 범위

```
scanner/weinstein.py    compute_weekly_indicators / _current_week_state /
                        detect_stage2_breakout / analyze_stock
scanner/scan_engine.py  _new_funnel / _funnel_record / _finalize_funnel
scanner/strict_filter.py _check_volume (Gate 5) — Codex P1 반영
config.py               BREAKOUT_WEEKLY_VOL_AS_GATE
tests/test_weinstein.py TestNoLookAhead 레퍼런스 2줄 (항목 6 참고)
```

## 전체 테스트

```bash
$PY -m pytest tests/ -q
```

**기대**: `242 passed`. (Step 0 종료 시점 baseline 226 + Step 1 신규 16)

---

## 1. 회귀 검증 — 완전 주 입력에서 반환값 동일

AS_GATE=true(기본)에서, 완전 주(elapsed=5) 입력에 대해 `detect_stage2_breakout`의
반환값이 변경 전후 동일해야 한다. 변경 전 동작 = `compute_weekly_indicators`에
`daily_df`를 넘기지 않은 경우(레거시 경로)와 정확히 같다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import (to_weekly_ohlcv, compute_weekly_indicators,
                               _build_indicators, detect_stage2_breakout)

days = _trading_days("2023-01-02", 80)          # 금요일 마감 = 완전 주
df   = _df_from_days(days)
weekly, daily_ind = to_weekly_ohlcv(df), _build_indicators(df)

before = compute_weekly_indicators(weekly)       # 변경 전 경로 (daily_df 없음)
after  = compute_weekly_indicators(weekly, df)   # 변경 후 경로

print("basis      :", after["week_volume_basis"], "elapsed", after["week_elapsed_days"])
print("ratio 동일 :", before["weekly_volume_ratio"] == after["weekly_volume_ratio"])
print("detect 동일:", detect_stage2_breakout(df, before, daily_ind)
                    == detect_stage2_breakout(df, after, daily_ind))
EOF
```

**기대**
```
basis      : CURRENT_COMPLETE elapsed 5
ratio 동일 : True
detect 동일: True
```

pytest 노드: `tests/test_weekly_volume_basis.py::TestWeekBasisPaths::test_complete_week_uses_current_without_normalization`
및 `::test_daily_df_omitted_keeps_legacy_behavior`

> 완전 주에서 basis 정책은 no-op이어야 한다. `CURRENT_COMPLETE`가 아니거나
> `ratio 동일`이 False면 회귀다.

---

## 2. 10주 평균 오염 — 분모에 부분 주가 섞이지 않는지

`PREVIOUS_COMPLETE` 경로에서 부분 주 거래량을 극단값으로 흔들어도 최종 비율이
**전혀 변하지 않아야** 한다. 흔들리면 분모 rolling(10)에 부분 주가 섞인 것이다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

days = _trading_days("2023-01-02", 80)[:-3]      # 화요일 마감 = 경과 2일
def ratio(v):
    df = _df_from_days(days, last_week_daily_vol=v)
    w  = compute_weekly_indicators(to_weekly_ohlcv(df), df)
    return w["week_volume_basis"], w["weekly_volume_ratio"], w["weekly_volume_ratio_raw"]

tiny, huge = ratio(10_000), ratio(25_000_000)    # 2500배 차이
print("tiny:", tiny)
print("huge:", huge)
print("최종 비율 불변 :", tiny[1] == huge[1])
print("raw 는 흔들림  :", tiny[2] != huge[2])    # 대조군 — 오염 없으면 raw만 변해야
EOF
```

**기대**
```
tiny: ('PREVIOUS_COMPLETE', 1.0, 0.02)
huge: ('PREVIOUS_COMPLETE', 1.0, 8.47)
최종 비율 불변 : True
raw 는 흔들림  : True
```

핵심은 3번째 줄이다. raw 비율이 0.02 ↔ 8.47로 400배 벌어지는 동안 최종
비율은 1.0으로 고정 — 분모가 부분 주의 영향을 전혀 받지 않는다는 뜻이다.

pytest 노드: `tests/test_weekly_volume_basis.py::TestRollingAverageNotPolluted::test_previous_complete_average_excludes_partial_week`

`CURRENT_NORMALIZED` 경로도 같은 규칙을 적용했다(분자만 정규화하고 분모를
오염된 채 두면 이중 보정이 된다). 검증:
`::TestRollingAverageNotPolluted::test_normalized_average_also_excludes_partial_week`

> **작업지시서에 명시된 것은 PREVIOUS_COMPLETE뿐이다.** CURRENT_NORMALIZED까지
> 확장한 것은 의도적 판단이므로 리뷰에서 반려 가능하다.

---

## 3. 공휴일 단축주 — 5/4 곱셈이 일어나지 않는지

거래일이 4일뿐인 주에서 `basis`가 `CURRENT_COMPLETE`로 나오고 정규화가
적용되지 않아야 한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

days = _trading_days("2023-01-02", 80)
days = [d for d in days if d != days[-5]]        # 마지막 주 월요일 휴장
assert days[-1].weekday() == 4                   # 금요일 마감, 거래일 4일
df = _df_from_days(days)
w  = compute_weekly_indicators(to_weekly_ohlcv(df), df)
print("elapsed:", w["week_elapsed_days"], "basis:", w["week_volume_basis"])
print("5/4 곱셈 없음:", w["weekly_volume_ratio"] == w["weekly_volume_ratio_raw"])
EOF
```

**기대**
```
elapsed: 4 basis: CURRENT_COMPLETE
5/4 곱셈 없음: True
```

pytest 노드: `tests/test_weekly_volume_basis.py::TestHolidayShortenedWeek::test_four_day_week_ending_friday_is_complete`
및 `::test_four_day_week_followed_by_next_week_is_complete`

### 알려진 한계 — 리뷰에서 반드시 판단할 것

완성 판정은 `_current_week_state()`가 다음 두 규칙으로만 한다(휴장일 캘린더 미도입):

1. 마지막 일봉 뒤에 더 늦은 ISO 주의 일봉이 있으면 → 완성 (확정)
2. 마지막 일봉이 금요일이면 → 완성 (금요일은 항상 그 주 마지막 거래일)

따라서 **금요일이 휴장인 주의 목요일 장 마감 시점**(예: Good Friday, 설 연휴가
금요일에 걸린 주)은 인덱스만으로 완성 여부를 구분할 수 없어 미완성으로 처리되고
×5/4가 적용된다. 재현:

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

days = _trading_days("2023-01-02", 80)
fri  = days[-1]
days = [d for d in days if d != fri]             # 마지막 주 금요일 휴장 → 목요일 마감
df = _df_from_days(days)
w  = compute_weekly_indicators(to_weekly_ohlcv(df), df)
print("elapsed:", w["week_elapsed_days"], "basis:", w["week_volume_basis"])
print("분자에 5/4 적용됨:", w["week_volume_basis"] == "CURRENT_NORMALIZED")
EOF
```

**현재 동작(= 알려진 한계)**
```
elapsed: 4 basis: CURRENT_NORMALIZED
분자에 5/4 적용됨: True
```

항목 3의 정상 케이스(월요일 휴장 → 금요일 마감)는 `CURRENT_COMPLETE`가 나오는데,
여기서는 같은 4일 주인데도 `CURRENT_NORMALIZED`가 나온다. 분자에 5/4 = 1.25배가
곱해진다(분모도 부분 주 제외로 함께 움직이므로 최종 비율의 순증은 1.25보다 약간 작다).

공휴일로 인한 발생 빈도는 KR/US 합쳐 연 수 회 수준이고, 그날 하루만 BREAKOUT
후보가 과다 추출되는 방향(누락이 아니라 과다)이다. 캘린더 도입 없이 해결
불가하므로 **허용할지 여부는 리뷰 판단 사항**이다.

### 실측 — US 피드가 하루 지연되어 같은 경로를 상시로 밟는다

2026-08-29(토) 측정 결과, yfinance 는 직전 금요일(08-28) 행을 **거래량만 있고
OHLC 는 전부 NaN** 인 상태로 반환한다. `us_stocks.get_us_batch` 의 `.dropna()`
가 이 행을 제거하므로 스캐너가 보는 마지막 봉은 **목요일(08-27)** 이다.
전 종목 공통이다.

```bash
$PY - <<'EOF'
import yfinance as yf, pandas as pd
raw = yf.download(["AAPL","MSFT","NVDA"], period="1mo", auto_adjust=True,
                  group_by="ticker", threads=True, progress=False)
raw.index = pd.to_datetime(raw.index).tz_localize(None)
for s in ("AAPL","MSFT","NVDA"):
    d = raw[s][["Open","High","Low","Close","Volume"]]
    print(f"{s}: raw 마지막 {d.index[-1].date()} "
          f"OHLC전부결측={d.iloc[-1][['Open','High','Low','Close']].isna().all()} "
          f"| dropna 후 {d.dropna().index[-1].date()}")
EOF
```

이 상태로 US 스캔을 돌리면 마지막 주가 월~목 4거래일로 보이고 목요일 마감이라
`CURRENT_NORMALIZED` + ×5/4 가 걸린다. **공휴일보다 훨씬 잦은 경로**이므로
Step 2 이전에 판단이 필요하다. 확인:

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner.us_stocks import get_us_batch
from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators
# 주의: get_us_batch 는 단일 종목(len(syms)==1) 경로에서 None 을 반환하므로 2개 이상 넘긴다
tk = [{"ticker": "AAPL", "name": "Apple"}, {"ticker": "MSFT", "name": "Microsoft"}]
for info, df in get_us_batch(tk):
    if df is None:
        print(info["ticker"], "df None"); continue
    w = compute_weekly_indicators(to_weekly_ohlcv(df), df)
    print(f'{info["ticker"]}: 마지막 봉 {df.index[-1].date()} {df.index[-1].strftime("%a")}'
          f' | basis {w["week_volume_basis"]} | elapsed {w["week_elapsed_days"]}'
          f' | ratio {w["weekly_volume_ratio"]} (raw {w["weekly_volume_ratio_raw"]})')
EOF
```

**2026-08-29(토) 실측**
```
AAPL: 마지막 봉 2026-08-27 Thu | basis CURRENT_NORMALIZED | elapsed 4 | ratio 0.58 (raw 0.48)
MSFT: 마지막 봉 2026-08-27 Thu | basis CURRENT_NORMALIZED | elapsed 4 | ratio 0.56 (raw 0.47)
```

### 장중 미완성 봉 — 미판정

Codex P2 가 지적한 "금요일 장중 스캔" 은 **토요일 측정으로는 판정 불가**하다
(양 시장 휴장이라 미완성 봉이 존재할 수 없음). 아래를 **장중**(KR 09:00~15:30
KST / US 09:30~16:00 ET)에 재실행해야 결론이 난다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from datetime import datetime
from scanner.kr_stocks import get_kr_ohlcv
from scanner.us_stocks import get_us_batch
print("실행 시각:", datetime.now().strftime("%Y-%m-%d %H:%M %a"))
df = get_kr_ohlcv("005930")
print("KR 005930 마지막 봉:", df.index[-1].date(), "→ 당일이면 미완성 봉 포함")
for info, d in get_us_batch([{"ticker": "AAPL", "name": "Apple"},
                            {"ticker": "MSFT", "name": "Microsoft"}]):
    if d is not None:
        print(f'US {info["ticker"]} 마지막 봉:', d.index[-1].date(), "→ 당일이면 미완성 봉 포함")
EOF
```

마지막 봉 날짜가 **실행 당일** 이면 미완성 봉이 유입되는 것이고, 그 경우
`_current_week_state` 의 "금요일 = 완성" 규칙이 장중에 오판한다. 수정은 스캔
시각을 함수에 넘기는 설계 변경이 필요해 Step 1 범위 밖으로 보류한다.

---

## 4. 경계 전환 — elapsed 2→3에서 basis 전환 및 연속성

`PREVIOUS_COMPLETE` → `CURRENT_NORMALIZED`로 바뀌고, 그 지점에서 비율이
불연속적으로 튀지 않아야 한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

print(f"{'마감':<4}{'elapsed':>8}{'basis':>22}{'raw':>7}{'final':>7}")
vals = {}
for cut, label in ((0,"금"),(1,"목"),(2,"수"),(3,"화"),(4,"월")):
    days = _trading_days("2023-01-02", 80)
    days = days[:len(days)-cut] if cut else days
    df = _df_from_days(days)
    w  = compute_weekly_indicators(to_weekly_ohlcv(df), df)
    vals[w["week_elapsed_days"]] = w["weekly_volume_ratio"]
    print(f"{label:<4}{w['week_elapsed_days']:>8}{w['week_volume_basis']:>22}"
          f"{w['weekly_volume_ratio_raw']:>7}{w['weekly_volume_ratio']:>7}")
print("2→3 점프:", round(abs(vals[3] - vals[2]), 3))
EOF
```

**기대** — 거래량이 매일 일정한 합성 데이터이므로 모든 경로가 1.0으로 수렴하고,
경계에서 점프가 0이어야 한다.

```
마감  elapsed                 basis    raw  final
금          5      CURRENT_COMPLETE    1.0    1.0
목          4    CURRENT_NORMALIZED   0.82    1.0
수          3    CURRENT_NORMALIZED   0.62    1.0
화          2     PREVIOUS_COMPLETE   0.43    1.0
월          1     PREVIOUS_COMPLETE   0.22    1.0
2→3 점프: 0.0
```

pytest 노드: `tests/test_weekly_volume_basis.py::TestWeekBasisPaths` (4개 전부)

> `2→3 점프`가 0이 아니면 정규화 계수 또는 분모 시리즈가 두 경로에서
> 불일치한다는 뜻이다. 실데이터에서는 주별 거래량 편차 때문에 정확히 0이
> 되지 않지만, 합성 등량 데이터에서는 0이어야 한다.

---

## 5. 적용 범위 — 거래량 외 지표 불변

동일 입력에 대해 `sma30w` / `slope30w` / `classify_stage`가 변경 전후 완전히
동일해야 한다(= `daily_df` 전달 여부와 무관해야 한다).

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner.weinstein import (to_weekly_ohlcv, compute_weekly_indicators,
                               _build_indicators, classify_stage)

ok = True
for cut in (0, 1, 2, 3, 4):
    days = _trading_days("2023-01-02", 80)
    days = days[:len(days)-cut] if cut else days
    df = _df_from_days(days)
    weekly, d = to_weekly_ohlcv(df), _build_indicators(df)
    before, after = compute_weekly_indicators(weekly), compute_weekly_indicators(weekly, df)
    same = (before["cur_sma30w"]  == after["cur_sma30w"]
            and before["cur_sma10w"] == after["cur_sma10w"]
            and before["slope30w"]   == after["slope30w"]
            and before["cur_close_w"] == after["cur_close_w"]
            and classify_stage(before, d) == classify_stage(after, d))
    print(f"cut={cut} 동일={same} basis={after['week_volume_basis']}")
    ok &= same
print("ALL SAME:", ok)
EOF
```

**기대**: 모든 줄 `동일=True`, 마지막 줄 `ALL SAME: True`.

pytest 노드: `tests/test_weekly_volume_basis.py::TestScopeLimitedToVolume::test_stage_and_sma_untouched_by_basis_policy`

> 하나라도 False면 작업 2("거래량에만 적용") 위반이다.

---

## 6. strict_* 스냅샷 훼손 여부

`analyze_stock` 반환 dict에서 `strict_price != price`인 케이스가 여전히
재현되어야 한다. strict_* 분리는 look-ahead bias 방지용 의도적 설계이므로
Step 1이 이를 무너뜨리면 안 된다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from tests.test_weinstein import _make_df, _make_stage2_base
from scanner.weinstein import analyze_stock

prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
bi = len(prices) - 5
prices[bi], volumes[bi] = 104.0, 6_000_000
for i in range(bi + 1, len(prices)):
    prices[i], volumes[i] = 110.0, 1_000_000

res = analyze_stock(_make_df(prices, volumes), "TEST", "테스트", "US")
print("signal_date :", res["signal_date"], "(last bar 이전이어야 함)")
print("price       :", res["price"], "  strict_price:", res["strict_price"])
print("분리 유지   :", res["price"] != res["strict_price"])
print("ma150 분리  :", res["ma150"] != res["strict_ma150"])
EOF
```

**기대**
```
signal_date : 2022-08-14 (last bar 이전이어야 함)
price       : 110.0   strict_price: 104.0
분리 유지   : True
ma150 분리  : True
```

pytest 노드: `tests/test_weinstein.py::TestNoLookAhead::test_strict_gate_inputs_at_signal_date`

### 이 테스트에서 변경된 2줄

Step 1이 `analyze_stock` 내부에서 `compute_weekly_indicators(weekly, daily_df)`를
호출하므로, 테스트가 비교 기준으로 쓰는 레퍼런스도 같은 인자를 넘기도록
2줄 수정했다.

```python
w_sig  = compute_weekly_indicators(w_sig_df, df_sig) if len(w_sig_df) > 0 else None
w_last = compute_weekly_indicators(to_weekly_ohlcv(df), df)
```

**단언문 자체는 하나도 바뀌지 않았다.** 리뷰 시 `git diff tests/test_weinstein.py`로
확인할 것 — assert 라인이 수정됐다면 invariant를 약화시킨 것이다.

---

## 7. AS_GATE 토글 — env만으로 동작이 갈리는지

```bash
BREAKOUT_WEEKLY_VOL_AS_GATE=true  $PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
import config; from scanner import weinstein
from tests.test_weinstein import _make_df, _make_stage2_base
from scanner.weinstein import _build_indicators, to_weekly_ohlcv, compute_weekly_indicators

p, v = _make_stage2_base(n_total=230, base_price=100.0)
p[-1], v[-1] = 104.0, 6_000_000
df = _make_df(p, v)
d  = _build_indicators(df)
w  = dict(compute_weekly_indicators(to_weekly_ohlcv(df), df))
w["weekly_volume_ratio"] = 0.5          # 임계값(2.0) 미달로 강제
print("AS_GATE =", weinstein.BREAKOUT_WEEKLY_VOL_AS_GATE)
print("결과     =", weinstein.detect_stage2_breakout(df, w, d))
EOF
```

**기대**: `AS_GATE = True`, `결과 = None`

같은 스크립트를 `BREAKOUT_WEEKLY_VOL_AS_GATE=false`로 실행하면
`AS_GATE = False`이고 결과가 `None`이 아닌 dict이며, 그 dict의
`warning_flags`에 `주봉 거래량 미달 (0.50 < 2.0)`이 들어 있어야 한다.

pytest 노드: `tests/test_weekly_volume_basis.py::TestWeeklyVolumeGateToggle::test_gate_true_blocks_and_gate_false_warns`

> **임계값 2.0은 이번에 바뀌지 않았다.** 확인:
> `grep -n "BREAKOUT_WEEKLY_VOL_RATIO" config.py` → `"2.0"` 이어야 한다.

---

## 7-b. AS_GATE 가 strict filter(Gate 5)까지 닿는지 — Codex 리뷰 P1

항목 7은 탐지기(`detect_stage2_breakout`)만 검증한다. 탐지기를 통과시켜도
`strict_filter._check_volume` 이 같은 임계값으로 `breakout_weekly_volume` 을
붙이면 후보가 결국 차단되어 **env A/B 실험이 성립하지 않는다.**

동일 signal dict 에 대해 토글만 바꿔 사유 유무가 갈려야 한다.

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner import strict_filter
from scanner.strict_filter import _check_volume, BREAKOUT_WEEKLY_VOLUME

strict_filter.STRICT_REQUIRE_BREAKOUT_VOLUME = True
strict_filter.BREAKOUT_DAILY_VOL_RATIO  = 3.0
strict_filter.BREAKOUT_WEEKLY_VOL_RATIO = 2.0
SIG = {"signal_type": "BREAKOUT",
       "volume_ratio": 3.5,                 # 일봉 통과
       "strict_weekly_volume_ratio": 1.0}   # 주봉 미달

for gate in (True, False):
    strict_filter.BREAKOUT_WEEKLY_VOL_AS_GATE = gate
    reasons = []
    _check_volume(dict(SIG), reasons)
    print(f"AS_GATE={gate:<5} reasons={reasons} "
          f"| breakout_weekly_volume 포함={BREAKOUT_WEEKLY_VOLUME in reasons}")
EOF
```

**기대**
```
AS_GATE=1     reasons=['breakout_weekly_volume'] | breakout_weekly_volume 포함=True
AS_GATE=0     reasons=[]                         | breakout_weekly_volume 포함=False
```

pytest 노드:
`tests/test_weekly_volume_basis.py::TestGateToggleReachesStrictFilter::test_gate_true_keeps_weekly_volume_reason`
`::test_gate_false_drops_weekly_volume_reason`
`::test_daily_volume_condition_ignores_toggle` (일봉 조건은 토글과 무관해야 함)

### 전 구간 e2e (탐지기 + strict filter 동시)

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner import weinstein
from scanner.weinstein import analyze_stock
from scanner.strict_filter import apply_strict_filter
from tests.test_weinstein import _make_df, _make_stage2_base

p, v = _make_stage2_base(n_total=230, base_price=100.0)
p[-1], v[-1] = 104.0, 1_800_000        # 일봉 3.2배(통과), 주봉 1.54(미달)
res = analyze_stock(_make_df(p, v), "T", "테스트", "US", market_condition="BULL")
print("AS_GATE =", weinstein.BREAKOUT_WEEKLY_VOL_AS_GATE, "| signal =", res["signal_type"])
_, reasons = apply_strict_filter(dict(res), {"market_condition": "BULL",
                                             "sector_stage": None,
                                             "benchmark_present": False})
print("filter_reasons =", reasons)
EOF
```

**기대** — `BREAKOUT_WEEKLY_VOL_AS_GATE=true` 로 실행하면
`signal = RE_BREAKOUT` (주봉 게이트가 BREAKOUT 을 막아 다음 탐지기로 넘어감),
`false` 로 실행하면 `signal = BREAKOUT` 이고 `filter_reasons` 에
`breakout_weekly_volume` 이 **없어야** 한다.

---

## 7-c. _REJECT_RANK 가 실제 체크 순서와 일치하는지 — Codex 리뷰 P2

```bash
$PY - <<'EOF'
import sys, os; sys.path.insert(0, os.getcwd())
from scanner.weinstein import _REJECT_RANK as R

ORDERS = {
 "_find_rebreakout_signal": ["daily_stage_not_2","no_ma150","price_below_ma150",
   "price_below_ma50","ma150_not_rising","base_too_short","pullback_too_shallow",
   "base_too_wide","no_pivot_breakout","daily_volume_insufficient","no_volume_dryup"],
 "_find_rebound_signal": ["daily_stage_not_2_or_3","ma150_not_rising",
   "no_rebound_touch","no_rebound_confirm","daily_volume_insufficient","rebound_too_old"],
 "detect_stage2_breakout": ["no_weekly_data","weekly_stage_not_1_or_2",
   "weekly_volume_insufficient","base_too_short","no_pivot_breakout",
   "extension_too_high","daily_volume_insufficient"],
}
ok = True
for det, order in ORDERS.items():
    ranks = [R[r] for r in order]
    inc = all(ranks[i] > ranks[i-1] for i in range(1, len(ranks)))
    print(f"{det:<26} 단조증가={inc}  {ranks}")
    ok &= inc
print("ALL OK:", ok)
EOF
```

**기대**: 세 줄 모두 `단조증가=True`, 마지막 `ALL OK: True`.

pytest 노드: `tests/test_weekly_volume_basis.py::TestRejectRankMatchesCheckOrder`
(3개 — 순서 단조성 / 지목된 두 역전 해소 / BREAKOUT 무영향)

> BREAKOUT 경로는 `weekly_volume_insufficient` 가 bar 루프 **밖** 단독 체크라
> 경쟁 사유가 없다. 따라서 랭크 재정렬로 BREAKOUT funnel 수치는 바뀌지 않고,
> RE_BREAKOUT / REBOUND 수치만 바뀐다.

---

## 8. funnel 진단 확장

스캔 결과 JSON에 `week_basis`와 `weekly_vol_ratio_stats`가 채워지는지.

```bash
$PY - <<'EOF'
import sys, os, json, types; sys.path.insert(0, os.getcwd())
from tests.test_weekly_volume_basis import _trading_days, _df_from_days
from scanner import scan_engine

rows = []
for i, cut in enumerate((0, 0, 1, 2, 2, 3, 4)):
    days = _trading_days("2023-01-02", 80)
    days = days[:len(days)-cut] if cut else days
    rows.append(({"ticker": f"W{i}", "name": f"주{i}"}, _df_from_days(days)))

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
print(json.dumps({k: funnel[k] for k in
                  ("total_scanned", "week_basis", "weekly_vol_ratio_stats")},
                 ensure_ascii=False, indent=2, sort_keys=True))
print("내부 키 누출:", "_wvr_samples" in funnel)
EOF
```

**기대**
```json
{
  "total_scanned": 7,
  "week_basis": {
    "CURRENT_COMPLETE": 2,
    "CURRENT_NORMALIZED": 3,
    "PREVIOUS_COMPLETE": 2
  },
  "weekly_vol_ratio_stats": { "max": 1.0, "median": 1.0, "n": 7, "p90": 1.0 }
}
내부 키 누출: False
```

`weekly_vol_ratio_stats`의 모수(`n`)는 **BREAKOUT 탐지기가 실제로 임계값과
비교한 종목** 수다(= `detect_stage2_breakout`이 주봉 거래량 분기까지 도달).
주봉 데이터 없음/Stage 미달로 그 전에 탈락한 종목은 제외된다.

이 스크립트는 `logs/funnel_US_*.json`을 남기므로 확인 후 지울 것.

---

## 리뷰 판단이 필요한 지점 요약

| # | 사안 | 상태 |
|---|---|---|
| 2 | `CURRENT_NORMALIZED` 분모에도 부분 주 제외를 확장 | 지시서 범위 밖, 의도적 판단 |
| 3 | 금요일 휴장 주의 목요일 마감 → ×1.25 과대평가 | 캘린더 없이 해결 불가, 미해결 |
| 6 | `TestNoLookAhead` 레퍼런스 2줄 수정 | 단언문은 불변 |
| 3 | US 피드 하루 지연 → 상시 `CURRENT_NORMALIZED` ×5/4 | 실측 확인, 미해결 |
| 3 | 장중 미완성 봉 유입 여부 | 토요일 측정으로 판정 불가, 장중 재측정 필요 |
