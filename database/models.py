from sqlalchemy import (create_engine, Column, Integer, String, Float,
                         DateTime, Boolean, Text, Enum, ForeignKey)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATABASE_URL

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Weinstein 스캔 결과 ──────────────────────────────────────────

class ScanResult(Base):
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, index=True)
    scan_time = Column(DateTime, default=datetime.utcnow, index=True)
    market = Column(String(10), index=True)   # KR / US
    ticker = Column(String(20), index=True)
    name = Column(String(100))
    signal_type = Column(String(20))          # BREAKOUT / RE_BREAKOUT / REBOUND
    stage = Column(String(10))
    price = Column(Float)
    ma150 = Column(Float)
    volume = Column(Float)
    volume_avg = Column(Float)
    volume_ratio = Column(Float)
    signal_date = Column(String(10))          # YYYY-MM-DD
    notified = Column(Boolean, default=False)
    # ── 확장 메타데이터 (nullable) ──────────────────────────────
    pivot_price      = Column(Float,       nullable=True)   # 돌파 기준 pivot 가격
    support_level    = Column(Float,       nullable=True)   # MA50 지지선
    market_condition = Column(String(20),  nullable=True)   # BULL/BEAR/CAUTION/NEUTRAL
    signal_quality   = Column(String(10),  nullable=True)   # STRONG/MODERATE/WEAK
    rs_value         = Column(Float,       nullable=True)   # Mansfield RS (v4)
    grade            = Column(String(5),   nullable=True)   # S/A/B 종합 등급
    # ── Scanner decision snapshot (chart overlay) ───────────────
    # 날짜로 저장해야 차트 조회 range가 달라져도 signal-date 당시 판정 구간이
    # 이동하지 않는다. v1 및 기존 행은 tight_*가 NULL이다.
    base_start_date   = Column(String(10),  nullable=True)
    base_end_date     = Column(String(10),  nullable=True)
    tight_start_date  = Column(String(10),  nullable=True)
    base_high         = Column(Float,       nullable=True)
    base_low          = Column(Float,       nullable=True)
    tight_high        = Column(Float,       nullable=True)
    tight_low         = Column(Float,       nullable=True)
    base_width_pct    = Column(Float,       nullable=True)
    tight_width_pct   = Column(Float,       nullable=True)
    contraction_ratio = Column(Float,       nullable=True)
    base_mode         = Column(String(5),   nullable=True)
    # ── Step 4 entry-control observations ───────────────────────
    pivot_ext_pct     = Column(Float,       nullable=True)
    upthrust_failed   = Column(Boolean,     nullable=True)  # None = D+N 미도래
    cur_ext_pct       = Column(Float,       nullable=True)
    cur_stop_pct      = Column(Float,       nullable=True)
    entry_warnings    = Column(Text,        nullable=True)  # JSON 배열
    # ── Step 5 position-sizing snapshot ────────────────────────
    suggested_qty         = Column(Integer,     nullable=True)
    r_per_share           = Column(Float,       nullable=True)
    risk_amount           = Column(Float,       nullable=True)
    position_pct          = Column(Float,       nullable=True)
    sizing_constrained_by = Column(String(20),  nullable=True)
    equity_snapshot       = Column(Float,       nullable=True)
    # ── Strict Weinstein filter (Phase 1 scaffold) ──────────────
    stop_loss            = Column(Float,       nullable=True)              # Gate 8: BUY 시그널 손절가
    sector_name          = Column(String(50),  nullable=True)              # Gate 2: 종목 sector (후속 plan)
    sector_stage         = Column(String(10),  nullable=True)              # Gate 2: sector Stage1-4
    rs_trend             = Column(String(10),  nullable=True)              # Gate 6: RISING/FALLING/FLAT
    rs_zero_crossed      = Column(Boolean,     nullable=True)              # Gate 6: 최근 RS 0선 음→양 전환
    strict_filter_passed = Column(Boolean,     nullable=True, index=True)  # 8 게이트 모두 통과
    filter_reasons       = Column(Text,        nullable=True)              # 거부 사유 JSON 배열


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    market = Column(String(10))
    total_scanned = Column(Integer, default=0)
    signals_found = Column(Integer, default=0)
    status = Column(String(20), default="RUNNING")  # RUNNING / DONE / ERROR
    error_msg = Column(Text, default="")
    triggered_by = Column(String(20), default="manual")


