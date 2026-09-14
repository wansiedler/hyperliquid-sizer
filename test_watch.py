"""The Hyperliquid watcher: parsing, policing, notices, journal, commands."""

import asyncio
import time
from typing import Any

import pytest

import hyper
import sheets
import watch
from watch import Position


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def candle_rows(n=40, base=100_000.0):
    rows = []
    t0 = 1_700_000_000_000
    for i in range(n):
        px = base * (1 + 0.001 * ((i % 7) - 3))
        rows.append(
            {
                "t": t0 + i * 900_000,
                "o": str(px),
                "h": str(px * 1.001),
                "l": str(px * 0.999),
                "c": str(px * 1.0005),
            }
        )
    return rows


class FakeHTTP:
    """Routes /info payloads by type, the way the exchange answers."""

    def __init__(self):
        self.state: dict[str, Any] = {
            "assetPositions": [],
            "marginSummary": {"accountValue": "1000"},
        }
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.funding: list[dict] = []
        self.ledger: list[dict] = []
        self.candles = candle_rows()
        self.requests: list[dict] = []
        self.fail_types: set[str] = set()

    async def post(self, url, json=None, timeout=None, data=None, files=None):
        self.requests.append(json)
        kind = json["type"]
        if kind in self.fail_types:
            raise OSError(f"{kind} down")
        answers = {
            "clearinghouseState": self.state,
            "frontendOpenOrders": self.orders,
            "userFills": self.fills,
            "userFunding": self.funding,
            "userNonFundingLedgerUpdates": self.ledger,
            "candleSnapshot": self.candles,
            "meta": {
                "universe": [
                    {"name": "BTC", "szDecimals": 3},
                    {"name": "ETH", "szDecimals": 2},
                ]
            },
            "allMids": {"BTC": "100500"},
        }
        return FakeResponse(answers[kind])


class Recorder:
    def __init__(self, photo_ok=True):
        self.sent: list[str] = []
        self.spoken: list[str] = []
        self.photos: list[str] = []
        self.photo_ok = photo_ok

    async def send(self, text, html=False):
        self.sent.append(text)
        return True

    async def speak(self, text):
        self.spoken.append(text)
        return True

    async def send_photo(self, caption, png):
        assert png.startswith(b"\x89PNG")
        self.photos.append(caption)
        return self.photo_ok

    async def send_album(self, caption, pngs):
        assert all(p.startswith(b"\x89PNG") for p in pngs)
        self.photos.append(caption)
        return self.photo_ok


class Actions:
    """Stands in for hyper's signed actions, recording each."""

    def __init__(self, refuse=False):
        self.calls: list[tuple] = []
        self.refuse = refuse

    def _make(self, name):
        async def act(*args):
            if self.refuse:
                raise OSError("refused")
            self.calls.append((name, *args))
            return {}

        return act


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(hyper, "ACCOUNT", "0xme")


@pytest.fixture
def acting(keyed, monkeypatch):
    actions = Actions()
    for name in ("market_close", "cancel_order", "update_leverage", "modify_order"):
        monkeypatch.setattr(hyper, name, actions._make(name))
    return actions


def pos_row(coin="BTC", szi="0.5", entry="100000", value="50000", upnl="0", lev=1):
    return {
        "position": {
            "coin": coin,
            "szi": szi,
            "entryPx": entry,
            "positionValue": value,
            "unrealizedPnl": upnl,
            "leverage": {"type": "cross", "value": lev},
        }
    }


def trigger(coin="BTC", kind="Stop Market", px="99000", oid=11, reduce_only=True, sz="0.5"):
    return {
        "coin": coin,
        "oid": oid,
        "isTrigger": True,
        "reduceOnly": reduce_only,
        "orderType": kind,
        "triggerPx": px,
        "sz": sz,
        "side": "A",
    }


def entry_order(coin="BTC", oid=5, px="100000", sz="0.5", side="B", children=None):
    return {
        "coin": coin,
        "oid": oid,
        "isTrigger": False,
        "reduceOnly": False,
        "orderType": "Limit",
        "limitPx": px,
        "sz": sz,
        "side": side,
        "children": children or [],
    }


def fill(
    coin="BTC",
    direction="Open Long",
    sz="0.5",
    px="100000",
    t=1_700_000_100_000,
    fee="7.5",
    closed="0",
):
    return {
        "coin": coin,
        "dir": direction,
        "sz": sz,
        "px": px,
        "time": t,
        "fee": fee,
        "closedPnl": closed,
    }


# --------------------------------------------------------------------------- #
#  positions
# --------------------------------------------------------------------------- #
def test_positions_fold_triggers_and_births(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(), pos_row(coin="ETH", szi="-2", entry="3000")]
    http.orders = [
        trigger(),  # BTC stop
        trigger(coin="BTC", kind="Take Profit Market", px="103000", oid=12),
        trigger(coin="ETH", kind="Take Profit Limit", px="2800", oid=13),
        trigger(coin="BTC", kind="Stop Market", px="0", oid=14),  # unpriced: ignored
        {"coin": "BTC", "isTrigger": False, "reduceOnly": False, "oid": 15},  # entry
        trigger(coin="GHOST", oid=16),  # no such position
    ]
    http.fills = [fill(), fill(coin="ETH", direction="Open Short", sz="2", t=1_700_000_200_000)]

    open_now = asyncio.run(watch.positions(http))

    btc, eth = open_now["BTC"], open_now["ETH"]
    assert (btc.side, btc.size, btc.stop_loss, btc.take_profit) == ("long", 0.5, 99000.0, 103000.0)
    assert (eth.side, eth.size, eth.take_profit) == ("short", 2.0, 2800.0)
    assert btc.created_ms == 1_700_000_100_000
    assert eth.created_ms == 1_700_000_200_000


