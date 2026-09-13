"""The API layer: info reads, signed actions, refusals and the dry-run."""

import asyncio

import pytest

import hyper


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"http {self.status_code}")


class FakeHTTP:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload if payload is not None else {}
        self.status_code = status_code
        self.posts: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return FakeResponse(self.payload, self.status_code)


class FakeExchange:
    """Records every SDK call and replays a canned answer."""

    def __init__(self, result=None):
        self.result = result if result is not None else {"status": "ok", "response": {}}
        self.calls: list[tuple] = []

    def order(self, *args):
        self.calls.append(("order", *args))
        return self.result

    def market_close(self, *args):
        self.calls.append(("market_close", *args))
        return self.result

    def modify_order(self, *args):
        self.calls.append(("modify_order", *args))
        return self.result

    def cancel(self, *args):
        self.calls.append(("cancel", *args))
        return self.result

    def update_leverage(self, *args):
        self.calls.append(("update_leverage", *args))
        return self.result


def test_enabled_needs_the_address(monkeypatch):
    assert hyper.enabled() is False
    monkeypatch.setattr(hyper, "ACCOUNT", "0xabc")
    assert hyper.enabled() is True


def test_armed_needs_key_and_live_mode(monkeypatch):
    monkeypatch.setattr(hyper, "ACCOUNT", "0xabc")
    monkeypatch.setattr(hyper, "SECRET", "0xkey")
    monkeypatch.setattr(hyper, "DRY_RUN", True)
    assert hyper.armed() is False
    monkeypatch.setattr(hyper, "DRY_RUN", False)
    assert hyper.armed() is True


def test_info_posts_the_payload():
    http = FakeHTTP({"ok": 1})

    got = asyncio.run(hyper.info(http, {"type": "meta"}))

    assert got == {"ok": 1}
    assert http.posts == [{"type": "meta"}]


def test_info_raises_on_a_bad_status():
    http = FakeHTTP({}, status_code=500)
    attempt = hyper.info(http, {"type": "meta"})

    with pytest.raises(RuntimeError, match="http 500"):
        asyncio.run(attempt)


def test_ok_passes_a_clean_answer():
    result = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
    assert hyper._ok(result, "x") is result


def test_ok_raises_on_a_refusal():
    with pytest.raises(RuntimeError, match="nope"):
        hyper._ok({"status": "err", "response": "nope"}, "x")


def test_ok_raises_on_an_embedded_error():
    result = {"status": "ok", "response": {"data": {"statuses": [{"error": "px too far"}]}}}
    with pytest.raises(RuntimeError, match="px too far"):
        hyper._ok(result, "x")


def test_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setattr(hyper, "DRY_RUN", True)
    fake = FakeExchange()
    hyper.set_exchange(fake)

    assert asyncio.run(hyper.place_limit("BTC", True, 0.5, "100000")) is None
    assert fake.calls == []


def test_place_limit_signs_and_sends(monkeypatch):
    fake = FakeExchange()
    hyper.set_exchange(fake)

    asyncio.run(hyper.place_limit("BTC", True, 0.5, "100000", reduce_only=True))

    assert fake.calls == [("order", "BTC", True, 0.5, 100000.0, {"limit": {"tif": "Gtc"}}, True)]


def test_market_close_passes_the_size():
    fake = FakeExchange()
    hyper.set_exchange(fake)

    asyncio.run(hyper.market_close("BTC", 0.25))

    assert fake.calls == [("market_close", "BTC", 0.25)]


def test_modify_order_rebuilds_the_order():
    fake = FakeExchange()
    hyper.set_exchange(fake)

    asyncio.run(hyper.modify_order(7, "BTC", True, 0.4, "99000"))

    assert fake.calls == [
        ("modify_order", 7, "BTC", True, 0.4, 99000.0, {"limit": {"tif": "Gtc"}}, False)
    ]


def test_cancel_and_leverage():
    fake = FakeExchange()
    hyper.set_exchange(fake)

    asyncio.run(hyper.cancel_order("BTC", 7))
    asyncio.run(hyper.update_leverage("BTC", 1))

    assert fake.calls == [("cancel", "BTC", 7), ("update_leverage", 1, "BTC", True)]


def test_action_raises_on_a_refused_order():
    fake = FakeExchange({"status": "ok", "response": {"data": {"statuses": [{"error": "no"}]}}})
    hyper.set_exchange(fake)
    attempt = hyper.market_close("BTC")

    with pytest.raises(RuntimeError, match="no"):
        asyncio.run(attempt)


def test_exchange_builds_once_from_the_key(monkeypatch):
    """The lazy SDK import path: a stub SDK proves the wiring without eth keys."""
    import sys
    import types

    built = {}

    class StubExchange:
        def __init__(self, wallet, base_url=None, account_address=None):
            built["wallet"] = wallet
            built["base_url"] = base_url
            built["account"] = account_address

    eth_account = types.ModuleType("eth_account")

    class Account:
        @staticmethod
        def from_key(key):
            return f"wallet:{key}"

    eth_account.Account = Account  # type: ignore[attr-defined]
    hl_pkg = types.ModuleType("hyperliquid")
    hl_exchange = types.ModuleType("hyperliquid.exchange")
    hl_exchange.Exchange = StubExchange  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eth_account", eth_account)
    monkeypatch.setitem(sys.modules, "hyperliquid", hl_pkg)
    monkeypatch.setitem(sys.modules, "hyperliquid.exchange", hl_exchange)
    monkeypatch.setattr(hyper, "ACCOUNT", "0xmain")
    monkeypatch.setattr(hyper, "SECRET", "0xkey")

    first = hyper.exchange()

    assert built == {"wallet": "wallet:0xkey", "base_url": hyper.API_URL, "account": "0xmain"}
    assert hyper.exchange() is first  # cached


def test_secret_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("HL_SECRET", "0xdirect")
    monkeypatch.setenv("HL_SECRET_KEYCHAIN", "hl-sizer")

    assert hyper._secret() == "0xdirect"


def test_secret_empty_without_any_source(monkeypatch):
    monkeypatch.delenv("HL_SECRET", raising=False)
    monkeypatch.delenv("HL_SECRET_KEYCHAIN", raising=False)

    assert hyper._secret() == ""


def test_secret_reads_the_keychain(monkeypatch):
    import subprocess

    monkeypatch.delenv("HL_SECRET", raising=False)
    monkeypatch.setenv("HL_SECRET_KEYCHAIN", "hl-sizer")
    seen = {}

    def fake_run(argv, capture_output, text, check, timeout):
        seen["argv"] = argv

        class Out:
            stdout = "0xfromkeychain\n"

        return Out()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert hyper._secret() == "0xfromkeychain"
    assert seen["argv"] == ["security", "find-generic-password", "-w", "-s", "hl-sizer"]


def test_secret_survives_a_locked_keychain(monkeypatch, caplog):
    import subprocess

    monkeypatch.delenv("HL_SECRET", raising=False)
    monkeypatch.setenv("HL_SECRET_KEYCHAIN", "hl-sizer")

    def refuse(*args, **kwargs):
        raise subprocess.CalledProcessError(44, "security")

    monkeypatch.setattr(subprocess, "run", refuse)

    with caplog.at_level("ERROR", logger="relay.hyper"):
        assert hyper._secret() == ""
    assert "keychain read failed" in caplog.text
