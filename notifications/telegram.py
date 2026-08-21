import requests
import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def send_telegram_message(text: str, chat_id: str = None, parse_mode: str = "Markdown") -> bool:
    token = TELEGRAM_BOT_TOKEN
    cid   = chat_id or TELEGRAM_CHAT_ID
    if not token or not cid:
        logger.warning("텔레그램 미설정 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
        return False

    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split(text, 4096)
    ok     = True
    for chunk in chunks:
        try:
            r = requests.post(url, json={
                "chat_id": cid, "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            if not r.ok:
                logger.error(f"텔레그램 실패: {r.status_code} {r.text}")
                ok = False
        except Exception as e:
            logger.error(f"텔레그램 오류: {e}")
            ok = False
    return ok


def test_telegram() -> bool:
    return send_telegram_message("🤖 Weinstein 스캐너 연결 테스트 성공!")


def _split(text: str, n: int) -> list:
    if len(text) <= n:
        return [text]
    chunks, buf = [], text
    while buf:
        if len(buf) <= n:
            chunks.append(buf); break
        cut = buf.rfind("\n", 0, n)
        if cut == -1: cut = n
        chunks.append(buf[:cut])
        buf = buf[cut:].lstrip("\n")
    return chunks
