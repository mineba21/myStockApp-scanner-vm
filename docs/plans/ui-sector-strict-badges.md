# UI: Sector + Strict Badges + Filter Reasons + Rejected Opt-In — 구현 계획

> 최종 위치: `stock-scanner/docs/plans/ui-sector-strict-badges.md` (구현 첫 커밋에서 이 파일로 복사 후 커밋)
> 브랜치: `ui-sector-strict-badges` (기준 = `main` 또는 strict-weinstein-phase-4 머지 후 main)

---

## Context

Strict Weinstein 백엔드는 Phase 4 + P2 follow-up까지 머지/리뷰 통과 상태다. `/api/results` 는 이미:

- 기본 응답에서 `strict_filter_passed=False` 행 제외
- `include_rejected=true` opt-in 으로 거부 행 포함
- 응답 dict에 `strict_filter_passed`(bool|null), `filter_reasons`(list[str]) 노출
- DB 컬럼 `sector_name` 정의됨(현재 항상 NULL — sector 매핑은 별도 plan)

UI(`web/templates/index.html`, Alpine.js v3 + Tailwind CSS, 단일 인라인 파일 ~1700줄)는 이 변화를 시각화하지 못한다. 사용자가 strict 통과 여부를 카드에서 즉시 식별하지 못하고, 향후 sector 매핑이 들어와도 노출 자리가 없다. 또 `include_rejected` opt-in이 백엔드에 있어도 프론트가 호출하지 않아 QA가 거부 행을 볼 방법이 없다.

이 계획은 백엔드 계약 변화 없이(예외: `sector_name` 응답 필드 추가) UI 만 4가지를 처리한다 — sector 배지, strict 상태 배지, filter_reasons 표시, 거부 행 opt-in.

### 사용자 결정 (2026-05-03)

1. **거부 카드 액션 버튼**: 매수/감시 버튼을 **disabled + 회색** 처리. 차트 버튼은 활성. 인스펙트는 가능하되 매수 동선 진입은 원천 차단.
2. **PR 단위**: **단일 PR** (논리 커밋 2~3개로 나눌 수 있음). 변경량 ~120줄로 리뷰 부담 낮음.
3. **`include_rejected` 토글 영속화**: **세션 한정**. 새로고침마다 OFF로 리셋. localStorage 미사용.

---

## Current-State Summary

| 항목 | 현재 상태 | 위치 |
|---|---|---|
| `/api/results` strict 필드 노출 | ✅ `strict_filter_passed`, `filter_reasons` | `web/app.py:84-117` |
| `/api/results` sector_name 노출 | ❌ 응답 dict 누락 | `web/app.py:108-117` |
| `/api/results` include_rejected 분기 | ✅ 백엔드 OK, 프론트 미호출 | `web/app.py:99-106` |
| UI 카드 배지군 | ⚠️ 시장/signal_type/RS 만, strict/sector 없음 | `index.html:288-301` |
| UI 거부 행 시각 차등 | ❌ 부재 (그리고 백엔드가 막아 거부 자체 미도달) | — |
| UI filter_reasons 표시 | ❌ 부재 | — |
| UI include_rejected 토글 | ❌ 부재 | — |
| UI 거부 행 매수/감시 버튼 가드 | ❌ 부재 (현재는 거부 행이 안 보여 잠재 위험) | `index.html:329-336` |
| `filter_reasons` 한국어 매핑 | ❌ 부재 | — |

`scanner/strict_filter.py` 의 reason enum 23종 (이미 정의됨):

```
market_bear, market_unknown, market_caution_breakout,
sector_stage4, sector_not_stage2,
weekly_data_missing, below_weekly_30ma, below_daily_150ma,
stage_stage3, stage_stage4, weekly_30ma_slope_negative,
base_insufficient, base_too_wide, rebound_no_retest,
breakout_daily_volume, breakout_weekly_volume,
rs_below_zero, rs_falling, rs_benchmark_missing, rs_no_zero_cross,
extended_above_ma150, extended_above_30w,
stop_loss_missing, stop_loss_above_price
```

UI 매핑 객체는 이 23개를 ground truth 로 따라간다.

---

## Goals

1. 카드에 sector 배지 자리 마련 (현재 NULL → 미표시, 후속 sector 매핑 plan 머지 시 자동 활성).
2. strict 통과/거부/legacy 3 상태를 카드 한눈에 식별 가능.
3. 거부 카드에서 reason enum을 한국어 라벨로 펼쳐서 인스펙트.
4. 거부 행은 명시적 opt-in 토글(세션 한정) 켤 때만 노출. 토글 ON 이어도 매수/감시 동선은 원천 차단.
5. 백엔드 회귀 0 — 단일 응답 필드(`sector_name`) 추가만, 기존 키 보존.

