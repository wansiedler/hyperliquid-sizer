"""The sizer: stop discovery, fee-inclusive quantities, the resize pass."""

import asyncio

import pytest

import hyper
import sizer
from test_watch import Actions, FakeHTTP, Recorder, entry_order, trigger


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(hyper, "ACCOUNT", "0xme")
    monkeypatch.setattr(hyper, "SECRET", "0xkey")


@pytest.fixture
def acting(keyed, monkeypatch):
    actions = Actions()
    monkeypatch.setattr(hyper, "modify_order", actions._make("modify_order"))
    return actions


def test_enabled_needs_address_and_key(monkeypatch):
    assert sizer.enabled() is False
    monkeypatch.setattr(hyper, "ACCOUNT", "0xme")
    assert sizer.enabled() is False
    monkeypatch.setattr(hyper, "SECRET", "0xkey")
    assert sizer.enabled() is True


# ------------------------------------------------------------------ stops
def test_stop_from_the_order_children():
    order = entry_order(children=[{"orderType": "Stop Market", "triggerPx": "98000"}])

    assert sizer._stop_of(order, []) == 98000.0


def test_stop_from_the_account_trigger():
    assert sizer._stop_of(entry_order(), [trigger(px="97000")]) == 97000.0


def test_stop_ignores_takes_unpriced_and_other_coins():
    orders = [
        trigger(kind="Take Profit Market", px="103000"),
        trigger(coin="ETH", px="1"),
        trigger(px="0"),
        {
            "coin": "BTC",
            "isTrigger": True,
            "reduceOnly": False,
            "orderType": "Stop Market",
            "triggerPx": "5",
        },
    ]

    assert sizer._stop_of(entry_order(), orders) is None


def test_stop_child_without_a_price_falls_through():
    order = entry_order(children=[{"orderType": "Stop Market", "triggerPx": "0"}])

    assert sizer._stop_of(order, [trigger(px="97000")]) == 97000.0


def test_stop_fallback_percent(monkeypatch):
    monkeypatch.setattr(sizer, "FALLBACK_SL_PCT", 0.01)

    assert sizer._stop_of(entry_order(), []) == pytest.approx(99000.0)
    assert sizer._stop_of(entry_order(side="A"), []) == pytest.approx(101000.0)


# ------------------------------------------------------------------ sizing
def test_target_qty_budgets_the_fees():
    # 0.5% of 10000 = 50 over (1000 + 15 maker + 44.55 taker) = 0.047...
    order = entry_order(px="100000")

    qty = sizer.target_qty(order, 99000.0, 10_000.0, 0.001)

    assert qty == pytest.approx(0.047)


def test_target_qty_respects_the_leverage_ceiling():
    order = entry_order(px="100000")

    # A 10-point stop asks for a huge size; 5x on 10k allows 0.5 at 100k.
    assert sizer.target_qty(order, 99990.0, 10_000.0, 0.001) == pytest.approx(0.5)


def test_target_qty_refuses_nonsense():
    assert sizer.target_qty(entry_order(px="0"), 99.0, 1000.0, 0.001) is None
    assert sizer.target_qty(entry_order(), 100000.0, 1000.0, 0.001) is None  # glued stop
    assert sizer.target_qty(entry_order(), 99.0, 1000.0, 0.0) is None
    assert sizer.target_qty(entry_order(px="100000"), 99999.99, 0.001, 0.001) is None  # dust


# ------------------------------------------------------------------ settling
def test_has_settled_waits_for_still_orders(monkeypatch):
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 2)
    order = entry_order()

    assert sizer.has_settled(order) is False
    assert sizer.has_settled(order) is True
    assert sizer.has_settled(dict(order, limitPx="101000")) is False  # moved: reset


# ------------------------------------------------------------------ the pass
def sizing_http(depo="10000"):
    http = FakeHTTP()
    http.state["marginSummary"] = {"accountValue": depo}
    return http


def test_tick_resizes_a_settled_order(acting):
    http = sizing_http()
    http.orders = [entry_order(sz="0.001"), trigger(px="99000")]
    out = Recorder()

    asyncio.run(sizer.tick(http, out.send))

    assert acting.calls == [("modify_order", 5, "BTC", True, 0.047, "100000")]
    assert out.sent == [
        "⚖️ BTC Buy limit @ 100000\n"
        "stop 99000 (1.00%) → qty 0.001 (100$, 1.0% депо) → 0.047 (4,700$, 47.0% депо)"
    ]