def test_positions_skip_zero_sizes_and_cache_births(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(szi="0"), pos_row(coin="ETH", szi="1")]
    http.fills = [fill(coin="ETH", direction="Open Long", sz="1")]

    first = asyncio.run(watch.positions(http))
    fills_asks = sum(1 for r in http.requests if r["type"] == "userFills")
    second = asyncio.run(watch.positions(http))

    assert "BTC" not in first
    assert second["ETH"].created_ms == 1_700_000_100_000
    assert sum(1 for r in http.requests if r["type"] == "userFills") == fills_asks  # cached


def test_positions_empty_account_asks_nothing_more(keyed):
    http = FakeHTTP()

    assert asyncio.run(watch.positions(http)) == {}
    assert [r["type"] for r in http.requests] == ["clearinghouseState"]


def test_position_birth_stops_at_the_window_edge():
    fills = [fill(direction="Open Long", sz="0.2")]

    assert watch.position_birth(fills, "BTC", 0.5) == 0  # window too short


def test_position_birth_ignores_closes_and_other_coins():
    fills = [
        fill(direction="Close Long", sz="9", t=1_700_000_300_000),
        fill(coin="ETH", direction="Open Long", sz="9", t=1_700_000_250_000),
        fill(direction="Open Long", sz="0.3", t=1_700_000_200_000),
        fill(direction="Open Long", sz="0.2", t=1_700_000_100_000),
    ]

    assert watch.position_birth(fills, "BTC", 0.5) == 1_700_000_100_000


# --------------------------------------------------------------------------- #
#  balances, funding, fees
# --------------------------------------------------------------------------- #
def test_equity_reads_the_account_value(keyed):
    assert asyncio.run(watch.equity(FakeHTTP())) == 1000.0


def test_equity_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}

    assert asyncio.run(watch.equity(http)) is None


def test_equity_without_a_figure_is_none(keyed):
    http = FakeHTTP()
    http.state["marginSummary"] = {}

    assert asyncio.run(watch.equity(http)) is None


def test_wallet_balance_excludes_unrealised(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(upnl="200"), pos_row(coin="ETH", szi="1", upnl="-50")]

    assert asyncio.run(watch.wallet_balance(http)) == 850.0


def test_wallet_balance_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}

    assert asyncio.run(watch.wallet_balance(http)) is None


def test_funding_flips_the_sign_to_cost(keyed):
    http = FakeHTTP()
    http.funding = [
        {"delta": {"coin": "BTC", "usdc": "-0.4"}},  # paid 0.4
        {"delta": {"coin": "BTC", "usdc": "0.1"}},  # received 0.1
        {"delta": {"coin": "ETH", "usdc": "-9"}},  # other coin
    ]

    assert asyncio.run(watch.accrued_funding(http, "BTC", 1)) == pytest.approx(0.3)


def test_funding_is_zero_for_a_fresh_position(keyed):
    assert asyncio.run(watch.accrued_funding(FakeHTTP(), "BTC", 0)) == 0.0


def test_funding_survives_a_refusal(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"userFunding"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        assert asyncio.run(watch.accrued_funding(http, "BTC", 1)) == 0.0
    assert "no funding history" in caplog.text


def test_entry_fee_per_unit_reads_opening_fills(keyed):
    http = FakeHTTP()
    http.fills = [
        fill(fee="0.3"),
        fill(fee="0.1", t=1_700_000_200_000),
        fill(direction="Close Long", fee="9", t=1_700_000_300_000),  # not an entry
        fill(fee="9", t=1),  # before birth
    ]
    pos = Position("long", 10.0, 100.0, 1000.0, created_ms=1_700_000_050_000)

    assert asyncio.run(watch.entry_fee_per_unit(http, "BTC", pos)) == pytest.approx(0.04)


def test_entry_fee_per_unit_unknown_without_history(keyed):
    pos = Position("long", 10.0, 100.0, 1000.0, created_ms=1_700_000_050_000)

    assert asyncio.run(watch.entry_fee_per_unit(FakeHTTP(), "BTC", pos)) is None


def test_entry_fee_per_unit_unknown_without_a_birth(keyed):
    assert (
        asyncio.run(watch.entry_fee_per_unit(FakeHTTP(), "BTC", Position("long", 1, 1, 1))) is None
    )


def test_entry_fee_per_unit_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"userFills"}
    pos = Position("long", 10.0, 100.0, 1000.0, created_ms=1)

    assert asyncio.run(watch.entry_fee_per_unit(http, "BTC", pos)) is None


@pytest.mark.parametrize(
    ("side", "expected"),
    [
        ("long", (100_000 + 45) / (1 - 0.00045)),
        ("short", (100_000 - 45) / (1 + 0.00045)),
    ],
)
def test_breakeven_assumes_taker_both_ways(side, expected):
    assert watch.breakeven_price(side, 100_000.0) == pytest.approx(expected)


def test_breakeven_uses_the_real_entry_fee_and_funding():
    be = watch.breakeven_price("long", 100_000.0, 10.0, entry_fee_per_unit=15.0)
    assert be == pytest.approx((100_000 + 15 + 10) / (1 - 0.00045))
    be = watch.breakeven_price("short", 100_000.0, 10.0, entry_fee_per_unit=15.0)
    assert be == pytest.approx((100_000 - 15 - 10) / (1 + 0.00045))


def test_lot_reads_the_meta_once(keyed):
    http = FakeHTTP()

    assert asyncio.run(watch._lot(http, "BTC")) == pytest.approx(0.001)
    assert asyncio.run(watch._lot(http, "ETH")) == pytest.approx(0.01)
    assert asyncio.run(watch._lot(http, "GHOST")) == 0.0
    assert sum(1 for r in http.requests if r["type"] == "meta") == 1


def test_fmt_size_snaps_to_the_grid():
    assert watch._fmt_size(0.30000000000000004, 0.001) == 0.3
    assert watch._fmt_size(1.23456, 0.0) == 1.23456


