import httpx

from metaads.notify import TelegramNotifier


def test_ads_go_to_the_requesting_chat_but_reports_go_to_the_channel(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, data=None, **kw: sent.append(data["chat_id"]) or
                        type("R", (), {"raise_for_status": lambda self: None})())
    n = TelegramNotifier("t", default_chat_id="dm", report_chat_id="channel")
    n.send_text("asker", "x"); n.send_text(None, "y"); n.send_report("asker", "summary")
    assert sent == ["asker", "dm", "channel"]


def test_without_a_channel_reports_stay_with_the_ads(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, data=None, **kw: sent.append(data["chat_id"]) or
                        type("R", (), {"raise_for_status": lambda self: None})())
    TelegramNotifier("t", default_chat_id="dm").send_report("asker", "summary")
    assert sent == ["asker"]


def test_from_env_splits_dm_and_channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "11,22")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1009")
    n = TelegramNotifier.from_env()
    assert (n.default_chat_id, n.report_chat_id) == ("11", "-1009")