def test_tick_leaves_a_fitting_order_alone(acting):
    http = sizing_http()
    http.orders = [entry_order(sz="0.047"), trigger(px="99000")]

    asyncio.run(sizer.tick(http, Recorder().send))

    assert acting.calls == []


def test_tick_skips_stopless_triggers_and_reduce_only(acting):
    http = sizing_http()
    http.orders = [
        entry_order(oid=1),  # no stop anywhere
        trigger(oid=2),  # a trigger, not an entry — also BTC's stop
        dict(entry_order(oid=3, coin="ETH"), reduceOnly=True),
    ]

    asyncio.run(sizer.tick(http, Recorder().send))

    # oid=1 IS covered by the account stop and resizes; make it stopless.
    http2 = sizing_http()
    http2.orders = [entry_order(coin="SOL", oid=9)]
    asyncio.run(sizer.tick(http2, Recorder().send))

    assert all(call[1] != 9 for call in acting.calls)


def test_tick_waits_for_settling_and_forgets_stale(acting, monkeypatch):
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 2)
    http = sizing_http()
    http.orders = [entry_order(sz="0.001"), trigger(px="99000")]

    asyncio.run(sizer.tick(http, Recorder().send))
    assert acting.calls == []  # first sight: still settling
    assert 5 in sizer._settling

    http.orders = []
    asyncio.run(sizer.tick(http, Recorder().send))
    assert 5 not in sizer._settling  # gone: forgotten


def test_tick_gives_up_without_equity(acting):
    http = sizing_http()
    http.orders = [entry_order(sz="0.001"), trigger(px="99000")]
    http.fail_types = {"clearinghouseState"}

    asyncio.run(sizer.tick(http, Recorder().send))

    assert acting.calls == []


def test_tick_skips_an_unknown_lot(acting):
    http = sizing_http()
    http.orders = [entry_order(coin="GHOST", sz="1"), trigger(coin="GHOST", px="99000")]

    asyncio.run(sizer.tick(http, Recorder().send))

    assert acting.calls == []


def test_tick_reports_a_refused_modify(acting):
    http = sizing_http()
    http.orders = [entry_order(sz="0.001"), trigger(px="99000")]
    acting.refuse = True
    out = Recorder()

    asyncio.run(sizer.tick(http, out.send))

    assert out.sent == ["❌ BTC: не смог пересайзить лимитку (refused)"]


def test_poll_announces_reports_new_errors_once(keyed, monkeypatch, caplog):
    http = sizing_http()
    http.fail_types = {"frontendOpenOrders"}
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sizer.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(hyper, "DRY_RUN", True)
    out = Recorder()
    attempt = sizer.poll(http, out.send)

    with caplog.at_level("ERROR", logger="relay.sizer"), pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)

    assert out.sent[0].startswith("⚖️ sizer up — dry-run, risk 0.5%")
    assert out.sent[1].startswith("⚖️❌ sizer:")
    assert len(out.sent) == 2  # the same error is not repeated


def test_poll_lets_cancellation_through(keyed):
    class Cancelling(FakeHTTP):
        async def post(self, url, json=None, timeout=None, data=None, files=None):
            raise asyncio.CancelledError

    attempt = sizer.poll(Cancelling(), Recorder().send)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)


def test_stop_skips_non_stop_children():
    order = entry_order(children=[{"orderType": "Take Profit Market", "triggerPx": "103000"}])

    assert sizer._stop_of(order, [trigger(px="97000")]) == 97000.0


def test_poll_recovers_and_resets_the_error(keyed, monkeypatch):
    calls = {"n": 0}

    class Blinking(FakeHTTP):
        async def post(self, url, json=None, timeout=None, data=None, files=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("blink")
            return await super().post(url, json=json, timeout=timeout)

    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sizer.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(hyper, "DRY_RUN", True)
    out = Recorder()
    attempt = sizer.poll(Blinking(), out.send)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)

    assert out.sent[1] == "⚖️❌ sizer: blink"
    assert len(out.sent) == 2
