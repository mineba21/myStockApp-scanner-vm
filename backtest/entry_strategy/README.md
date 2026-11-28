# account1 미국 Scanner 자동매수 — 진입 전략 백테스트

매도 규칙은 운영 S01 Scanner Stop(고정 손절가, 시간 청산 없음)으로 고정하고, **매수 조건만** 비교한다.
로컬 백테스트·설계 전용. 운영 VM / 운영 S01 코드 / 운영 DB·저널 / 자동손절 migration / 실주문 / push·배포와 무관하다.

## 실행

```bash
.venv/bin/python -m backtest.entry_strategy.run all       # P0~P6 전체 (수 분, 무작위 기준선 46,000회 포함)
.venv/bin/python -m backtest.entry_strategy.run posthoc   # 사후 탐색 (채택 근거 아님) + 권장 JSON 갱신
.venv/bin/python -m backtest.entry_strategy.run p7        # P7 Heat·위험 예산 추가 검증 (등록 282회 + B02 무작위 30,000회, 약 4분)
.venv/bin/python -m backtest.entry_strategy.run report    # output/entry_strategy_backtest_report.html
.venv/bin/python -m pytest backtest/entry_strategy/tests -q
```

입력: `backtest/exit_strategy/data`의 시세 캐시와 RECONSTRUCTED replay 신호(재사용). `data/`, `output/`은 재생성 가능하며 git에서 제외한다.

## 구성

| 파일 | 역할 |
|---|---|
| `configs/entry_backtest.yaml` | 사전 등록: 비용, 주문 규칙, 보유기간 H60/H120/UNL, 프로필 P_A/P_B/P_C, 분할, 채택 기준, 실험 목록 |
| `src/data.py` | 시세·시장 상태·결정 시점 특징량·경고 플래그, 탐지일 기준 분할 |
| `src/fills.py` | T0/T1_050/T1_100/T2/T3 체결 모델, S01 청산 경로 (같은 봉 보수/대안) |
| `src/portfolio.py` | 일별 포트폴리오 (운영 `scanner.sizing.calculate_position` 호출, Heat current/entry) |
| `src/strategies.py` | 후보군·시장 필터·Grade 순위·점수제(Train 적합) |
| `src/pipeline.py`, `src/experiments.py` | 신호 단위 연구, 등록부 실행, Validation 선택, Walk-forward, 무작위 기준선, 판정, 사후 탐색 |
| `src/risk_portfolio.py`, `src/p7.py` | P7: Entry/Current Heat 분리, 미체결 예약, 현금 하한, 손절선 이탈·손절가 없는 보유, 차단 사유·집중도 |
| `src/recommend.py`, `src/report.py`, `src/report_template.html` | 권장 조건 JSON, 단일 파일 HTML 보고서 |
| `docs/AUTO_BUY_S01_LINK_DESIGN.md` | 자동매수 체결 ↔ S01 원장 연동 설계 v2 (위험 예산·A1~A12, 코드 변경 없음) |
| `docs/AUTO_BUY_SHADOW_PLAN.md` | P8 자동매수 SHADOW 구현 Plan (검토안) |
| `docs/STEP_LOG.md` | 단계별 기록·수치·한계 |

## 결론 (요약)

- 사전 등록 선택 규칙 `F1 · SPY>MA50 · Grade · Top-1` (P_C, H60, T1_100): **NOT_RECOMMENDED**. Test 수익률 −0.5%, 기준선은 +1.0%. PF는 0.81, 기준선은 1.16.
- Grade A/B, STRONG/MODERATE는 신호 유형과 시장을 통제하면 차이가 없다. Auto Buy Score는 근거가 없다.
- 사후 후보 `Strict + BULL + 명백 위험 제외 + Grade Top-2`: **NEED_MORE_DATA**. 전향적 SHADOW로만 검증한다.
- 운영형 S01(UNL)과 현재가 기준 Heat를 함께 쓰면 신규 매수가 사실상 멈춘다. 자동매수 전에 정해야 할 정책 문제다.
- P7 (위험 예산): 판정은 **NOT_RECOMMENDED 그대로**다.
  - 운영형에서 매수가 멈추는 원인은 현재가 기준 Heat 다. 6년간 12~17건이며, Current Heat 의 79~86% 가 평가이익이다.
  - Entry Heat 로 바꾸면 매수는 167~231건으로 회복되지만, 자금 포화와 섹터 집중이 생긴다.
  - 손절가 없는 수동 보유 30% 를 15% 위험으로 계산에 합치면 자동매수가 0건이다. 자동매수 위험을 sleeve 로 분리해야 한다.
  - B01 순위는 같은 후보군 무작위 이하다.