## Non-Goals

- **종목당 sector 매핑 구현** (yfinance.info / KRX 업종) — 별도 plan.
- **컴포넌트화 / 빌드 시스템 도입** — Alpine + Tailwind CDN 유지(CLAUDE.md 권장).
- **localStorage 영속화** — 사용자 결정 3 에 따라 세션 한정.
- **Playwright/Jest 도입** — 인라인 JS 규모 대비 비용 과다, 수동 QA 체크리스트 대체.
- **stats 카드 거부 분리 표시** — `stats.buy` 등은 strict-pass 의미 그대로 유지(거부 토글 ON 이어도 stats는 pass만 카운트).
- **차트 모달의 strict 정보 표시** — 본 plan 범위 외(필요 시 별도).

---

## Files to Change

| 파일 | 역할 | 변경 |
|---|---|---|
| `stock-scanner/web/app.py` | `/api/results` 응답 dict | `"sector_name": r.sector_name` 한 줄 추가 (~108-117줄) |
| `stock-scanner/web/templates/index.html` | UI 전체 | ① 필터 행에 토글 ② 카드 배지군 확장 ③ 거부 카드 시각 차등 ④ filter_reasons 펼치기 ⑤ 매수/감시 버튼 가드 ⑥ JS `filter` 객체에 `include_rejected` ⑦ `loadResults`/`deleteAllResults` URL 조립 ⑧ `REASON_LABELS_KO` 매핑 객체 |
| `stock-scanner/tests/test_results_api.py` | API 회귀 | `sector_name` 노출 테스트 1 케이스 추가 |
| `stock-scanner/docs/plans/ui-sector-strict-badges.md` | 본 문서 | 신규 디렉토리(`docs/plans/`) 생성 + 본 계획 사본 커밋 |

DB 스키마 / `_migrate()` / 테스트 픽스처 변경 없음.

---

## UI 설계

### A. Sector 배지 (요구사항 1)

- **위치**: 카드 상단 배지군 끝(RS 배지 다음). `index.html:288-301` 의 마지막 `<template x-if>` 다음.
- **렌더 조건**: `sector_name` 이 truthy 일 때만. NULL 이면 배지 자체 미렌더.
- **클래스**: `text-xs px-2 py-0.5 rounded-full font-semibold bg-indigo-50 text-indigo-700`.
- **표현**: `<template x-if="r.sector_name"><span class="..." x-text="r.sector_name"></span></template>`.
- 색상은 indigo — 시장(red/blue), signal(emerald/purple/blue), RS(orange/slate), strict(emerald/red/slate)와 충돌 없음.

### B. Strict 상태 배지 (요구사항 2)

| 상태 | 값 | 라벨 | 클래스 |
|---|---|---|---|
| pass | `true` | `✓ Strict` | `bg-emerald-100 text-emerald-700` |
| reject | `false` | `✗ 거부` | `bg-red-100 text-red-700` |
| legacy | `null` | `Strict —` | `bg-slate-100 text-slate-500` |

- **위치**: signal_type 배지 바로 뒤(RS 앞). 정보 우선순위 = 신호종류 → strict 통과여부 → RS.
- **이중 신호**: 색상 + 아이콘(`✓ / ✗ / —`). 색맹 사용자도 식별 가능.
- **카드 전체 시각 차등 (거부만)**: `:class` 에 조건부 `'opacity-60 ring-1 ring-red-200'` 추가. 기존 `sig-b/sig-r/RE_BREAKOUT` 그라디언트는 유지(중복 시그널). 삭제 X 버튼은 그대로 노출.

### C. filter_reasons 펼치기 (요구사항 3)

