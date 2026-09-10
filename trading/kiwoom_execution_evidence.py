"""Strict, read-only account2 execution evidence. Never submits orders.

Official contracts: docs/kiwoom_execution_evidence.md.
Cash reservation semantics remain unverified; this module cannot enable trading.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import time

from trading.kiwoom_readonly import KiwoomError
from trading.allocation_rebalance import UnavailableBroker, RebalanceBlocked

KST = ZoneInfo('Asia/Seoul')


def amount(value):
    if isinstance(value, bool) or value is None:
        raise KiwoomError('금액/수량 필드 누락 또는 잘못된 형식')
    try:
        result = Decimal(str(value).strip().replace(',', ''))
    except InvalidOperation as exc:
        raise KiwoomError('금액/수량 형식 오류') from exc
    if not result.is_finite() or result < 0:
        raise KiwoomError('금액/수량은 유한한 0 이상의 값이어야 합니다.')
    return result


def quantity(value):
    result = amount(value)
    if result != result.to_integral_value():
        raise KiwoomError('정수 수량이 필요합니다.')
    return int(result)


def order_number(value):
    value = str(value or '').strip()
    if not value.isascii() or not value.isdigit() or len(value) > 9 or int(value) == 0:
        raise KiwoomError('유효한 주문번호가 없습니다.')
    return value.zfill(9)


def order_date(value):
    try:
        parsed = datetime.strptime(value, '%Y%m%d')
        if parsed.strftime('%Y%m%d') != value:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise KiwoomError('주문일자는 KST YYYYMMDD 형식이어야 합니다.') from exc
    return value


class ExecutionEvidence:
    def __init__(self, client, token, *, account='account2', max_pages=100):
        if account != 'account2' or not 1 <= max_pages <= 100:
            raise KiwoomError('account2 및 유효한 페이지 제한이 필요합니다.')
        self.client, self.token, self.max_pages = client, token, max_pages

    def rows(self, api_id, payload):
        if api_id not in {'ust21150', 'ust21050', 'ust21110', 'ust31490'}:
            raise KiwoomError('허용되지 않은 조회 API')
        rows, seen = [], set()
        headers = {'Content-Type': 'application/json;charset=UTF-8',
                   'authorization': 'Bearer ' + self.token, 'api-id': api_id}
        for page in range(self.max_pages):
            response = self.client.session.post(
                self.client.config.base_url + ('/api/us/ordr' if api_id == 'ust31490' else '/api/us/acnt'), headers=dict(headers),
                json=payload, timeout=self.client.config.timeout_seconds)
            data = self.client._parse_response(response, '주문 검증 조회')
            code = data.get('return_code')
            if type(code) not in (int, str) or code not in (0, '0'):
                raise KiwoomError('명시적인 성공 코드가 없습니다.')
            if api_id == 'ust31490':
                if response.headers.get('cont-yn') not in (None, '', 'N'):
                    raise KiwoomError('주문가능수량 단일 응답이 완전하지 않습니다.')
                return data
            items = data.get('result_list')
            if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
                raise KiwoomError('전체 결과 목록을 확인할 수 없습니다.')
            rows.extend(items)
            continuation = response.headers.get('cont-yn', '')
            if continuation in ('', 'N', None):
                return rows
            key = response.headers.get('next-key')
            if continuation != 'Y' or not key or key in seen:
                raise KiwoomError('연속조회 응답이 불완전하거나 반복됩니다.')
            seen.add(key)
            headers.update({'cont-yn': 'Y', 'next-key': key})
            if page + 1 < self.max_pages:
                time.sleep(.2)
        raise KiwoomError('전체 페이지 조회가 끝나지 않았습니다.')

    def buy_capacity(self, ticker, exchange, price, *, reserve_bps=100):
        if (not isinstance(ticker, str) or not ticker.strip() or len(ticker) > 12 or
                exchange not in ('ND', 'NY', 'NA')):
            raise KiwoomError('종목과 거래소를 확인하세요.')
        price = amount(price)
        reserve = amount(reserve_bps)
        if price <= 0 or price != price.quantize(Decimal('.01')) or reserve >= 10000:
            raise KiwoomError('센트 단위 양수 가격과 유효한 여유금이 필요합니다.')
        started = time.time()
        data = self.rows('ust31490', {'stex_tp': exchange, 'stk_cd': ticker.strip().upper(), 'uv': str(price)})
        if data.get('crnc_code') != 'USD':
            raise KiwoomError('USD 주문가능수량 응답이 아닙니다.')
        cash = amount(data.get('ord_alowa'))
        no_margin_cash = amount(data.get('min_ord_alowa'))
        no_margin_qty = quantity(data.get('min_ord_alowq'))
        # Never use the 50% margin or KRW order capacity fields.
        budget = min(cash, no_margin_cash) * (1 - reserve / 10000)
        max_qty = min(no_margin_qty, int(budget / price))
        if time.time() - started > 60:
            raise KiwoomError('주문가능수량 조회가 만료되었습니다.')
        return {'ticker': ticker.strip().upper(), 'exchange': exchange, 'currency': 'USD',
                'limit_price': str(price), 'broker_orderable_cash': str(cash),
                'no_margin_cash': str(no_margin_cash), 'broker_max_quantity': no_margin_qty,
                'budget_with_reserve': str(budget), 'max_quantity_with_reserve': max_qty,
                'reserve_bps': str(reserve), 'source': 'ust31490', 'observed_at': started,
                'valid_until': started + 60, 'execution_enabled': False,
                'note': '개별 주문 조회 한도입니다. 종목별 한도를 합산하지 마세요. 전체 주문 예산 검증은 별도입니다.'}

    def history(self, day):
        day = order_date(day)
        rows = self.rows('ust21150', {'ord_dt': day, 'query_tp': '1', 'slby_tp': '0',
                                    'stex_tp': '', 'stk_cd': '', 'oppo_trde_tp': '%', 'fr_ord_no': ''})
        normalized = [self.normalize(row, day) for row in rows]
        ids = [row['order_number'] for row in normalized]
        if len(ids) != len(set(ids)):
            raise KiwoomError('주문번호가 중복되어 고유 대조할 수 없습니다.')
        return normalized

    @staticmethod
    def normalize(row, day):
        if row.get('crnc_code') != 'USD' or row.get('slby_tp_nm') not in ('매수', '매도'):
            raise KiwoomError('주문 통화/방향을 확인할 수 없습니다.')
        ticker = row.get('stk_cd')
        if not isinstance(ticker, str) or not ticker.strip():
            raise KiwoomError('주문 종목이 없습니다.')
        qty, filled, remaining, cancelled, modified = [quantity(row.get(k)) for k in
            ('ord_qty', 'cntr_qty', 'ord_remnq', 'cncl_qty', 'mdfy_qty')]
        if qty <= 0 or filled > qty or remaining > qty or cancelled > qty or modified > qty:
            raise KiwoomError('주문 수량이 일관되지 않습니다.')
        try:
            at = datetime.strptime(day + ' ' + row['ord_time'], '%Y%m%d %H:%M:%S').replace(tzinfo=KST).timestamp()
        except (ValueError, KeyError, TypeError) as exc:
            raise KiwoomError('주문 시각을 확인할 수 없습니다.') from exc
        raw_state = row.get('ord_stat_nm')
        if modified or raw_state == '정정완료':
            state = 'MODIFIED'
        elif raw_state == '무효주문':
            state = 'REJECTED'
        elif cancelled or raw_state == '취소완료':
            state = 'CANCELLED'
        elif raw_state == '체결완료' and filled == qty and remaining == 0:
            state = 'FILLED'
        elif raw_state == '접수' and filled + remaining == qty and remaining > 0:
            state = 'PARTIAL' if filled else 'OPEN'
        else:
            state = 'UNKNOWN'
        return {'order_number': order_number(row.get('ord_no')), 'ticker': ticker.strip().upper(),
                'side': 'BUY' if row['slby_tp_nm'] == '매수' else 'SELL', 'quantity': qty,
                'filled_quantity': filled, 'remaining_quantity': remaining,
                'price': str(amount(row.get('ord_uv'))), 'state': state, 'ordered_at': at,
                'order_date': day, 'account': 'account2'}

    def lookup(self, intent):
        # Without a returned order number, an identical manual order cannot be
        # distinguished from this request. Never auto-adopt a ticker/time match.
        if not intent.get('order_number'):
            return None
        day = datetime.fromtimestamp(intent['created_at'], KST).strftime('%Y%m%d')
        number = order_number(intent['order_number'])
        candidates = [r for r in self.history(day) if r['order_number'] == number]
        if len(candidates) != 1:
            return None
        row = candidates[0]
        if (row['ticker'] != intent['ticker'] or row['side'] != intent['side'] or
            row['quantity'] != intent['quantity'] or amount(row['price']) != amount(intent['price']) or
            not intent['created_at'] - 1 <= row['ordered_at'] <= intent['created_at'] + 120):
            return None
        return {**row, 'matched_intent_id': intent['id']}

    def cash_check(self, day):
        started = time.time()
        day = order_date(day)
        pending = self.rows('ust21050', {'ord_dt': day, 'slby_tp': '0', 'stex_tp': '', 'stk_cd': ''})
        usd = [r for r in self.rows('ust21110', {}) if r.get('crnc_code') == 'USD']
        if len(usd) != 1:
            raise KiwoomError('USD 주문가능 금액이 없거나 중복됩니다.')
        cash = amount(usd[0].get('fc_ord_alowa'))
        blockers = ['주문가능 금액의 예약금·수수료 반영 의미 미검증',
                    '미체결 조회는 지정한 KST 주문일자 범위이며 전체 유효 주문 범위 미검증']
        if pending:
            blockers.append('조회일자에 미체결 주문이 남아 있습니다.')
        if time.time() - started > 60:
            blockers.append('조회가 60초를 초과했습니다. 재조회가 필요합니다.')
        return {'account': 'account2', 'currency': 'USD', 'order_date': day,
                'broker_orderable_cash': str(cash), 'cash_source': 'ust21110.fc_ord_alowa',
                'open_order_count': len(pending), 'cash_includes_reservations': None,
                'validated_for_execution': False, 'blockers': blockers, 'observed_at': started}


class EvidenceBroker(UnavailableBroker):
    """Live history lookup; other operations remain fail-closed pending contracts."""
    def __init__(self, evidence_factory):
        self.evidence_factory = evidence_factory

    def lookup(self, intent):
        try:
            return self.evidence_factory().lookup(intent)
        except Exception as exc:
            raise RebalanceBlocked('증권사 주문 대조 실패: 주문을 재전송하지 않습니다.') from exc
