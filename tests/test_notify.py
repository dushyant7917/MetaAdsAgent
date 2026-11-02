import httpx

from metaads import notify
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


def test_a_photo_goes_out_as_a_photo_and_an_oversized_one_as_a_document(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: sent.append((url, kw)) or type("R", (), {"raise_for_status": lambda s: None})())
    small = tmp_path / "a.png"
    small.write_bytes(b"x" * 10)
    big = tmp_path / "b.png"
    big.write_bytes(b"x" * 10)
    n = notify.TelegramNotifier("tok", "42")
    n.send_photo(None, small, "caption")
    monkeypatch.setattr(notify, "MAX_PHOTO_BYTES", 5)
    n.send_photo(None, big, "caption")
    assert sent[0][0].endswith("/sendPhoto") and "photo" in sent[0][1]["files"] and sent[0][1]["data"]["chat_id"] == "42"
    assert sent[1][0].endswith("/sendDocument") and "document" in sent[1][1]["files"]
    notify.NullNotifier().send_photo("1", small, "c")                      # does nothing, does not fail
