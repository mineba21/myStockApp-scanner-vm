"""자동매수 SHADOW 는 실제 주문·취소 모듈에 접근하지 않는다 (import 그래프 + 호출 차단)."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("trading.kiwoom_orders", "trading.scanner_buy_orders", "trading.auto_stop.brokers",
             "trading.auto_stop.engine", "trading.auto_stop.runner", "trading.auto_stop.service",
             "trading.auto_stop.store", "trading.kiwoom_allocation_live_broker", "web.scanner_order_api",
             "web.holding_order_api")


def test_shadow_modules_do_not_import_order_modules():
    code = (
        "import sys, json\n"
        "import trading.auto_buy.settings, trading.auto_buy.shadow.runner, trading.auto_buy.shadow.engine\n"
        "import trading.auto_buy.shadow.snapshot, trading.auto_buy.shadow.report, trading.auto_buy.shadow.risk\n"
        "import trading.auto_buy.shadow.market_data, web.auto_buy_api, scripts.auto_buy_shadow_journal_migrate\n"
        "import trading.auto_buy.shadow.inputs, trading.auto_buy.version\n"
        "import web.kiwoom_holdings, web.kiwoom_sizing\n"   # OPS 계좌 스냅샷의 읽기 전용 조회 경로
        "print(json.dumps(sorted(m for m in sys.modules)))\n")
    env = {"PATH": "/usr/bin:/bin", "AUTO_BUY_US_MODE": "OFF", "KIWOOM_TRADING_ENABLED": "false",
           "DATABASE_URL": "sqlite://", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not loaded & set(FORBIDDEN), loaded & set(FORBIDDEN)


def test_account_snapshot_path_does_not_load_order_modules():
    """kiwoom_account_snapshot 실제 호출 경로(web.kiwoom_holdings 조회 함수)를 가짜 클라이언트로 끝까지 실행한다."""
    code = (
        "import sys, json, os\n"
        "os.environ['KIWOOM_WEB_ENABLED']='true'\n"
        "import web.kiwoom_holdings as H\n"
        "H.get_kiwoom_account_summaries = lambda **k: [{'account_profile':'account1','updated_at':'t',"
        "'overseas':{'cash':100.0,'orderable_cash':50.0,'evaluation_amount':0.0}}]\n"
        "H.get_kiwoom_holdings = lambda **k: []\n"
        "from trading.auto_buy.shadow.market_data import kiwoom_account_snapshot\n"
        "r = kiwoom_account_snapshot()\n"
        "print(json.dumps({'status': r['status'], 'mods': sorted(sys.modules)}))\n")
    env = {"PATH": "/usr/bin:/bin", "AUTO_BUY_US_MODE": "OFF", "KIWOOM_TRADING_ENABLED": "false",
           "DATABASE_URL": "sqlite://", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["status"] == "OK"
    assert not set(res["mods"]) & set(FORBIDDEN), set(res["mods"]) & set(FORBIDDEN)


def test_shadow_source_has_no_order_references():
    pat = re.compile(r"kiwoom_orders|scanner_buy_orders|KiwoomOrderClient|auto_stop\.(brokers|engine|runner|service|store)"
                     r"|place_order|cancel_order|ust2000[13]")
    hits = [str(p) for p in (ROOT / "trading" / "auto_buy").rglob("*.py") if pat.search(p.read_text())]
    hits += [str(ROOT / "web" / "auto_buy_api.py")] if pat.search((ROOT / "web" / "auto_buy_api.py").read_text()) else []
    assert hits == []


def test_shadow_job_runs_with_order_client_forbidden(monkeypatch, tmp_path):
    """실계좌 주문 클라이언트가 생성·호출되면 실패하도록 막은 상태에서 SHADOW 전체 흐름을 돌린다."""
    import trading.kiwoom_orders as KO
    from tests.test_auto_buy_shadow import ACCT, Bars, D1, D2, scan, sig, utc
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database.models import Base, ScanLog, ScanResult
    from trading.auto_buy.settings import AutoBuySettings
    from trading.auto_buy.shadow.runner import run_shadow_job
    from trading.auto_buy.shadow.schema import upgrade_journal
    from trading.auto_buy.shadow.vfills import Bar

    def forbidden(*a, **k):
        raise AssertionError("SHADOW 가 실계좌 주문 클라이언트에 접근했습니다")
    monkeypatch.setattr(KO.KiwoomOrderClient, "__init__", forbidden)
    for name in dir(KO.KiwoomOrderClient):
        if not name.startswith("_") and callable(getattr(KO.KiwoomOrderClient, name)):
            monkeypatch.setattr(KO.KiwoomOrderClient, name, forbidden)
    path = str(tmp_path / "s.sqlite3")
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", path)
    upgrade_journal(path, allow_create=True)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[ScanResult.__table__, ScanLog.__table__])
    f = sessionmaker(bind=eng)
    scan(f, utc(D1, 21, 30), [sig("AAA")])
    st = AutoBuySettings.from_env()
    kw = dict(settings=st, account_provider=lambda _l: ACCT, session_factory=f, scan_running=lambda: False)
    run_shadow_job(now=utc(D1, 22, 0), bars_provider=Bars({}), **kw)
    scan(f, utc(D2, 21, 30), [])
    out = run_shadow_job(now=utc(D2, 22, 0), bars_provider=Bars({D2: {"AAA": Bar(100.5, 103, 99, 102)}}), **kw)
    assert out["days"] == [D2] and "error" not in out
