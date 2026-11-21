"""The scan-buy modal must surface live (broker-cash) sizing or why it is hidden."""
import re
from pathlib import Path

HTML = (Path(__file__).parents[1] / "web" / "templates" / "index.html").read_text(encoding="utf-8")


def _scan_buy_quantity_block():
    start = HTML.index('x-model.number="scanBuy.quantity"')
    return HTML[max(0, start - 2500):start]


def test_modal_shows_live_sizing_blocked_reason():
    block = _scan_buy_quantity_block()
    assert "scanBuy.result?.live_sizing_blocked_reason" in block


def test_modal_shows_live_sizing_quantity_and_account():
    block = _scan_buy_quantity_block()
    assert "scanBuy.result?.live_sizing?.qty" in block
    assert "scanBuy.result?.live_sizing?.account_name" in block


def test_scan_time_suggestion_is_labelled_as_scan_time():
    block = _scan_buy_quantity_block()
    assert re.search(r"스캔 시점 제안", block)


def test_blocked_reason_is_rendered_as_text_not_html():
    block = _scan_buy_quantity_block()
    assert not re.search(r'x-html="[^"]*live_sizing', block)