class UpthrustCooldown(Base):
    """확정된 돌파 실패의 쿨다운 만료 시각.

    ScanResult.scan_time 은 같은 신호를 재스캔할 때 갱신되므로 쿨다운 기준으로
    쓰면 만료가 계속 밀린다. 최초 실패 봉 날짜를 별도 보존해 그 문제를 막는다.
    """
    __tablename__ = "upthrust_cooldowns"

    id = Column(Integer, primary_key=True, index=True)
    market = Column(String(10), nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    source_signal_date = Column(String(10), nullable=False)
    failed_date = Column(String(10), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── 계좌 및 거래 관리 ────────────────────────────────────────────

class AccountEquity(Base):
    """시장별 계좌 자산 스냅샷.

    append-only 이력을 유지한다. 현재 값은 market 별 recorded_at 최신 행이며,
    기존 행을 갱신하지 않는다.
    """
    __tablename__ = "account_equity"

    id = Column(Integer, primary_key=True)
    market = Column(String(10), nullable=False, index=True)  # KR / US
    currency = Column(String(3), nullable=False)             # KRW / USD
    total_equity = Column(Float, nullable=False)
    cash_balance = Column(Float, nullable=False)
    note = Column(String(200), nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Account(Base):
    """계좌 (여러 계좌 지원)"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    # KR_STOCK / US_STOCK / KR_PENSION / KR_IRP / KR_ISA / OTHER
    account_type = Column(String(20), default="KR_STOCK", server_default="KR_STOCK")
    currency = Column(String(10), default="KRW")  # KRW / USD (account_type으로 자동 결정)
    broker = Column(String(50), default="")        # 증권사 (키움, 한국투자, ...)
    memo = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    transactions = relationship("Transaction", back_populates="account",
                                order_by="Transaction.trade_date.desc()")
    holdings = relationship("Holding", back_populates="account")

    @property
    def cash_balance(self):
        """입출금 + 매수/매도 후 현금 잔고 계산"""
        bal = 0.0
        for t in self.transactions:
            if t.tx_type == "DEPOSIT":
                bal += t.amount
            elif t.tx_type == "WITHDRAW":
                bal -= t.amount
            elif t.tx_type == "BUY":
                bal -= t.amount + (t.fee or 0)
            elif t.tx_type == "SELL":
                bal += t.amount - (t.fee or 0) - (t.tax or 0)
        return round(bal, 2)


class Transaction(Base):
    """거래 일지 (매수/매도/입금/출금)"""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    tx_type = Column(String(10), nullable=False)  # BUY / SELL / DEPOSIT / WITHDRAW
    trade_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    ticker = Column(String(20), nullable=True)
    name = Column(String(100), nullable=True)
    market = Column(String(10), nullable=True)    # KR / US
    quantity = Column(Float, nullable=True)       # 수량
    price = Column(Float, nullable=True)          # 단가
    amount = Column(Float, nullable=False)        # 총금액 (price * quantity 또는 입출금액)
    fee = Column(Float, default=0)               # 수수료
    tax = Column(Float, default=0)               # 세금 (매도세)
    memo = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="transactions")


class Holding(Base):
    """현재 보유 주식 (평단가 자동 계산)"""
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    market = Column(String(10), nullable=False)   # KR / US
    quantity = Column(Float, default=0)           # 보유 수량
    avg_price = Column(Float, default=0)          # 평단가
    current_price = Column(Float, nullable=True)  # 현재가 (캐시)
    price_updated_at = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=True)
    initial_stop_loss = Column(Float, nullable=True)
    current_stop_loss = Column(Float, nullable=True)
    initial_r = Column(Float, nullable=True)
    sell_status = Column(String(20), default="PENDING", server_default="PENDING")
    sell_severity = Column(String(10), nullable=True)
    sell_reason = Column(Text, nullable=True)
    sell_checked_at = Column(DateTime, nullable=True)
    last_alert_severity = Column(String(10), nullable=True)
    last_alert_reason = Column(String(200), nullable=True)
    last_alert_at = Column(DateTime, nullable=True)
    memo = Column(Text, default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="holdings")

    @property
    def eval_amount(self):
        if self.current_price and self.quantity:
            return round(self.current_price * self.quantity, 2)
        return round(self.avg_price * self.quantity, 2)

    @property
    def profit_loss(self):
        if not self.current_price:
            return 0.0
        return round((self.current_price - self.avg_price) * self.quantity, 2)

    @property
    def profit_loss_pct(self):
        if not self.current_price or not self.avg_price:
            return 0.0
        return round((self.current_price - self.avg_price) / self.avg_price * 100, 2)


# ── Weinstein 감시 목록 (매도 시그널 알림용) ─────────────────────

class WatchList(Base):
    """Weinstein 매도 시그널 감시 종목"""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String(20), unique=True, index=True)
    name = Column(String(100))
    market = Column(String(10))
    buy_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    memo = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()
    db = SessionLocal()
    try:
        if db.query(Account).count() == 0:
            db.add(Account(name="국내 주식 계좌", account_type="KR_STOCK", currency="KRW"))
            db.add(Account(name="해외 주식 계좌", account_type="US_STOCK", currency="USD"))
            db.commit()
    finally:
        db.close()


def _migrate():
    """기존 DB에 새 컬럼이 없으면 추가한다 (SQLite / PostgreSQL)."""
    from sqlalchemy import text as _text

    scan_result_ddls = [
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_start_date VARCHAR(10)",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_end_date VARCHAR(10)",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS tight_start_date VARCHAR(10)",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_high DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_low DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS tight_high DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS tight_low DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_width_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS tight_width_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS contraction_ratio DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS base_mode VARCHAR(5)",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS pivot_ext_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS upthrust_failed BOOLEAN",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS cur_ext_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS cur_stop_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS entry_warnings TEXT",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS suggested_qty INTEGER",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS r_per_share DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS risk_amount DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS position_pct DOUBLE PRECISION",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS sizing_constrained_by VARCHAR(20)",
        "ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS equity_snapshot DOUBLE PRECISION",
    ]

    if engine.dialect.name != "sqlite":
        holding_ddls = [
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS sell_status VARCHAR(20) DEFAULT 'PENDING'",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS sell_severity VARCHAR(10)",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS sell_reason TEXT",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS sell_checked_at TIMESTAMP",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS entry_price DOUBLE PRECISION",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS initial_stop_loss DOUBLE PRECISION",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS current_stop_loss DOUBLE PRECISION",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS initial_r DOUBLE PRECISION",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS last_alert_severity VARCHAR(10)",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS last_alert_reason VARCHAR(200)",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS last_alert_at TIMESTAMP",
        ]
        with engine.begin() as conn:
            for ddl in scan_result_ddls + holding_ddls:
                conn.execute(_text(ddl))
        return

    with engine.connect() as conn:
        def _existing_cols(table):
            return {row[1] for row in conn.execute(_text(f"PRAGMA table_info({table})"))}

        # accounts 테이블
        acct_cols = _existing_cols("accounts")
        for col, ddl in [
            ("account_type", "ALTER TABLE accounts ADD COLUMN account_type VARCHAR(20) DEFAULT 'KR_STOCK'"),
            ("broker",       "ALTER TABLE accounts ADD COLUMN broker VARCHAR(50) DEFAULT ''"),
        ]:
            if col not in acct_cols:
                try:
                    conn.execute(_text(ddl)); conn.commit()
                except Exception:
                    pass

        # scan_results 테이블 — 새 메타데이터 컬럼
        sr_cols = _existing_cols("scan_results")
        for col, ddl in [
            ("pivot_price",          "ALTER TABLE scan_results ADD COLUMN pivot_price REAL"),
            ("support_level",        "ALTER TABLE scan_results ADD COLUMN support_level REAL"),
            ("market_condition",     "ALTER TABLE scan_results ADD COLUMN market_condition VARCHAR(20)"),
            ("signal_quality",       "ALTER TABLE scan_results ADD COLUMN signal_quality VARCHAR(10)"),
            ("rs_value",             "ALTER TABLE scan_results ADD COLUMN rs_value REAL"),
            ("grade",                "ALTER TABLE scan_results ADD COLUMN grade VARCHAR(5)"),
            # Scanner decision snapshot (chart overlay)
            ("base_start_date",      "ALTER TABLE scan_results ADD COLUMN base_start_date VARCHAR(10)"),
            ("base_end_date",        "ALTER TABLE scan_results ADD COLUMN base_end_date VARCHAR(10)"),
            ("tight_start_date",     "ALTER TABLE scan_results ADD COLUMN tight_start_date VARCHAR(10)"),
            ("base_high",            "ALTER TABLE scan_results ADD COLUMN base_high REAL"),
            ("base_low",             "ALTER TABLE scan_results ADD COLUMN base_low REAL"),
            ("tight_high",           "ALTER TABLE scan_results ADD COLUMN tight_high REAL"),
            ("tight_low",            "ALTER TABLE scan_results ADD COLUMN tight_low REAL"),
            ("base_width_pct",       "ALTER TABLE scan_results ADD COLUMN base_width_pct REAL"),
            ("tight_width_pct",      "ALTER TABLE scan_results ADD COLUMN tight_width_pct REAL"),
            ("contraction_ratio",    "ALTER TABLE scan_results ADD COLUMN contraction_ratio REAL"),
            ("base_mode",            "ALTER TABLE scan_results ADD COLUMN base_mode VARCHAR(5)"),
            # Step 4 entry-control observations
            ("pivot_ext_pct",        "ALTER TABLE scan_results ADD COLUMN pivot_ext_pct REAL"),
            ("upthrust_failed",      "ALTER TABLE scan_results ADD COLUMN upthrust_failed BOOLEAN"),
            ("cur_ext_pct",          "ALTER TABLE scan_results ADD COLUMN cur_ext_pct REAL"),
            ("cur_stop_pct",         "ALTER TABLE scan_results ADD COLUMN cur_stop_pct REAL"),
            ("entry_warnings",       "ALTER TABLE scan_results ADD COLUMN entry_warnings TEXT"),
            # Step 5 position-sizing snapshot
            ("suggested_qty",         "ALTER TABLE scan_results ADD COLUMN suggested_qty INTEGER"),
            ("r_per_share",           "ALTER TABLE scan_results ADD COLUMN r_per_share REAL"),
            ("risk_amount",           "ALTER TABLE scan_results ADD COLUMN risk_amount REAL"),
            ("position_pct",          "ALTER TABLE scan_results ADD COLUMN position_pct REAL"),
            ("sizing_constrained_by", "ALTER TABLE scan_results ADD COLUMN sizing_constrained_by VARCHAR(20)"),
            ("equity_snapshot",       "ALTER TABLE scan_results ADD COLUMN equity_snapshot REAL"),
            # Strict Weinstein filter (Phase 1)
            ("stop_loss",            "ALTER TABLE scan_results ADD COLUMN stop_loss REAL"),
            ("sector_name",          "ALTER TABLE scan_results ADD COLUMN sector_name VARCHAR(50)"),
            ("sector_stage",         "ALTER TABLE scan_results ADD COLUMN sector_stage VARCHAR(10)"),
            ("rs_trend",             "ALTER TABLE scan_results ADD COLUMN rs_trend VARCHAR(10)"),
            ("rs_zero_crossed",      "ALTER TABLE scan_results ADD COLUMN rs_zero_crossed BOOLEAN"),
            ("strict_filter_passed", "ALTER TABLE scan_results ADD COLUMN strict_filter_passed BOOLEAN"),
            ("filter_reasons",       "ALTER TABLE scan_results ADD COLUMN filter_reasons TEXT"),
        ]:
            if col not in sr_cols:
                try:
                    conn.execute(_text(ddl)); conn.commit()
                except Exception:
                    pass

        # strict_filter_passed 인덱스 (자주 쓰는 필터)
        try:
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS ix_scan_results_strict_filter_passed "
                "ON scan_results(strict_filter_passed)"
            )); conn.commit()
        except Exception:
            pass

        holding_cols = _existing_cols("holdings")
        for col, ddl in [
            ("sell_status", "ALTER TABLE holdings ADD COLUMN sell_status VARCHAR(20) DEFAULT 'PENDING'"),
            ("sell_severity", "ALTER TABLE holdings ADD COLUMN sell_severity VARCHAR(10)"),
            ("sell_reason", "ALTER TABLE holdings ADD COLUMN sell_reason TEXT"),
            ("sell_checked_at", "ALTER TABLE holdings ADD COLUMN sell_checked_at DATETIME"),
            ("entry_price", "ALTER TABLE holdings ADD COLUMN entry_price REAL"),
            ("initial_stop_loss", "ALTER TABLE holdings ADD COLUMN initial_stop_loss REAL"),
            ("current_stop_loss", "ALTER TABLE holdings ADD COLUMN current_stop_loss REAL"),
            ("initial_r", "ALTER TABLE holdings ADD COLUMN initial_r REAL"),
            ("last_alert_severity", "ALTER TABLE holdings ADD COLUMN last_alert_severity VARCHAR(10)"),
            ("last_alert_reason", "ALTER TABLE holdings ADD COLUMN last_alert_reason VARCHAR(200)"),
            ("last_alert_at", "ALTER TABLE holdings ADD COLUMN last_alert_at DATETIME"),
        ]:
            if col not in holding_cols:
                try:
                    conn.execute(_text(ddl)); conn.commit()
                except Exception:
                    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
