"""Slack Incoming Webhook notification adapter."""
import logging

import requests

from config import SLACK_WEBHOOK_URL


logger = logging.getLogger(__name__)
_MAX_TEXT_LENGTH = 39_000


def send_slack_message(text: str, webhook_url: str = None) -> bool:
    """Post text to the channel associated with a Slack Incoming Webhook."""
    url = (webhook_url or SLACK_WEBHOOK_URL).strip()
    if not url:
        logger.warning("Slack 미설정 (SLACK_WEBHOOK_URL)")
        return False
    if not url.startswith("https://hooks.slack.com/services/"):
        logger.error("Slack Webhook URL 형식이 올바르지 않습니다")
        return False

    ok = True
    for chunk in _split(text, _MAX_TEXT_LENGTH):
        try:
            response = requests.post(url, json={"text": chunk}, timeout=10)
            if not response.ok:
                logger.error("Slack 실패: %s %s", response.status_code, response.text)
                ok = False
        except Exception as exc:
            logger.error("Slack 오류: %s", exc)
            ok = False
    return ok


def test_slack() -> bool:
    return send_slack_message("🤖 Weinstein 스캐너 Slack 연결 테스트 성공!")


def _split(text: str, size: int) -> list:
    if len(text) <= size:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, size)
        if cut == -1:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks
