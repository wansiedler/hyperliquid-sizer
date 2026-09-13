"""The main loop: config, bot delivery, lifecycle, wiring."""

import asyncio
import signal

import pytest

import hyper
import relay
import sheets
import speaker
import tv_alerts
import watch


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeHTTP:
    """Stands in for httpx.AsyncClient: records posts, replays canned answers."""

    def __init__(self, post_response=None, post_error=None):
        self._post_response = post_response or FakeResponse()
        self._post_error = post_error
        self.posted: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, data=None, files=None, timeout=None):
        if self._post_error is not None:
            raise self._post_error
        if json is not None and "text" in json:
            self.posted.append(json["text"])
        elif json is not None:
            # a hyperliquid /info read from watch.equity during --check
            return FakeResponse(payload={"marginSummary": {"accountValue": "1000"}})
        elif data is not None and "media" in data:
            import json as _json

            media = _json.loads(data["media"])
            self.posted.append((media[0].get("caption", ""), len(media), len(files or {})))
        else:
            self.posted.append(data["caption"])
        return self._post_response


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(relay, "BOT_TOKEN", "token")
    monkeypatch.setattr(relay, "TARGET_CHAT_ID", "777")
    monkeypatch.setattr(relay, "NOTIFY_LIFECYCLE", True)
    monkeypatch.setattr(hyper, "ACCOUNT", "0xme")


# --------------------------------------------------------------------------- #
#  require_config
# --------------------------------------------------------------------------- #
def test_require_config_passes_when_complete(config):
    relay.require_config()


def test_require_config_lists_every_missing_name(monkeypatch):
    monkeypatch.setattr(relay, "BOT_TOKEN", None)
    monkeypatch.setattr(relay, "TARGET_CHAT_ID", "")
    monkeypatch.setattr(hyper, "ACCOUNT", "")

    with pytest.raises(SystemExit) as exit_info:
        relay.require_config()

    message = str(exit_info.value)
    assert "BOT_TOKEN" in message
    assert "TARGET_CHAT_ID" in message
    assert "HL_ACCOUNT" in message


# --------------------------------------------------------------------------- #
#  bot delivery
# --------------------------------------------------------------------------- #
def test_send_via_bot_accepted(config):
    http = FakeHTTP()

    assert asyncio.run(relay.send_via_bot(http, "hi")) is True
    assert http.posted == ["hi"]


def test_send_via_bot_html_and_quiet(config, caplog):
    http = FakeHTTP()

    with caplog.at_level("INFO", logger="relay"):
        asyncio.run(relay.send_via_bot(http, "<b>x</b>", True, quiet=True))

    assert "sent:" not in caplog.text


def test_send_via_bot_rejections(config):
    assert (
        asyncio.run(relay.send_via_bot(FakeHTTP(FakeResponse(status_code=400, text="bad")), "x"))
        is False
    )
    refused = FakeHTTP(FakeResponse(payload={"ok": False}))
    assert asyncio.run(relay.send_via_bot(refused, "x")) is False

    class NotJSON(FakeResponse):
        def json(self):
            raise ValueError("no json")

    assert asyncio.run(relay.send_via_bot(FakeHTTP(NotJSON(text="<html>")), "x")) is False


def test_send_via_bot_survives_transport_errors(config):
    import httpx

    http = FakeHTTP(post_error=httpx.ConnectError("no route"))

    assert asyncio.run(relay.send_via_bot(http, "x")) is False


def test_send_photo_via_bot(config):
    http = FakeHTTP()

    assert asyncio.run(relay.send_photo_via_bot(http, "cap", b"png")) is True
    assert http.posted == ["cap"]


def test_send_photo_via_bot_refused_and_broken(config):
    import httpx

    refused = FakeHTTP(FakeResponse(payload={"ok": False}))
    assert asyncio.run(relay.send_photo_via_bot(refused, "cap", b"png")) is False
    broken = FakeHTTP(post_error=httpx.ConnectError("x"))
    assert asyncio.run(relay.send_photo_via_bot(broken, "cap", b"png")) is False


def test_send_album_via_bot(config):
    http = FakeHTTP()

    ok = asyncio.run(relay.send_album_via_bot(http, "cap", [b"a", b"b"]))

    assert ok is True
    assert http.posted == [("cap", 2, 2)]


def test_send_album_via_bot_refused_and_broken(config):
    import httpx

    refused = FakeHTTP(FakeResponse(payload={"ok": False}))
    assert asyncio.run(relay.send_album_via_bot(refused, "c", [b"a"])) is False
    broken = FakeHTTP(post_error=httpx.ConnectError("x"))
    assert asyncio.run(relay.send_album_via_bot(broken, "c", [b"a"])) is False


# --------------------------------------------------------------------------- #
#  lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "5s"), (65, "1m5s"), (3 * 3600 + 60, "3h1m"), (86400 * 2 + 3600, "2d1h")],
)
def test_human_uptime(seconds, expected):
    assert relay.human(seconds) == expected


def test_notify_posts_quietly(config, caplog):
    http = FakeHTTP()

    with caplog.at_level("INFO", logger="relay"):
        asyncio.run(relay.notify(http, "🟢 up"))

    assert http.posted == ["🟢 up"]
    assert "sent:" not in caplog.text


