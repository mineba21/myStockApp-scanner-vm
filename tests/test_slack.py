"""Slack Incoming Webhook adapter tests."""
from notifications import slack


class _Response:
    def __init__(self, ok=True, status_code=200, text="ok"):
        self.ok = ok
        self.status_code = status_code
        self.text = text


def test_slack_is_disabled_without_webhook(monkeypatch):
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "")
    assert slack.send_slack_message("hello") is False


def test_slack_posts_json_text(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return _Response()

    monkeypatch.setattr(slack.requests, "post", fake_post)

    result = slack.send_slack_message(
        "buy signal",
        webhook_url="https://hooks.slack.com/services/T/B/secret",
    )

    assert result is True
    assert captured == {
        "url": "https://hooks.slack.com/services/T/B/secret",
        "json": {"text": "buy signal"},
        "timeout": 10,
    }


def test_slack_rejects_non_slack_url(monkeypatch):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return _Response()

    monkeypatch.setattr(slack.requests, "post", fake_post)

    assert slack.send_slack_message("hello", "https://example.com/hook") is False
    assert called is False


def test_slack_http_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        slack.requests,
        "post",
        lambda *args, **kwargs: _Response(False, 403, "invalid_token"),
    )

    assert slack.send_slack_message(
        "hello",
        "https://hooks.slack.com/services/T/B/secret",
    ) is False