# --------------------------------------------------------------------------- #
#  the guard
# --------------------------------------------------------------------------- #
NAKED_POS = Position("long", 0.5, 100_000.0, 50_000.0)
COVERED = Position("long", 0.5, 100_000.0, 50_000.0, stop_loss=99_000.0)
LEVERED = Position("short", 1.0, 3_000.0, 3_000.0, stop_loss=3_100.0, leverage=3.0)


@pytest.fixture
def guarding(acting, monkeypatch):
    monkeypatch.setattr(watch, "RISK_GUARD", True)
    monkeypatch.setattr(watch, "GUARD_GRACE", 0.0)
    return acting


def test_guard_stays_dormant_when_disabled(acting):
    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], Recorder().send, Recorder().speak))

    assert acting.calls == []


def test_guard_closes_a_stopless_position_instantly(guarding):
    out = Recorder()

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], out.send, out.speak))

    assert guarding.calls == [("market_close", "BTC")]
    assert out.sent == ["🛑 BTC закрыт маркетом риск-менеджером: нет стопа"]


def test_guard_resets_leverage_after_closing(guarding):
    out = Recorder()

    asyncio.run(watch.guard(FakeHTTP(), {"ETH": LEVERED}, [], out.send, out.speak))

    assert guarding.calls == [("market_close", "ETH"), ("update_leverage", "ETH", 1)]
    assert out.sent == [
        "🛑 ETH закрыт маркетом риск-менеджером: плечо 3x > 1x · плечо сброшено на 1x"
    ]


def test_guard_survives_a_failed_leverage_reset(guarding, monkeypatch, caplog):
    out = Recorder()

    async def refuse(*args):
        raise OSError("margin mode")

    monkeypatch.setattr(hyper, "update_leverage", refuse)

    with caplog.at_level("ERROR", logger="relay.watch"):
        asyncio.run(watch.guard(FakeHTTP(), {"ETH": LEVERED}, [], out.send, out.speak))

    assert out.sent == ["🛑 ETH закрыт маркетом риск-менеджером: плечо 3x > 1x"]
    assert "leverage reset failed" in caplog.text


def test_guard_reports_a_refused_close(guarding, monkeypatch):
    out = Recorder()

    async def refuse(*args):
        raise OSError("min qty")

    monkeypatch.setattr(hyper, "market_close", refuse)

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], out.send, out.speak))

    assert out.sent == ["❌ BTC: риск-менеджер не смог закрыть (min qty)"]


def test_guard_leaves_a_covered_position_alone(guarding):
    asyncio.run(watch.guard(FakeHTTP(), {"BTC": COVERED}, [], Recorder().send, Recorder().speak))

    assert guarding.calls == []


def test_guard_warns_first_with_grace(guarding, monkeypatch):
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    out = Recorder()

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], out.send, out.speak))

    assert guarding.calls == []
    assert out.sent == ["🛑 BTC: нет стопа — закрою маркетом через 45с"]
    assert out.spoken == ["Bitcoin risk breach"]


def test_guard_acts_once_the_grace_runs_out(guarding, monkeypatch):
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    out = Recorder()
    watch._guard_seen["BTC"] = time.monotonic() - 46

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], out.send, out.speak))

    assert guarding.calls == [("market_close", "BTC")]


def test_guard_holds_inside_the_window(guarding, monkeypatch):
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    watch._guard_seen["BTC"] = time.monotonic() - 1

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": NAKED_POS}, [], Recorder().send, Recorder().speak))

    assert guarding.calls == []


def test_guard_forgets_healed_breaches(guarding):
    watch._guard_seen["BTC"] = 1.0
    watch._guard_seen["order:9"] = 1.0

    asyncio.run(watch.guard(FakeHTTP(), {"BTC": COVERED}, [], Recorder().send, Recorder().speak))

    assert watch._guard_seen == {}


def test_guard_cancels_a_naked_entry_order(guarding):
    out = Recorder()

    asyncio.run(watch.guard(FakeHTTP(), {}, [entry_order()], out.send, out.speak))

    assert guarding.calls == [("cancel_order", "BTC", 5)]
    assert out.sent == ["🛑 BTC: лимитка без стопа отменена риск-менеджером"]


def test_guard_warns_about_a_naked_order_with_grace(guarding, monkeypatch):
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    out = Recorder()

    asyncio.run(watch.guard(FakeHTTP(), {}, [entry_order()], out.send, out.speak))

    assert guarding.calls == []
    assert out.sent == ["🛑 BTC: лимитка без стопа — отменю через 45с"]


def test_guard_reports_a_refused_cancel(guarding, monkeypatch):
    out = Recorder()

    async def refuse(*args):
        raise OSError("gone")

    monkeypatch.setattr(hyper, "cancel_order", refuse)

    asyncio.run(watch.guard(FakeHTTP(), {}, [entry_order()], out.send, out.speak))

    assert out.sent == ["❌ BTC: не смог отменить лимитку (gone)"]


def test_guard_spares_protected_entries(guarding):
    orders = [
        entry_order(),  # covered by the account stop below
        trigger(),
        entry_order(coin="ETH", oid=6, children=[{"orderType": "Stop Market"}]),
        entry_order(coin="SOL", oid=7),  # covered by the position's stop
        trigger(coin="SOL", oid=8, kind="Take Profit Market"),  # not a stop
    ]
    open_now = {"SOL": Position("long", 1.0, 200.0, 200.0, stop_loss=195.0)}

    asyncio.run(watch.guard(FakeHTTP(), open_now, orders, Recorder().send, Recorder().speak))

    assert [c for c in guarding.calls if c[0] == "cancel_order"] == []


# --------------------------------------------------------------------------- #
#  the trimmer
# --------------------------------------------------------------------------- #
@pytest.fixture
def trimming(acting, monkeypatch):
    monkeypatch.setattr(watch, "RISK_TRIM", True)
    return acting


def trim_http(depo="1000"):
    http = FakeHTTP()
    http.state["marginSummary"] = {"accountValue": depo}
    return http