- **매핑 위치**: `index.html` 의 `app()` 객체 안 `REASON_LABELS_KO` 상수. 백엔드 enum 은 stable identifier 로 둠 → 라벨 변경 시 백엔드 재배포 불필요.
- **매핑 예시** (23개 전부 정의):
  ```js
  const REASON_LABELS_KO = {
    market_bear:               '시장 BEAR — 신규 매수 차단',
    market_unknown:            '시장 데이터 부재',
    market_caution_breakout:   'CAUTION 시장에서 돌파 차단',
    sector_stage4:             '섹터 Stage 4 (하락)',
    sector_not_stage2:         '섹터 Stage 2 아님',
    weekly_data_missing:       '주봉 데이터 부재',
    below_weekly_30ma:         '주봉 30MA 미달',
    below_daily_150ma:         '일봉 150MA 미달',
    stage_stage3:              '종목 Stage 3 (분배)',
    stage_stage4:              '종목 Stage 4 (하락)',
    weekly_30ma_slope_negative:'30주 MA 기울기 음수',
    base_insufficient:         'Base 기간 부족',
    base_too_wide:             'Base 폭 과대 (>15%)',
    rebound_no_retest:         '리테스트 없는 반등',
    breakout_daily_volume:     '돌파일 일봉 거래량 미달',
    breakout_weekly_volume:    '돌파주 주봉 거래량 미달',
    rs_below_zero:             'Mansfield RS < 0',
    rs_falling:                'RS 추세 하락',
    rs_benchmark_missing:      'RS 벤치마크 부재',
    rs_no_zero_cross:          'RS 0선 음→양 전환 없음',
    extended_above_ma150:      'MA150 +15% 초과 과열',
    extended_above_30w:        '30주 MA 대비 30%+ 과열',
    stop_loss_missing:         '손절가 산출 실패',
    stop_loss_above_price:     '손절가가 현재가 이상(비정상)',
  };
  function reasonLabel(key) { return REASON_LABELS_KO[key] || key; }
  ```
- **표시 방식**: 거부 카드에서만 `📋 거부 사유 N개 ▾` 토글 버튼을 배지군 아래에 배치. 클릭 시 액션 버튼 위에 칩 리스트로 펼침. `r._reasonsOpen` 로컬 플래그(Alpine 객체에 직접 토글).
- **칩 클래스**: `bg-red-50 text-red-700 text-[11px] px-1.5 py-0.5 rounded`. 펼침/접힘 외 다른 카드 영역은 그대로.
- **대안 검토**: 툴팁(모바일 접근성 ↓) / 모달(비교 워크플로 ↓) / 항상 펼침(그리드 정렬 깨짐) → 모두 기각.

### D. include_rejected opt-in (요구사항 4)

- **위치**: 필터 행(`index.html:201-227`) 끝, "↺ 새로고침" 버튼 바로 뒤. 일괄 삭제 버튼은 더 우측(`ml-auto`).
- **컨트롤 형태**: 체크박스 + 라벨 `👁 거부 신호 보기 (QA)`.
- **상태**: `filter.include_rejected: false` (Alpine `data()` 초기값). 새로고침 시 OFF 리셋(localStorage 미사용).
- **ON 시 추가 시각 표시**: 결과 그리드 상단에 작은 안내 배너(`bg-amber-50 text-amber-700 text-xs px-3 py-2 rounded-md`) — `"QA 모드: 거부 신호 N건 포함"`. N은 `results.filter(r => r.strict_filter_passed === false).length`.
- **`loadResults()` URL 조립**:
  ```js
  const inc = this.filter.include_rejected ? '&include_rejected=true' : '';
  fetch(`/api/results?market=${m}&signal_type=${st}&days=${d}&limit=200${inc}`)
  ```
- **`deleteAllResults()` URL 동일 패턴**. 일괄 삭제 모달 요약(`index.html:238-254`)에 `<div>거부 행 포함</div>` 한 줄 조건부 추가.
- **stats 카운트 정책**: `stats.buy` 등은 **strict-pass 만** 카운트 유지(`results.filter(r => r.strict_filter_passed !== false).length`). 토글 ON 으로 거부가 결과에 섞여도 stats는 pass 의미를 보전.

### E. 거부 카드 매수/감시 버튼 가드

- `+ 매수` / `👁 감시` 버튼에 `:disabled="r.strict_filter_passed === false"` + `:class` 조건부 회색.
- 비활성 시 클래스: `'opacity-50 cursor-not-allowed pointer-events-none'`. hover 색 변화 없음.
- `📈 차트` 버튼은 무조건 활성 — 인스펙트 가능.
- `× 삭제` 버튼도 활성 — QA 정리용.
- **백엔드 가드 부재 보존**: `/api/watchlist` 자체에는 strict 검증 없음. UI 차단으로 충분(직접 API 호출은 운영자만, 검토 후).

---

## Backend 변경

`web/app.py:108-117` 의 `/api/results` 응답 dict에 한 줄 추가:

```python
return [{"id": r.id, ..., "filter_reasons": _parse_filter_reasons(r.filter_reasons),
         "sector_name": r.sector_name}  # ← 추가
        for r in rows]
```

`_parse_filter_reasons` 헬퍼 변경 없음. 기존 키 보존 — 후방 호환.

---

## JS 상태 / 동작

### `app()` 객체 변경

```js
// 기존
filter: {market:'ALL', signal_type:'ALL', days:7},

// 변경 후
filter: {market:'ALL', signal_type:'ALL', days:7, include_rejected:false},
```

상수 `REASON_LABELS_KO` 와 `reasonLabel()` 헬퍼는 `app()` 함수 외부 모듈 스코프에 둠(여러 인스턴스에서 재사용 가능).

### 거부 사유 펼치기 상태

각 결과 객체에 `_reasonsOpen` 임시 플래그를 부여:

```js
async loadResults() {
  ...
  const data = await (await fetch(url)).json();
  this.results = data.map(r => ({...r, _reasonsOpen: false}));
  ...
}
```

토글:
```html
<button @click="r._reasonsOpen = !r._reasonsOpen" x-show="r.strict_filter_passed === false && (r.filter_reasons||[]).length">
  📋 거부 사유 <span x-text="r.filter_reasons.length"></span>개
  <span x-text="r._reasonsOpen ? '▴' : '▾'"></span>
</button>
```

### Alpine 한계

`index.html` 이 1700줄 넘음. 본 PR 추가량 ~80~120줄. 한계점 근접. 별도 모듈화 PR(예: `index.js` 분리)은 본 plan 범위 외 — 별도 spawn 권장.

---

## DB / 스키마 영향

**없음**. `sector_name` / `strict_filter_passed` / `filter_reasons` 컬럼은 Phase 1에서 이미 추가됨. 마이그레이션 불필요.

---

## Test Plan

### 백엔드 회귀 (1 케이스 추가)

`tests/test_results_api.py::TestResultsRejectedFilter` 에:

```python
def test_response_exposes_sector_name(self, client_with_db):
    """API 응답이 sector_name 을 명시적으로 노출 (NULL 도 명시)."""
    client, session = client_with_db
    _insert_result(session, ticker="A", strict_filter_passed=True)  # sector_name=None
    # 직접 sector_name 채운 행 추가 — _insert_result 헬퍼 확장 또는 raw insert
    ...
    r = client.get("/api/results")
    rows = {row["ticker"]: row for row in r.json()}
    assert "sector_name" in rows["A"]
    assert rows["A"]["sector_name"] is None
```

`_insert_result` 헬퍼에 `sector_name=None` kwarg 추가.

### 프론트엔드 — 수동 QA 체크리스트

DB 시드 (3 행):
- 행 1: strict_filter_passed=True, sector_name=NULL, filter_reasons=NULL
- 행 2: strict_filter_passed=False, filter_reasons=`["rs_below_zero","stop_loss_missing"]`, sector_name=NULL
- 행 3: strict_filter_passed=NULL (legacy), sector_name="Technology"

체크 항목:
1. ✅ 페이지 로드 직후 `include_rejected=false` (체크박스 OFF). 행 1, 행 3만 노출. 행 2 미노출.
2. ✅ 행 1 카드: `✓ Strict` 에메랄드 배지. sector 배지 미렌더.
3. ✅ 행 3 카드: `Strict —` 슬레이트 배지. sector 배지 = "Technology" indigo.
4. ✅ 토글 ON → 행 2 등장. 안내 배너 "QA 모드: 거부 신호 1건 포함" 표시.
5. ✅ 행 2 카드: `✗ 거부` 빨강 배지, opacity-60, ring-red-200. `📋 거부 사유 2개 ▾` 버튼 보임.
6. ✅ 거부 사유 토글 클릭 → "Mansfield RS < 0", "손절가 산출 실패" 칩 펼침.
7. ✅ 행 2 의 `+ 매수` / `👁 감시` 버튼 = disabled + 회색. 클릭 무반응. `📈 차트` 활성.
8. ✅ 일괄 삭제 (토글 OFF) → 행 1, 3만 삭제. 행 2 보존(DB 직접 조회).
9. ✅ 일괄 삭제 (토글 ON) → 행 2 도 삭제. 모달에 "거부 행 포함" 표시.
10. ✅ 새로고침 → 토글 OFF 리셋(세션 한정).
11. ✅ 색맹 시뮬레이션(브라우저 확장)에서 ✓/✗/— 아이콘으로 식별 가능.

### 컴포넌트 단위 테스트

도입 안 함. 인라인 JS 규모 대비 비용 과다. 별도 모듈화 PR에서 재검토.

---

## Verification

