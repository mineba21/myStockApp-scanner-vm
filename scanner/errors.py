"""스캐너 도메인 예외 정의.

`DataFetchError` 는 외부 OHLCV 어댑터(`fetch_ohlcv`) 에서 *명시적* 으로
실패를 알릴 때만 사용한다. "정상적으로 데이터가 없는" 경우(빈 결과,
짧은 시계열, 미상장 티커 등) 는 기존 graceful-None 정책을 그대로
유지한다 — 호출자가 외부 장애와 정상 빈 결과를 구분할 수 있게 한다.
"""


class DataFetchError(Exception):
    """외부 OHLCV 어댑터의 명시적 fetch 실패."""
