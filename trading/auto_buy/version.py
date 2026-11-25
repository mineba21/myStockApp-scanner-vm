"""판정 로직 버전 (4.2). 전략 설정 해시와 별도로, 매수 판정에 영향을 주는 코드의 정규화 해시를 보존한다.

- 대상: 적격 판정, 위험·사이징, 가상 체결, 엔진, 신호 정규화·최초 통과 판정, 거래일·입력 확정, 시장 데이터 처리,
  그리고 의존하는 운영 ``scanner.sizing.calculate_position`` (호출하는 보조 함수·모듈 상수 포함).
- 주석·docstring·공백 변경은 해시에 영향이 없다 (AST 기준). 화면·조회(report, API, 템플릿)·저널 접근은 대상이 아니다.
- 기록된 해시와 다르면 SHADOW job 은 기존 평가 버전으로 계속 처리하지 않는다 (``STRATEGY_VERSION`` 을 올려 새 계좌로 시작).
  이미 기록된 결과는 삭제하지 않는다. Python 버전이 바뀌어 AST 표현이 달라져도 보수적으로 불일치로 본다.
"""
from __future__ import annotations

import ast
import hashlib
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOGIC_FILES = (
    "trading/auto_buy/settings.py",
    "trading/auto_buy/shadow/eligibility.py",
    "trading/auto_buy/shadow/risk.py",
    "trading/auto_buy/shadow/vfills.py",
    "trading/auto_buy/shadow/engine.py",
    "trading/auto_buy/shadow/inputs.py",
    "trading/auto_buy/shadow/snapshot.py",
    "trading/auto_buy/shadow/calendar.py",
    "trading/auto_buy/shadow/market_data.py",
    "trading/auto_stop/us_calendar.py",
)
SIZING_FILE = "scanner/sizing.py"
SIZING_FUNCS = ("calculate_position", "_cfg_value", "_empty_result")


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _norm(src: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(src)), include_attributes=False)


def _sizing_norm(src: str) -> str:
    tree = _strip_docstrings(ast.parse(src))
    keep = [n for n in tree.body
            if isinstance(n, (ast.Assign, ast.AnnAssign)) or (isinstance(n, ast.FunctionDef) and n.name in SIZING_FUNCS)]
    return ast.dump(ast.Module(body=keep, type_ignores=[]), include_attributes=False)


def logic_components(root: Path = ROOT) -> dict:
    out = {}
    for rel in LOGIC_FILES:
        out[rel] = hashlib.sha256(_norm((root / rel).read_text(encoding="utf-8")).encode()).hexdigest()[:16]
    out[SIZING_FILE + "::" + ",".join(SIZING_FUNCS)] = hashlib.sha256(
        _sizing_norm((root / SIZING_FILE).read_text(encoding="utf-8")).encode()).hexdigest()[:16]
    out["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
    return out


@lru_cache(maxsize=1)
def logic_hash() -> str:
    comps = logic_components()
    return hashlib.sha256("|".join(f"{k}={comps[k]}" for k in sorted(comps)).encode()).hexdigest()[:16]
