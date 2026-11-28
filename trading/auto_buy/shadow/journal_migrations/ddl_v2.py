"""자동매수 SHADOW 저널 스키마 v2 추가 DDL (리비전 ab0002). v1(ab0001)은 공개 브랜치에 올라가 동결됐으므로 바꾸지 않고
열 추가만 한다 (기존 행·트리거·제약 유지, 테이블 재생성 없음).

- ``ab_bars.volume`` / ``volume_status``: 일봉 거래량과 상태. POSITIVE(양수 확인) · ZERO(0 확정) · UNKNOWN(NULL·음수·확인
  불가). v2 이전에 저장된 행은 NULL 이며 거래량 미확인(데이터 불완전)으로 본다.
- ``ab_fill_ledger.bar_volume``: 가상 체결에 쓴 봉의 거래량 (근거 기록).
- ``ab_positions.zero_volume_days`` / ``last_zero_volume_day``: 보유 중 거래량 0 확정일 수와 마지막 날
  (손절 체결을 만들지 않은 날. 거래 재개 후 손절·갭 손실 기록에 쓴다).
"""

DDL_V2 = (
    "ALTER TABLE ab_bars ADD COLUMN volume REAL",
    "ALTER TABLE ab_bars ADD COLUMN volume_status TEXT CHECK(volume_status IN ('POSITIVE','ZERO','UNKNOWN'))",
    "ALTER TABLE ab_fill_ledger ADD COLUMN bar_volume REAL",
    "ALTER TABLE ab_positions ADD COLUMN zero_volume_days INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ab_positions ADD COLUMN last_zero_volume_day TEXT",
)
