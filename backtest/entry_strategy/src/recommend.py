"""권장 자동매수 조건 JSON. 백테스트 결과만으로 자동매수를 활성화하지 않는다."""
from __future__ import annotations

import math

import pandas as pd

POOL_TEXT = {
    "F0": "Strict 통과 전체",
    "F1": "유형별 적격 조건 (BREAKOUT/RE_BREAKOUT: 피벗 대비 0~5%, 손절폭 ≤12%, 돌파 실패 확정 아님 · "
          "REBOUND: 손절폭 ≤12%, MA150 대비 ≤15%)",
}
MARKET_TEXT = {"M0": "추가 시장 필터 없음 (Strict 의 BEAR·CAUTION 돌파 차단만)",
               "M1": "운영 시장 상태 BULL 일 때만", "M2": "SPY 종가 > SPY 50일선 일 때만",
               "M3": "유니버스 150일선 위 종목 비율 > 50% 일 때만"}
RANK_TEXT = {"GRADE": "기존 Grade → Signal Quality 우선순위", "SC1": "Auto Buy Score SC1 (Train 선택 요소, 동일 가중)",
             "SC2": "Auto Buy Score SC2 (이론 방향 고정: RS+, 거래량+, 확장−, 손절폭−)",
             "SC3": "Auto Buy Score SC3 (Train 최강 단일 요소)"}


def avoid_conditions(fs: pd.DataFrame) -> list:
    """전체 기간(H60)과 Train 절단 결과에서 모두 평균 R<0 이고 PF<1, 표본 ≥30 인 구간."""
    dims = ("type×stop_pct", "type×ext_pct", "type×grade", "type×quality", "market_condition", "grade", "quality",
            "strict_pass_is_first_detection", "stop_pct", "ext_pct")
    full = fs[(fs["horizon"] == "H60") & (fs["period"] == "full")]
    trn = fs[(fs["horizon"] == "H60") & (fs["period"] == "train_truncated")]
    unl = fs[(fs["horizon"] == "UNL") & (fs["period"] == "full")]
    out = []
    for a in list(dims) + sorted(x for x in full["analysis"].unique() if x.startswith("warn:")):
        f = full[full["analysis"] == a]
        for r in f.itertuples():
            if r.n < 30 or r.avg_R is None or not (r.avg_R < 0) or not (r.profit_factor is not None and r.profit_factor < 1):
                continue
            if a.startswith("warn:") and r.bucket != "True":
                continue
            t = trn[(trn["analysis"] == a) & (trn["bucket"] == r.bucket)]
            u = unl[(unl["analysis"] == a) & (unl["bucket"] == r.bucket)]
            tr_ok = len(t) and t.iloc[0]["n"] >= 15 and t.iloc[0]["avg_R"] < 0
            out.append({"dimension": a, "bucket": r.bucket, "n_full": int(r.n), "avg_R_full_H60": r.avg_R,
                        "pf_full_H60": r.profit_factor,
                        "avg_R_train": float(t.iloc[0]["avg_R"]) if len(t) else None,
                        "n_train": int(t.iloc[0]["n"]) if len(t) else 0,
                        "avg_R_UNL": float(u.iloc[0]["avg_R"]) if len(u) else None,
                        "evidence": "CONSISTENT" if tr_ok else "FULL_PERIOD_ONLY"})
    return out


def _posthoc_candidate(ph):
    if ph is None or ph.empty:
        return None
    q = ph[(ph["pool"] == "HARD_EXCL") & (ph["market"] == "M1") & (ph["topn"] == 2) & (ph["horizon"] == "H60")]
    seg = {r["segment"]: {k: r[k] for k in ("total_return", "cagr", "mdd", "profit_factor", "trades", "avg_R",
                                              "random_percentile", "random_median") if k in r and r[k] == r[k]}
           for _, r in q.iterrows()}
    return {
        "status": "NEED_MORE_DATA",
        "label": "사후 탐색 후보 (결과 확인 후 정의 — 이번 백테스트의 채택 근거 아님, 전향적 SHADOW 로만 검증)",
        "rule": "Strict 통과 + 운영 시장 상태 BULL + 제외(최초 탐지 후 늦게 Strict 통과 · 피벗 아래 · 손절폭 >15%) "
                "+ 기존 Grade 순위 + 하루 Top-2, T1_100 지정가, P_C 위험 설정",
        "segments_H60_P_C": seg,
        "why": "신호 단위 통제 분석에서 시장 NEUTRAL·CAUTION 의 R 이 유의하게 낮았고(90% CI 0 미포함), "
               "제외 구간은 전체·Train 모두 평균 R<0. 같은 후보군 무작위 대비 백분위는 중앙값 수준 → 효과는 순위가 아니라 필터.",
    }


def build(verdict: dict, fs: pd.DataFrame, summary: pd.DataFrame, rank_tab: pd.DataFrame, et: pd.DataFrame,
          posthoc: pd.DataFrame = None) -> dict:
    sel = verdict["selected"]
    return {
        "posthoc_candidate": _posthoc_candidate(posthoc),
        "generated_note": "백테스트 결과만으로 자동매수를 활성화하지 않는다. 구현·SHADOW·LIVE 는 각각 별도 승인. "
                          "매도는 운영 S01 (고정 Scanner 손절가) 그대로이며 시간 청산을 추가하지 않는다.",
        "data_quality": "RECONSTRUCTED (현재 스캐너 로직 replay). VERIFIED_HISTORICAL 0건. 현재 유니버스 생존 편향. "
                        "Test 구간은 기존 전체 기간 결과 확인 후 설계라 완전한 미관측 데이터가 아님.",
        "status": verdict["status"],
        "selected_rule": {
            "pool": sel["pool"], "pool_text": POOL_TEXT.get(sel["pool"], sel["pool"]),
            "market": sel["market"], "market_text": MARKET_TEXT[sel["market"]],
            "rank": sel["rank"], "rank_text": RANK_TEXT[sel["rank"]],
            "daily_top_n": sel["topn"], "timing": sel["timing"],
            "order": "탐지일 장 마감 후 결정 → 다음 정규장 시가 확인(시가 ≤ 손절가 또는 R<3% 이면 전송 안 함) → "
                     "탐지일 종가 +1% 지정가 1회, 미체결 시 취소·추격 없음",
            "sizing": "운영 scanner.sizing.calculate_position (지정가 상한 기준): 위험 0.5%, 종목 10%, Heat 2%, R 3~15%",
            "score_components": verdict["score_fit_train"].get(sel["rank"]) if sel["rank"].startswith("SC") else None,
        },
        "checks": verdict["checks"],
        "test": verdict["test"], "operational_unl_full": verdict["operational_unl_full"],
        "do_not_buy": {
            "hard_rules": [
                "Strict 미통과(거부·미평가) 신호",
                "매수 시점 시가가 고정 손절가 이하이거나 (시가-손절)/시가 < 3%",
                "R(지정가 상한 기준) 3~15% 밖, 계좌 Heat·종목 비중·현금 한도로 0주",
                "이미 보유(수동 포함) 또는 주문 대기 중인 종목 — 자동 추가매수 금지",
                "같은 신호(signal_key) 재주문, 지정가 미체결 후 추격 재주문",
            ],
            "evidence_based": avoid_conditions(fs),
        },
    }