def test_notify_silent_when_disabled(config, monkeypatch):
    monkeypatch.setattr(relay, "NOTIFY_LIFECYCLE", False)
    http = FakeHTTP()

    asyncio.run(relay.notify(http, "🟢 up"))

    assert http.posted == []


def test_notify_swallows_failures(config, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("down")

    monkeypatch.setattr(relay, "send_via_bot", boom)

    asyncio.run(relay.notify(FakeHTTP(), "🟢 up"))  # must not raise


# --------------------------------------------------------------------------- #
#  wiring
# --------------------------------------------------------------------------- #
def test_links_pair_masks_the_secret(monkeypatch):
    monkeypatch.setattr(tv_alerts, "JOURNAL_URL", "https://sheet.example")
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(tv_alerts, "TV_PUBLIC_URL", "https://tv.example")

    links, shown = relay._links_pair()

    assert "https://tv.example/tv/s3cret" in links
    assert "s3cret" not in shown
    assert "📒 https://sheet.example" in shown


def test_links_pair_empty_without_config():
    assert relay._links_pair() == ("", "")


def test_command_handlers_reach_the_watch_side(config, monkeypatch):
    seen = []

    async def positions_report(http, album=None):
        seen.append("positions")
        return "x"

    monkeypatch.setattr(watch, "positions_report", positions_report)
    handlers = relay._command_handlers(FakeHTTP(), "links")

    assert asyncio.run(handlers.positions()) == "x"
    assert handlers.links == "links"
    assert handlers.stop_all is not None
    assert handlers.close_one is not None
    assert handlers.statistics is not None
    assert handlers.market is not None
    assert handlers.lev_one is not None


def test_spawn_watchers_matches_the_config(config, monkeypatch):
    monkeypatch.setattr(hyper, "SECRET", "0xkey")
    monkeypatch.setattr(sheets, "SHEETS_URL", "https://x")
    monkeypatch.setattr(sheets, "SHEETS_SECRET", "s")

    async def spawn():
        tasks = relay._spawn_watchers(FakeHTTP())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    assert asyncio.run(spawn()) == 4  # watch, money, sizer, sheets


def test_spawn_watchers_idle_without_keys(monkeypatch):
    monkeypatch.setattr(hyper, "ACCOUNT", "")

    async def spawn():
        return len(relay._spawn_watchers(FakeHTTP()))

    assert asyncio.run(spawn()) == 0


# --------------------------------------------------------------------------- #
#  run() and check()
# --------------------------------------------------------------------------- #
class InstantShutdown:
    """A _Shutdown that fires the moment the loop waits on it."""

    def __init__(self):
        self.event = asyncio.Event()
        self.reason = "test"

    def install(self):
        self.event.set()


def test_run_starts_and_stops_cleanly(config, monkeypatch):
    monkeypatch.setattr(relay, "_Shutdown", InstantShutdown)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda: FakeHTTP())
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(tv_alerts, "TV_PORT", 0)

    asyncio.run(relay.run())


def test_run_without_tv_or_speaker(config, monkeypatch):
    monkeypatch.setattr(relay, "_Shutdown", InstantShutdown)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda: FakeHTTP())

    asyncio.run(relay.run())


def test_run_with_a_speaker(config, monkeypatch):
    monkeypatch.setattr(relay, "_Shutdown", InstantShutdown)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda: FakeHTTP())

    class FakeAudio:
        def shutdown(self):
            self.stopped = True

    audio = FakeAudio()
    monkeypatch.setattr(speaker, "enabled", lambda: True)
    monkeypatch.setattr(speaker, "serve_forever", lambda: audio)
    monkeypatch.setattr(speaker, "hours_text", lambda: "08:00-23:00")
    lifecycle_calls = []

    async def lifecycle(text):
        lifecycle_calls.append(text)

    monkeypatch.setattr(speaker, "lifecycle", lifecycle)

    asyncio.run(relay.run())

    assert lifecycle_calls == ["Relay up", "Relay down"]
    assert audio.stopped is True


def test_check_reports_delivery_and_equity(config, monkeypatch, capsys):
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda: FakeHTTP())

    asyncio.run(relay.check())

    out = capsys.readouterr().out
    assert "telegram: ok" in out
    assert "hyperliquid equity: 1000.0" in out


def test_shutdown_turns_signals_into_the_event():
    async def scenario():
        shutdown = relay._Shutdown()
        shutdown.install()
        shutdown._fire("SIGTERM")
        assert shutdown.event.is_set()
        assert shutdown.reason == "SIGTERM"
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)

    asyncio.run(scenario())


def test_main_routes_the_check_flag(config, monkeypatch):
    ran = []

    async def fake_check():
        ran.append("check")

    async def fake_run():
        ran.append("run")

    monkeypatch.setattr(relay, "check", fake_check)
    monkeypatch.setattr(relay, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["relay.py", "--check"])
    relay.main()
    monkeypatch.setattr("sys.argv", ["relay.py"])
    relay.main()

    assert ran == ["check", "run"]