# Entry 100, stop 99: 1$ of price risk per unit, 1.08955$ with both taker
# fees (199 × 0.00045). Ten units risk 10.90$ against the 5$ target; the
# budget also pays the entry fee on all ten and the cut's own close.
OVERSIZED = Position("long", 10.0, 100.0, 1000.0, stop_loss=99.0)


def test_trim_stays_dormant_when_disabled(acting):
    asyncio.run(watch.trim(FakeHTTP(), {"ETH": OVERSIZED}, Recorder().send, Recorder().speak))

    assert acting.calls == []


def test_trim_cuts_back_to_the_target_risk(trimming):
    http, out = trim_http(), Recorder()

    asyncio.run(watch.trim(http, {"ETH": OVERSIZED}, out.send, out.speak))

    # budget = 5 - 2*10*0.045 = 4.1; per kept unit 1 + 0.04455 - 0.045 = 0.99955
    # want = floor(4.1/0.99955/0.01)*0.01 = 4.1 -> cut 5.9
    assert trimming.calls == [("market_close", "ETH", 5.9)]
    assert out.sent == ["✂️ ETH: риск 1.09% депо при цели 0.50% — режу 10→4.1"]
    assert out.spoken == ["Ethereum trimmed"]


def test_trim_leaves_a_fitting_position_alone(trimming):
    fits = Position("long", 4.1, 100.0, 410.0, stop_loss=99.0)

    asyncio.run(watch.trim(trim_http(), {"ETH": fits}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_trim_skips_stopless_zero_and_glued(trimming):
    naked = Position("long", 1.0, 100.0, 100.0)
    glued = Position("long", 1.0, 100.0, 100.0, stop_loss=100.0)

    asyncio.run(
        watch.trim(trim_http(), {"A": naked, "B": glued}, Recorder().send, Recorder().speak)
    )

    assert trimming.calls == []


def test_trim_respects_the_cooldown(trimming):
    watch._trim_cooldown["ETH"] = time.monotonic()

    asyncio.run(watch.trim(trim_http(), {"ETH": OVERSIZED}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_trim_gives_up_without_equity(trimming, monkeypatch):
    http = trim_http()
    http.fail_types = {"clearinghouseState"}

    asyncio.run(watch.trim(http, {"ETH": OVERSIZED}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_trim_gives_up_on_an_unknown_lot(trimming):
    asyncio.run(watch.trim(trim_http(), {"GHOST": OVERSIZED}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_trim_cannot_cut_below_one_lot_step(trimming):
    hair = Position("long", 4.11, 100.0, 411.0, stop_loss=99.0)

    asyncio.run(watch.trim(trim_http(), {"ETH": hair}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_trim_reports_a_refused_cut(trimming, monkeypatch):
    out = Recorder()

    async def refuse(*args):
        raise OSError("busy")

    monkeypatch.setattr(hyper, "market_close", refuse)

    asyncio.run(watch.trim(trim_http(), {"ETH": OVERSIZED}, out.send, out.speak))

    assert out.sent == ["❌ ETH: не смог подрезать (busy)"]


def test_trim_fetches_equity_once(trimming):
    http = trim_http()
    fine = Position("long", 1.0, 100.0, 100.0, stop_loss=99.0)

    asyncio.run(watch.trim(http, {"A": fine, "B": fine}, Recorder().send, Recorder().speak))

    assert sum(1 for r in http.requests if r["type"] == "clearinghouseState") == 1


# --------------------------------------------------------------------------- #
#  changes and notices
# --------------------------------------------------------------------------- #
LONG = Position("long", 0.5, 100_000.0, 50_000.0)
BIGGER = Position("long", 1.0, 100_000.0, 100_000.0)
SHORT = Position("short", 0.5, 100_000.0, 50_000.0)


def test_diff_spots_every_kind_of_change():
    changes = watch.diff(
        {"BTC": LONG, "ETH": LONG, "SOL": LONG, "DOGE": LONG},
        {"BTC": LONG, "ETH": BIGGER, "SOL": SHORT, "OP": LONG},
    )

    kinds = {(kind, coin) for kind, coin, _, _ in changes}
    assert kinds == {("changed", "ETH"), ("flipped", "SOL"), ("opened", "OP"), ("closed", "DOGE")}


@pytest.mark.parametrize(
    ("kind", "was", "now", "line", "spoken"),
    [
        ("opened", None, LONG, "💰📈BTC 50,000$@100000", "Bitcoin long opened"),
        ("flipped", LONG, SHORT, "💰BTC → short 50,000@100000", "Bitcoin flipped to short"),
        ("changed", LONG, BIGGER, "💰📈BTC 50,000→100,000", "Bitcoin long increased"),
        ("changed", BIGGER, LONG, "💰📈BTC 100,000→50,000", "Bitcoin long reduced"),
        ("closed", SHORT, None, "💸📉BTC", "Bitcoin short closed"),
    ],
)
def test_describe_every_kind(kind, was, now, line, spoken):
    assert watch.describe(kind, "BTC", was, now) == (line, spoken)


def test_trade_warnings_flag_a_thin_rr():
    thin = Position("long", 1.0, 100.0, 100.0, take_profit=101.0, stop_loss=99.0)
    fine = Position("long", 1.0, 100.0, 100.0, take_profit=110.0, stop_loss=99.0)
    naked = Position("long", 1.0, 100.0, 100.0)

    assert watch.trade_warnings(thin) == ["⚠️ RR 1.00 < 2"]
    assert watch.trade_warnings(fine) == []
    assert watch.trade_warnings(naked) == []


def test_trade_warnings_off_without_min_rr(monkeypatch):
    monkeypatch.setattr(watch, "MIN_RR", 0.0)
    thin = Position("long", 1.0, 100.0, 100.0, take_profit=101.0, stop_loss=99.0)

    assert watch.trade_warnings(thin) == []


def test_tick_primes_silently(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    out = Recorder()

    snapshot = asyncio.run(watch.tick(http, None, out.send, out.speak, out.send_photo))

    assert "BTC" in snapshot
    assert out.sent == []


def test_tick_announces_an_entry_with_chart(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    http.orders = [
        trigger(),
        trigger(kind="Take Profit Market", px="103000", oid=12),
    ]
    http.fills = [fill(t=int(time.time() * 1000), fee="6.75")]
    out = Recorder()

    asyncio.run(watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.photos, "the entry must go out as a chart caption"
    caption = out.photos[0]
    assert caption.startswith("💰📈BTC 50,000$@100000 | RR3.00")
    assert "sl99000:" in caption
    assert "tp103000:" in caption
    assert "комса6.75" in caption
    assert out.spoken == ["Bitcoin long opened"]


def test_tick_falls_back_to_text_when_the_photo_fails(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    out = Recorder(photo_ok=False)

    asyncio.run(watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert len(out.sent) == 1
    assert out.sent[0].startswith("💰📈BTC")


def test_tick_survives_a_dead_order_listing(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"frontendOpenOrders"}
    http.state["assetPositions"] = [pos_row()]

    with caplog.at_level("ERROR", logger="relay.watch"):
        snapshot = asyncio.run(
            watch.tick(http, None, Recorder().send, Recorder().speak, Recorder().send_photo)
        )

    assert "BTC" in snapshot


def test_entry_notice_warns_below_min_rr(keyed):
    http = FakeHTTP()
    thin = Position("long", 0.5, 100_000.0, 50_000.0, take_profit=100_500.0, stop_loss=99_000.0)

    line, png = asyncio.run(watch._entry_notice(http, "BTC", thin, "💰"))

    assert "⚠️ RR 0.50 &lt; 2" in line


def test_entry_fee_recent_survives_a_refusal(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"userFills"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        assert asyncio.run(watch._entry_fee_recent(http, "BTC")) == 0.0


# --------------------------------------------------------------------------- #
#  closes: result, kind, journal
# --------------------------------------------------------------------------- #
def test_closing_result_nets_the_fees():
    fills = [
        fill(direction="Close Long", sz="0.3", px="103000", t=2_000, fee="10", closed="900"),
        fill(direction="Close Long", sz="0.2", px="103100", t=3_000, fee="7", closed="620"),
        fill(direction="Open Long", t=1_000, fee="9"),  # the entry, not the close
        fill(coin="ETH", direction="Close Long", t=2_500, closed="99"),  # other coin
        fill(direction="Close Long", t=500, closed="99"),  # before this position
    ]

    pnl, fees, exit_px, closed_ms = watch.closing_result(fills, "BTC", 1_000)

    assert pnl == pytest.approx(1503.0)  # 1520 gross - 17 fees
    assert fees == 17.0
    assert exit_px == 103100.0
    assert closed_ms == 3_000


def test_close_kind_judges_by_the_exit_level():
    was = Position("long", 1.0, 100.0, 100.0, take_profit=103.0, stop_loss=99.0)

    assert watch.close_kind(was, 99.01, "") == "стоп"
    assert watch.close_kind(was, 103.1, "") == "тейк"
    assert watch.close_kind(was, 101.0, "") == "руками"
    assert watch.close_kind(was, 99.0, "нет стопа") == "риск-гард"
    assert watch.close_kind(Position("long", 1, 100.0, 100.0), 100.0, "") == "руками"


def test_journal_row_fills_the_sheet_columns():
    was = Position(
        "long",
        0.5,
        100_000.0,
        50_000.0,
        take_profit=103_000.0,
        stop_loss=99_000.0,
        created_ms=1_700_000_000_000,
    )

    row = watch.journal_row("BTC", was, 1503.0, 17.0, 103_100.0, depo=10_000.0, kind="тейк")

    assert row[1] == "BTC"
    assert row[2] == "Лонг"
    assert row[3] == "win"
    assert row[4] == "1к3.0"
    assert row[5] == 3.01  # R multiple: 1503 / 500
    assert row[8] == 100_000.0
    assert row[9] == 103_100.0
    assert row[10] == 1503.0
    assert row[11] == 15.03
    assert row[12] == "17.00"
    assert row[15] == "тейк"


def test_journal_row_without_extras_falls_back():
    was = Position("short", 1.0, 100.0, 100.0)

    row = watch.journal_row("BTC", was, -5.0, 0.0, 0.0, forced="нет стопа")

    assert row[0] == ""
    assert row[2] == "Шорт"
    assert row[3] == "stop"
    assert row[4] == "принудительно остановлено: нет стопа"
    assert row[5] == -5.0  # net USDT, no risk to divide by
    assert row[9] == ""
    assert row[11] == ""
    assert row[12] == ""
    assert row[13] == ""


def test_tick_announces_a_close_with_journal(keyed, monkeypatch):
    http = FakeHTTP()
    was = Position(
        "long",
        0.5,
        100_000.0,
        50_000.0,
        take_profit=103_000.0,
        stop_loss=99_000.0,
        created_ms=1_700_000_000_000,
    )
    http.fills = [
        fill(
            direction="Close Long",
            sz="0.5",
            px="103000",
            t=1_700_010_000_000,
            fee="23",
            closed="1500",
        ),
    ]
    http.funding = [{"delta": {"coin": "BTC", "usdc": "-2"}}]
    logged = []

    async def log_close(http_, entry):
        logged.append(entry)
        return True

    monkeypatch.setattr(sheets, "log_close", log_close)
    out = Recorder()

    asyncio.run(watch.tick(http, {"BTC": was}, out.send, out.speak, out.send_photo))

    assert out.photos, "the close must go out as a chart"
    caption = out.photos[0]
    # 1500 - 23 fees - 2 funding = 1475
    assert caption.startswith("💸<b>+1,475.00</b>=<b>1,000.00$</b>(+147.50%)·📈BTC·тейк")
    assert "(комса 23.00, фанд 2.00)" in caption
    assert logged
    assert logged[0]["row"][15] == "тейк"
    assert "png" in logged[0]
    assert logged[0]["name"] == "BTC-1700010000000"
    assert out.spoken == ["Bitcoin long closed, profit 1475"]


def test_tick_close_without_fills_keeps_the_plain_line(keyed):
    http = FakeHTTP()
    out = Recorder()

    asyncio.run(watch.tick(http, {"BTC": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸📈BTC"]


def test_close_notice_survives_a_fills_refusal(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"userFills"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        line, spoken, png = asyncio.run(watch._close_notice(http, "BTC", LONG, "x"))

    assert line is None
    assert png is None


def test_close_notice_without_a_birth_looks_a_day_back(keyed, monkeypatch):
    http = FakeHTTP()
    recent = int(time.time() * 1000) - 1000
    http.fills = [
        fill(direction="Close Long", sz="0.5", px="100100", t=recent, fee="1", closed="50")
    ]

    async def log_close(http_, entry):
        return True

    monkeypatch.setattr(sheets, "log_close", log_close)

    line, spoken, png = asyncio.run(watch._close_notice(http, "BTC", LONG, "x"))

    assert line is not None
    assert line.startswith("💸<b>+49.00</b>")


def test_positions_do_not_cache_births_past_a_fills_refusal(keyed, caplog):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    http.fail_types = {"userFills"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        first = asyncio.run(watch.positions(http))
    http.fail_types = set()
    http.fills = [fill()]
    second = asyncio.run(watch.positions(http))

    assert first["BTC"].created_ms == 0
    assert second["BTC"].created_ms == 1_700_000_100_000


# --------------------------------------------------------------------------- #
#  /positions and the other commands
# --------------------------------------------------------------------------- #
def test_positions_report_needs_the_address():
    assert asyncio.run(watch.positions_report(FakeHTTP())) == watch._NO_KEYS


def test_positions_report_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}

    assert asyncio.run(watch.positions_report(http)) == watch._NO_ANSWER


def test_positions_report_with_nothing_open(keyed):
    assert asyncio.run(watch.positions_report(FakeHTTP())) == "Открытых позиций нет"


def test_positions_report_builds_blocks_and_album(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [
        dict(pos_row(upnl="120")),
        pos_row(coin="ETH", szi="-2", entry="3000", value="6000", upnl="-15"),
    ]
    http.orders = [
        trigger(),
        trigger(kind="Take Profit Market", px="103000", oid=12),
    ]
    http.fills = [fill(fee="7.5"), fill(coin="ETH", direction="Open Short", sz="2", fee="1.8")]
    http.funding = [{"delta": {"coin": "BTC", "usdc": "-0.4"}}]
    out = Recorder()

    report = asyncio.run(watch.positions_report(http, out.send_album))

    assert report == ""  # the album carried it
    caption = out.photos[0]
    assert "📈BTC 50,000$@100000 | RR3.00" in caption
    assert "sl99000:" in caption
    assert "tp103000:" in caption
    assert "−фанд0.40" in caption
    assert "📉ETH 6,000$@3000" in caption
    assert "ΣPnL" in caption


def test_positions_report_text_without_album(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]

    report = asyncio.run(watch.positions_report(http))

    assert report.startswith("📈BTC 50,000$@100000")
    assert "PnL+0.00−комса" in report


def test_positions_report_shows_received_funding(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(szi="-0.5")]
    http.fills = [fill(direction="Open Short")]
    http.funding = [{"delta": {"coin": "BTC", "usdc": "0.4"}}]

    report = asyncio.run(watch.positions_report(http))

    assert "+фанд0.40" in report


def test_stopall_arms_then_fires(keyed, acting):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]

    first = asyncio.run(watch.close_everything(http))
    second = asyncio.run(watch.close_everything(http))

    assert first.startswith("⚠️ Закрою МАРКЕТОМ 1 поз.: BTC")
    assert "✅ BTC закрывается" in second
    assert acting.calls == [("market_close", "BTC")]


def test_stopall_needs_the_address():
    assert asyncio.run(watch.close_everything(FakeHTTP())) == watch._NO_KEYS


def test_stopall_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}

    assert asyncio.run(watch.close_everything(http)) == watch._NO_ANSWER


def test_stopall_with_nothing_open_disarms(keyed, monkeypatch):
    monkeypatch.setattr(watch, "_stopall_armed", 0.0)

    assert "нечего" in asyncio.run(watch.close_everything(FakeHTTP()))


def test_stopall_reports_refused_closes(keyed, acting):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    acting.refuse = True

    asyncio.run(watch.close_everything(http))
    answer = asyncio.run(watch.close_everything(http))

    assert "❌ BTC: refused" in answer


def test_close_one_position(keyed, acting):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]

    answer = asyncio.run(watch.close_position(http, "btc"))

    assert answer == "✅ BTC закрывается — отчёт 💸 придёт следом"
    assert acting.calls == [("market_close", "BTC")]


def test_close_needs_arguments_and_a_position(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]

    assert "Какую позицию" in asyncio.run(watch.close_position(http, " "))
    assert asyncio.run(watch.close_position(http, "ETH")) == "Позиции ETH нет. Открыто: BTC"


def test_close_needs_the_address():
    assert asyncio.run(watch.close_position(FakeHTTP(), "BTC")) == watch._NO_KEYS


def test_close_survives_refusals(keyed, acting):
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}
    assert asyncio.run(watch.close_position(http, "BTC")) == watch._NO_ANSWER

    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    acting.refuse = True
    assert "не смог закрыть" in asyncio.run(watch.close_position(http, "BTC"))


def test_lev1_named_and_every_open(keyed, acting):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(), pos_row(coin="ETH", szi="1")]
    http.fills = [fill(), fill(coin="ETH", sz="1")]

    named = asyncio.run(watch.force_leverage_one(http, "eth"))
    swept = asyncio.run(watch.force_leverage_one(http))

    assert named == "✅ ETH → 1x"
    assert swept == "✅ BTC → 1x\n✅ ETH → 1x"


def test_lev1_edges(keyed, acting):
    assert (
        asyncio.run(watch.force_leverage_one(FakeHTTP()))
        == "Нет открытых позиций — плечо ставить некому"
    )
    http = FakeHTTP()
    http.fail_types = {"clearinghouseState"}
    assert asyncio.run(watch.force_leverage_one(http)) == watch._NO_ANSWER
    acting.refuse = True
    assert "❌ BTC: refused" in asyncio.run(watch.force_leverage_one(FakeHTTP(), "BTC"))


def test_lev1_needs_the_address():
    assert asyncio.run(watch.force_leverage_one(FakeHTTP(), "BTC")) == watch._NO_KEYS


def test_stats_report_sums_daily_closes(keyed):
    http = FakeHTTP()
    now_ms = int(time.time() * 1000)
    http.fills = [
        fill(direction="Close Long", closed="120", fee="10", t=now_ms),
        fill(direction="Close Long", closed="-40", fee="5", t=now_ms - 86_400_000),
        fill(direction="Open Long", t=now_ms),  # not a close
        fill(direction="Close Long", closed="999", t=1),  # out of the window
    ]
    out = Recorder()

    report = asyncio.run(watch.stats_report(http, out.send_photo, "7"))

    assert report == ""  # the chart carried it
    assert out.photos[0].startswith("📊 за 7 дн.: закрытий 2, в плюс 1")
    assert "+65.00" in out.photos[0]


def test_stats_report_plain_text_paths(keyed):
    http = FakeHTTP()

    assert "закрытий 0" in asyncio.run(watch.stats_report(http, None, "nonsense"))
    assert asyncio.run(watch.stats_report(FakeHTTP(), None, "0")).startswith("📊 за 1 дн.")


def test_stats_report_needs_the_address():
    assert asyncio.run(watch.stats_report(FakeHTTP())) == watch._NO_KEYS


def test_stats_report_survives_a_refusal(keyed):
    http = FakeHTTP()
    http.fail_types = {"userFills"}

    assert asyncio.run(watch.stats_report(http)) == watch._NO_ANSWER


def test_stats_chart_failure_still_answers(keyed, monkeypatch):
    http = FakeHTTP()
    http.fills = [fill(direction="Close Long", closed="10", t=int(time.time() * 1000))]

    def broken(*a, **k):
        raise ValueError("no data")

    import chart

    monkeypatch.setattr(chart, "equity_curve", broken)
    out = Recorder()

    report = asyncio.run(watch.stats_report(http, out.send_photo, "7"))

    assert report.startswith("📊 за 7 дн.: закрытий 1")


def test_market_report_sends_the_snapshot(keyed):
    out = Recorder()

    asyncio.run(watch.market_report(FakeHTTP(), out.send_photo))

    assert out.photos
    assert out.photos[0].startswith("BTC ")


def test_market_report_without_a_sender_or_candles(keyed):
    out = Recorder()

    asyncio.run(watch.market_report(FakeHTTP(), None))
    http = FakeHTTP()
    http.fail_types = {"candleSnapshot"}
    asyncio.run(watch.market_report(http, out.send_photo))

    assert out.photos == []


def test_price_before_reads_the_mids(keyed):
    assert asyncio.run(watch.price_before(FakeHTTP(), "BTCUSDT")) == 100500.0
    assert asyncio.run(watch.price_before(FakeHTTP(), "GHOST")) is None
    http = FakeHTTP()
    http.fail_types = {"allMids"}
    assert asyncio.run(watch.price_before(http, "BTC")) is None


# --------------------------------------------------------------------------- #
#  money moves
# --------------------------------------------------------------------------- #
def test_money_moves_read_the_ledger(keyed):
    http = FakeHTTP()
    http.ledger = [
        {"hash": "0xa", "time": 1, "delta": {"type": "deposit", "usdc": "500"}},
        {"hash": "0xb", "time": 2, "delta": {"type": "withdraw", "usdc": "125.5"}},
        {"hash": "0xc", "time": 3, "delta": {"type": "spotTransfer", "usdc": "9"}},
    ]

    moves = asyncio.run(watch.money_moves(http))

    assert [(m["id"], m["amount"]) for m in moves] == [
        ("deposit-0xa", 500.0),
        ("withdraw-0xb", -125.5),
    ]


def test_money_tick_primes_then_announces(keyed, monkeypatch):
    http = FakeHTTP()
    http.ledger = [{"hash": "0xa", "time": 1, "delta": {"type": "deposit", "usdc": "500"}}]
    logged = []

    async def log_close(http_, entry):
        logged.append(entry)
        return True

    monkeypatch.setattr(sheets, "log_close", log_close)
    out = Recorder()

    seen = asyncio.run(watch.money_tick(http, None, out.send))
    assert out.sent == []  # priming is silent

    http.ledger.append({"hash": "0xb", "time": 2, "delta": {"type": "withdraw", "usdc": "100"}})
    seen = asyncio.run(watch.money_tick(http, seen, out.send))

    assert out.sent == ["💵 вывел -100.00 USDC · деп 1,000.00$"]
    assert logged[0]["row"][1] == "перевод"
    assert logged[0]["row"][-1] == -100.0
    assert "deposit-0xa" in seen
    assert "withdraw-0xb" in seen


def test_money_tick_skips_seen_and_missing_depo(keyed, monkeypatch):
    http = FakeHTTP()
    http.state["marginSummary"] = {}
    http.ledger = [{"hash": "0xa", "time": 1, "delta": {"type": "deposit", "usdc": "500"}}]

    async def log_close(http_, entry):
        return True

    monkeypatch.setattr(sheets, "log_close", log_close)
    out = Recorder()

    asyncio.run(watch.money_tick(http, set(), out.send))

    assert out.sent == ["💵 завел +500.00 USDC"]


# --------------------------------------------------------------------------- #
#  the poll loops
# --------------------------------------------------------------------------- #
def test_poll_backs_off_after_a_failure(keyed, monkeypatch, caplog):
    class Flaky(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.fail_types = {"clearinghouseState"}

    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)
    out = Recorder()
    attempt = watch.poll(Flaky(), out.send, out.speak, out.send_photo)

    with caplog.at_level("ERROR", logger="relay.watch"), pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)

    assert "hyperliquid poll failed" in caplog.text


def test_poll_lets_cancellation_through(keyed, monkeypatch):
    class Cancelling(FakeHTTP):
        async def post(self, url, json=None, timeout=None, data=None, files=None):
            raise asyncio.CancelledError

    attempt = watch.poll(Cancelling(), Recorder().send, Recorder().speak, Recorder().send_photo)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)


def test_money_poll_backs_off_and_cancels(keyed, monkeypatch, caplog):
    class Flaky(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.fail_types = {"userNonFundingLedgerUpdates"}

    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)
    attempt = watch.money_poll(Flaky(), Recorder().send)

    with caplog.at_level("ERROR", logger="relay.watch"), pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)

    assert "money poll failed" in caplog.text


def test_money_poll_lets_cancellation_through(keyed):
    class Cancelling(FakeHTTP):
        async def post(self, url, json=None, timeout=None, data=None, files=None):
            raise asyncio.CancelledError

    attempt = watch.money_poll(Cancelling(), Recorder().send)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(attempt)


# --------------------------------------------------------------------------- #
#  klines
# --------------------------------------------------------------------------- #
def test_bar_of_clamps_to_the_edges():
    times = [100, 200, 300]

    assert watch._bar_of(times, 50) == 0
    assert watch._bar_of(times, 250) == 1
    assert watch._bar_of(times, 999) == 2
    assert watch._bar_of([], 5) == 0


def test_entry_chart_survives_a_candle_refusal(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"candleSnapshot"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        assert asyncio.run(watch.entry_chart(http, "BTC", LONG)) is None


def test_close_chart_survives_a_candle_refusal(keyed, caplog):
    http = FakeHTTP()
    http.fail_types = {"candleSnapshot"}

    with caplog.at_level("ERROR", logger="relay.watch"):
        got = asyncio.run(watch.close_chart(http, "BTC", LONG, 100_000.0, 1_700_010_000_000))
    assert got is None


def test_close_chart_renders_the_finished_trade(keyed):
    http = FakeHTTP()
    was = Position("long", 0.5, 100_000.0, 50_000.0, created_ms=1_700_000_900_000)

    png = asyncio.run(watch.close_chart(http, "BTC", was, 100_500.0, 1_700_030_000_000))

    assert png is not None
    assert png.startswith(b"\x89PNG")


# --------------------------------------------------------------------------- #
#  the last branches
# --------------------------------------------------------------------------- #
def test_fold_triggers_ignores_unknown_kinds(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    http.orders = [trigger(kind="Twap")]

    open_now = asyncio.run(watch.positions(http))

    assert open_now["BTC"].stop_loss is None


def test_guard_keeps_memory_of_a_still_naked_order(guarding, monkeypatch):
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    watch._guard_seen["order:5"] = time.monotonic() - 1

    asyncio.run(watch.guard(FakeHTTP(), {}, [entry_order()], Recorder().send, Recorder().speak))

    assert "order:5" in watch._guard_seen


def test_trim_gives_up_when_nothing_can_be_kept(trimming):
    tiny = Position("long", 0.05, 100.0, 5.0, stop_loss=99.0)

    asyncio.run(watch.trim(trim_http(depo="1"), {"ETH": tiny}, Recorder().send, Recorder().speak))

    assert trimming.calls == []


def test_sig2_and_usd_edges():
    assert watch._sig2(0.0) == "0.00"
    assert watch._sig2(0.04213) == "0.042"
    assert watch._usd(0.5) == "+0.50"
    assert watch._usd(-0.042) == "-0.042"
    assert watch._usd(0.0) == "+0.00"
    assert watch._usd(12.3) == "+12.30"


def test_chart_notes_without_a_depo():
    tp_note, sl_note = watch._chart_notes(1.0, -1.0, None)

    assert tp_note == "+1.00"
    assert sl_note == "-1.00"


def test_positions_report_album_survives_dead_candles(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row()]
    http.fail_types = {"candleSnapshot"}
    out = Recorder()

    report = asyncio.run(watch.positions_report(http, out.send_album))

    assert report.startswith("📈BTC")  # text fallback, no album
    assert out.photos == []


def test_close_notice_without_depo_fees_or_chart(keyed, monkeypatch):
    http = FakeHTTP()
    http.state["marginSummary"] = {}
    http.fail_types = {"candleSnapshot"}
    recent = int(time.time() * 1000) - 1000
    http.fills = [
        fill(direction="Close Long", sz="0.5", px="100100", t=recent, fee="0", closed="50")
    ]
    logged = []

    async def log_close(http_, entry):
        logged.append(entry)
        return True

    monkeypatch.setattr(sheets, "log_close", log_close)

    line, spoken, png = asyncio.run(watch._close_notice(http, "BTC", LONG, "x"))

    assert line == "💸<b>+50.00</b>·📈BTC·руками"
    assert png is None
    assert "png" not in logged[0]


def test_tick_announces_a_size_change_as_text(keyed):
    http = FakeHTTP()
    http.state["assetPositions"] = [pos_row(szi="1", value="100000")]
    http.fills = [fill(sz="1")]
    out = Recorder()

    asyncio.run(watch.tick(http, {"BTC": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💰📈BTC 50,000→100,000"]


def test_stats_text_answers_when_the_photo_is_refused(keyed):
    http = FakeHTTP()
    http.fills = [fill(direction="Close Long", closed="10", t=int(time.time() * 1000))]
    out = Recorder(photo_ok=False)

    report = asyncio.run(watch.stats_report(http, out.send_photo, "7"))

    assert report.startswith("📊 за 7 дн.")