```bash
cd stock-scanner

# 1. 컴파일 + 테스트
venv/bin/python -m compileall scanner database web
venv/bin/python -m pytest tests/ -q
# 기대: 199 passed (198 + 1 sector_name)

# 2. 로컬 서버 + 시드 + 수동 QA
venv/bin/python -m sqlite3 stock_scanner.db <<'SQL'
INSERT INTO scan_results (scan_time, market, ticker, name, signal_type, stage,
  price, ma150, volume_ratio, signal_date, strict_filter_passed, filter_reasons, sector_name)
VALUES
  (datetime('now'), 'US', 'PASSCO',  'Pass Inc.',   'BREAKOUT', 'STAGE2', 100, 90, 2.5, date('now'), 1, NULL, NULL),
  (datetime('now'), 'US', 'REJCO',   'Reject Inc.', 'BREAKOUT', 'STAGE2', 100, 90, 2.5, date('now'), 0,
    '["rs_below_zero","stop_loss_missing"]', NULL),
  (datetime('now'), 'KR', 'LEGACYCO','Legacy Inc.', 'REBOUND',  'STAGE2', 80000, 75000, 1.8, date('now'), NULL, NULL, 'Technology');
SQL

uvicorn web.app:app --reload --port 8000
# → 브라우저 http://localhost:8000 → §Test Plan 11개 체크
```

---

## Phasing / Branch Strategy

**브랜치**: `ui-sector-strict-badges` (단일).
**기준**: `main` 분기. (strict-weinstein-phase-4 PR이 main 머지 전이라면 그 머지 후 rebase.)

**논리 커밋**:
1. `feat(api): /api/results 에 sector_name 응답 필드 추가 + 회귀 테스트`
2. `feat(ui): sector / strict 배지 + 거부 행 시각 차등 + 매수·감시 가드`
3. `feat(ui): include_rejected 토글 + filter_reasons 펼치기 + 한국어 라벨 매핑`

세 커밋 모두 한 PR. 리뷰어가 커밋 단위로 보면 영향 범위 파악 쉬움. 변경량 ~120줄.

---

## Risks

1. **인라인 JS 비대화**: 본 PR ~80-120줄 추가. `index.html` 한계점 근접. 완화: 별도 spawn(`Refactor index.html into ES module bundle`).
2. **거부 행 페이지 성능**: `limit=200` 으로 가드. 카드 렌더 비용 작음. 가상화 불필요.
3. **UX 충격**: 백엔드는 이미 strict-pass 만 보내므로 결과 건수 변화 없음. UI 가 처음으로 "✓ Strict" 강조 — 머지 노트에 "사용자 가시성 변화: strict 배지 도입" 명기.
4. **색맹 접근성**: 모든 배지에 ✓/✗/— + 텍스트 라벨 동시. 회귀 시 색만으로 구분되지 않게 디자인 가드.
5. **localStorage 누락 회귀**: 사용자 결정대로 세션 한정. 운영자가 매일 토글 켜는 마찰 감수. 재고 시 별도 plan.
6. **REASON_LABELS_KO 드리프트**: 백엔드 reason enum 과 매핑 누락 위험. `reasonLabel(key)` 헬퍼가 미매핑 키는 raw enum 그대로 반환 → graceful degrade. PR description 에 "신규 reason 추가 시 라벨도 동시 업데이트" 명기.
7. **stats 카운트 의미**: `stats.buy = pass + legacy` 유지. 토글 ON 시 사용자가 거부 행을 stats 에 포함이라 오해할 수 있으나, 안내 배너로 충분히 분리.
8. **거부 카드의 매수/감시 직접 호출 가능성**: API 자체엔 가드 없음. UI 만 차단. 위협 모델 = 운영자 콘솔 직접 호출 → 별도 plan 필요 시 추가. 본 plan 범위 외.

---

## Rollback

브랜치 단위 revert. 백엔드 한 줄 추가는 후방 호환 — `sector_name` 키가 사라져도 UI 가 graceful degrade(NULL 취급).

긴급 hotfix: UI만 revert, 백엔드 변경은 유지(테스트 영향 없음).

---

## Critical Files

| 경로 | 역할 |
|---|---|
| `stock-scanner/web/templates/index.html` | UI 전체 — 배지/토글/사유/JS 상태 |
| `stock-scanner/web/app.py` | `/api/results` 응답에 `sector_name` 추가 |
| `stock-scanner/tests/test_results_api.py` | sector_name 회귀 테스트 |
| `stock-scanner/scanner/strict_filter.py` | reason enum 23종 ground truth — `REASON_LABELS_KO` 작성 시 참조 |
| `stock-scanner/database/models.py` | `sector_name` / `strict_filter_passed` / `filter_reasons` 컬럼 정의 — 변경 없이 참조용 |
| `stock-scanner/docs/plans/ui-sector-strict-badges.md` | 본 계획 사본 (구현 첫 커밋에서 생성) |
