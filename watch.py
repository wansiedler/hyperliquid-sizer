"""Watch the Hyperliquid account: announce, journal and police every trade.

The same watchdog the Bybit sizer runs, on Hyperliquid's order book:

1. A position with no stop-loss trigger → reduce-only market close.
2. A position leveraged above MAX_LEVERAGE → market close, leverage reset.
3. An entry order with no stop attached → cancelled.
4. A market entry whose stop risks more than RISK_PCT of the account →
   trimmed back down, every fee budgeted.

Hyperliquid keeps TP/SL as separate trigger orders rather than fields on
the position, so positions() folds the account's reduce-only triggers back
into each Position before anyone else looks.
"""

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import httpx

import chart
import hyper
import sheets
from coin_names import COIN_NAMES
from hyper import info

log = logging.getLogger("relay.watch")

RISK_TARGET = float(os.getenv("RISK_PCT", "1")) / 100
MAKER_FEE = float(os.getenv("MAKER_FEE", "0.00015"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.00045"))
RISK_GUARD = os.getenv("RISK_GUARD", "1") == "1"
RISK_TRIM = os.getenv("RISK_TRIM", "1") == "1"
GUARD_MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "1"))
GUARD_GRACE = float(os.getenv("GUARD_GRACE_SEC", "45"))
MIN_RR = float(os.getenv("MIN_RR", "2"))
POLL = float(os.getenv("POLL_SEC", "2"))

CHART_INTERVAL = os.getenv("CHART_INTERVAL", "15")
CHART_BARS = min(int(os.getenv("CHART_BARS", "180")), 1000)

_NO_KEYS = "HL_ACCOUNT не задан — смотреть не на что"
_NO_ANSWER = "Hyperliquid не ответил — попробуй ещё раз"


def enabled() -> bool:
    return hyper.enabled()


@dataclass(frozen=True)
class Position:
    side: str  # "long" | "short"
    size: float  # in the base coin, always positive
    price: float  # average entry
    value: float  # position value, USDC
    take_profit: float | None = None
    stop_loss: float | None = None
    unrealised: float = 0.0
    leverage: float = 0.0
    created_ms: int = 0


def _position_of(row: dict) -> Position:
    """One clearinghouseState assetPosition as the watcher's Position."""
    szi = float(row.get("szi") or 0)
    leverage = row.get("leverage") or {}
    return Position(
        side="long" if szi > 0 else "short",
        size=abs(szi),
        price=float(row.get("entryPx") or 0),
        value=float(row.get("positionValue") or 0),
        unrealised=float(row.get("unrealizedPnl") or 0),
        leverage=float(leverage.get("value") or 0),
    )


def _fold_triggers(open_now: dict[str, Position], orders: list[dict]) -> None:
    """Give the account's reduce-only triggers back to their positions.

    Hyperliquid keeps TP/SL as standalone trigger orders; the watcher (and
    every notice, chart and journal row) wants them on the Position.
    """
    for order in orders:
        position = open_now.get(order.get("coin", ""))
        if position is None or not order.get("isTrigger") or not order.get("reduceOnly"):
            continue
        price = float(order.get("triggerPx") or 0)
        if price <= 0:
            continue
        kind = str(order.get("orderType", ""))
        if "Stop" in kind:
            open_now[order["coin"]] = replace(position, stop_loss=price)
        elif "Take Profit" in kind:
            open_now[order["coin"]] = replace(position, take_profit=price)


async def open_orders(http: httpx.AsyncClient) -> list[dict]:
    """Every resting order, triggers included, as the frontend sees them."""
    result = await info(http, {"type": "frontendOpenOrders", "user": hyper.ACCOUNT})
    return result if isinstance(result, list) else []


async def positions(http: httpx.AsyncClient) -> dict[str, Position]:
    """Open perp positions by coin, TP/SL folded in, zero sizes dropped."""
    state = await info(http, {"type": "clearinghouseState", "user": hyper.ACCOUNT})
    open_now: dict[str, Position] = {}
    for wrapped in state.get("assetPositions", []):
        row = wrapped.get("position") or {}
        if float(row.get("szi") or 0) != 0:
            open_now[row["coin"]] = _position_of(row)
    if open_now:
        try:
            _fold_triggers(open_now, await open_orders(http))
        # Deliberately broad: no order answer only hides the TP/SL lines.
        except Exception:  # noqa: BLE001
            log.exception("no order listing for tp/sl")
        await _stamp_births(http, open_now)
    return open_now


# When each coin's position was first pieced together from the fills, so
# the fill walk does not repeat on every poll.
_births: dict[tuple[str, str, float], int] = {}


def position_birth(fills: list[dict], coin: str, size: float) -> int:
    """When the current position started, walked back through the fills.

    Newest first: the moment the accumulated opening size reaches the
    position's own is the position's first fill. Zero when the fills
    window (Hyperliquid serves the most recent ~2000) does not reach it.
    """
    remaining = size
    born = 0
    for fill in sorted(
        (f for f in fills if f.get("coin") == coin), key=lambda f: -int(f.get("time") or 0)
    ):
        direction = str(fill.get("dir", ""))
        if not direction.startswith("Open"):
            continue
        remaining -= float(fill.get("sz") or 0)
        born = int(fill.get("time") or 0)
        if remaining <= 1e-12:
            return born
    return 0


async def _stamp_births(http: httpx.AsyncClient, open_now: dict[str, Position]) -> None:
    """Fill in created_ms for each position, cached per (coin, side, size)."""
    missing = [(coin, p) for coin, p in open_now.items() if (coin, p.side, p.size) not in _births]
    if missing:
        try:
            fills = await user_fills(http)
        # Deliberately broad: an unknown birth only blunts the funding math —
        # and stays uncached, so the next poll asks again.
        except Exception:  # noqa: BLE001
            log.exception("no fills for the position births")
            fills = None
        if fills is not None:
            for coin, position in missing:
                _births[(coin, position.side, position.size)] = position_birth(
                    fills, coin, position.size
                )
    for coin, position in open_now.items():
        open_now[coin] = replace(
            position, created_ms=_births.get((coin, position.side, position.size), 0)
        )


async def user_fills(http: httpx.AsyncClient) -> list[dict]:
    result = await info(http, {"type": "userFills", "user": hyper.ACCOUNT})
    return result if isinstance(result, list) else []


async def equity(http: httpx.AsyncClient) -> float | None:
    """The account value, unrealised PnL included — the depo figure."""
    try:
        state = await info(http, {"type": "clearinghouseState", "user": hyper.ACCOUNT})
        value = state.get("marginSummary", {}).get("accountValue")
        return float(value) if value is not None else None
    # Deliberately broad: a depo figure is garnish on every notice.
    except Exception:  # noqa: BLE001
        log.exception("no equity answer")
        return None


async def wallet_balance(http: httpx.AsyncClient) -> float | None:
    """The risk base: account value with the unrealised PnL taken out —
    an open loser must not shrink what the next stop is allowed to cost."""
    try:
        state = await info(http, {"type": "clearinghouseState", "user": hyper.ACCOUNT})
        value = float(state.get("marginSummary", {}).get("accountValue") or 0)
        floating = sum(
            float((w.get("position") or {}).get("unrealizedPnl") or 0)
            for w in state.get("assetPositions", [])
        )
        return value - floating
    # Deliberately broad: no answer means no trim this poll, not a crash.
    except Exception:  # noqa: BLE001
        log.exception("no wallet answer")
        return None


async def accrued_funding(http: httpx.AsyncClient, coin: str, since_ms: int) -> float:
    """Funding actually charged on the position since it opened.

    Hyperliquid's ledger reports the payment as a signed usdc delta —
    positive received. The watcher wants cost: positive paid.
    """
    if not since_ms:
        return 0.0
    try:
        rows = await info(
            http, {"type": "userFunding", "user": hyper.ACCOUNT, "startTime": since_ms}
        )
        return -sum(
            float((r.get("delta") or {}).get("usdc") or 0)
            for r in rows
            if (r.get("delta") or {}).get("coin") == coin
        )
    # Deliberately broad: the funding figure is garnish, zero is fine.
    except Exception:  # noqa: BLE001
        log.exception("no funding history for %s", coin)
        return 0.0


async def entry_fee_per_unit(
    http: httpx.AsyncClient, coin: str, position: Position
) -> float | None:
    """The real entry cost per unit, from the opening fills.

    None when the fills window does not reach the position's birth — the
    caller assumes a taker entry, the pessimistic bound.
    """
    if not position.created_ms or position.size <= 0:
        return None
    try:
        fills = await user_fills(http)
    # Deliberately broad: an estimate is a fine substitute for history.
    except Exception:  # noqa: BLE001
        log.exception("no fills for %s", coin)
        return None
    fees = sum(
        float(f.get("fee") or 0)
        for f in fills
        if f.get("coin") == coin
        and int(f.get("time") or 0) >= position.created_ms
        and str(f.get("dir", "")).startswith("Open")
    )
    return fees / position.size if fees > 0 else None


def breakeven_price(
    side: str,
    entry: float,
    extra_cost_per_unit: float = 0.0,
    entry_fee_per_unit: float | None = None,
) -> float:
    """The exit price where the trade nets zero, taker exit assumed."""
    t = TAKER_FEE
    paid = entry * t if entry_fee_per_unit is None else entry_fee_per_unit
    if side == "long":
        return (entry + paid + extra_cost_per_unit) / (1 - t)
    return (entry - paid - extra_cost_per_unit) / (1 + t)


# ---------------------------------------------------------------------------
#  lot steps
# ---------------------------------------------------------------------------
_sz_decimals: dict[str, int] = {}


async def _lot(http: httpx.AsyncClient, coin: str) -> float:
    """The size step for one coin, cached from the exchange metadata."""
    if not _sz_decimals:
        meta = await info(http, {"type": "meta"})
        for asset in meta.get("universe", []):
            _sz_decimals[asset["name"]] = int(asset.get("szDecimals") or 0)
    if coin not in _sz_decimals:
        return 0.0
    return float(10 ** -_sz_decimals[coin])


def _fmt_size(size: float, step: float) -> float:
    """A size rounded onto the exchange grid, floats forgiven."""
    decimals = max(0, round(-math.log10(step))) if step > 0 else 8
    return round(size, decimals)


# ---------------------------------------------------------------------------
#  the risk guard
# ---------------------------------------------------------------------------
_guard_seen: dict[str, float] = {}
_guard_closed: dict[str, str] = {}


def _guard_violation(position: Position) -> str | None:
    if not position.stop_loss:
        return "нет стопа"
    if GUARD_MAX_LEVERAGE and position.leverage > GUARD_MAX_LEVERAGE:
        return f"плечо {position.leverage:g}x > {GUARD_MAX_LEVERAGE:g}x"
    return None


def _naked_entries(orders: list[dict], open_now: dict[str, Position]) -> dict[str, dict]:
    """Entry orders with no stop anywhere: no trigger child, no position SL."""
    protected = {
        o.get("coin")
        for o in orders
        if o.get("isTrigger") and o.get("reduceOnly") and "Stop" in str(o.get("orderType", ""))
    }
    naked = {}
    for order in orders:
        if order.get("isTrigger") or order.get("reduceOnly"):
            continue
        coin = order.get("coin", "")
        position = open_now.get(coin)
        covered = coin in protected or (position is not None and position.stop_loss)
        children = order.get("children") or []
        has_child_stop = any("Stop" in str(c.get("orderType", "")) for c in children)
        if not covered and not has_child_stop:
            naked[f"order:{order.get('oid')}"] = order
    return naked


def _forget_fixed(naked: dict[str, dict], open_now: dict[str, Position]) -> None:
    """Drop guard memory for breaches that healed themselves."""
    for key in list(_guard_seen):
        if key.startswith("order:"):
            if key not in naked:
                del _guard_seen[key]
        else:
            fixed = open_now.get(key)
            if fixed is None or _guard_violation(fixed) is None:
                del _guard_seen[key]


async def _grace_holds(key: str, warning: str, spoken: str, send, speak) -> bool:
    """Whether enforcement waits: warn on first sight, hold inside the window."""
    now = time.monotonic()
    first = _guard_seen.get(key)
    if first is None:
        _guard_seen[key] = now
        if GUARD_GRACE > 0:
            await send(warning)
            await speak(spoken)
            return True
    elif now - first < (GUARD_GRACE or 30.0):
        return True
    else:
        _guard_seen[key] = now
    return False


def _say(coin: str) -> str:
    return COIN_NAMES.get(coin, coin)


async def _cancel_naked(http: httpx.AsyncClient, naked: dict[str, dict], send, speak) -> None:
    """Cancel entry orders that still have no stop once the grace runs out."""
    for key, order in naked.items():
        coin = order.get("coin", "")
        if await _grace_holds(
            key,
            f"🛑 {coin}: лимитка без стопа — отменю через {GUARD_GRACE:.0f}с",
            f"{_say(coin)} naked order",
            send,
            speak,
        ):
            continue
        try:
            await hyper.cancel_order(coin, int(order["oid"]))
            await send(f"🛑 {coin}: лимитка без стопа отменена риск-менеджером")
        # Deliberately broad: one refused cancel must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("guard cancel failed for %s", coin)
            await send(f"❌ {coin}: не смог отменить лимитку ({exc})")


async def _enforce_rules(
    http: httpx.AsyncClient, open_now: dict[str, Position], send, speak
) -> None:
    """Market-close positions that still break a rule once the grace runs out."""
    for coin, position in open_now.items():
        reason = _guard_violation(position)
        if reason is None:
            continue
        if await _grace_holds(
            coin,
            f"🛑 {coin}: {reason} — закрою маркетом через {GUARD_GRACE:.0f}с",
            f"{_say(coin)} risk breach",
            send,
            speak,
        ):
            continue
        try:
            await hyper.market_close(coin)
            _guard_closed[coin] = reason
            note = ""
            if reason.startswith("плечо"):
                try:
                    await hyper.update_leverage(coin, 1)
                    note = " · плечо сброшено на 1x"
                # Deliberately broad: the close already went through.
                except Exception:  # noqa: BLE001
                    log.exception("leverage reset failed for %s", coin)
            await send(f"🛑 {coin} закрыт маркетом риск-менеджером: {reason}{note}")
        # Deliberately broad: the guard keeps watching past one refusal.
        except Exception as exc:  # noqa: BLE001
            log.exception("guard close failed for %s", coin)
            await send(f"❌ {coin}: риск-менеджер не смог закрыть ({exc})")


async def guard(
    http: httpx.AsyncClient, open_now: dict[str, Position], orders: list[dict], send, speak
) -> None:
    """The risk manager: warn about a rule breach, then enforce it."""
    if not RISK_GUARD:
        return
    naked = _naked_entries(orders, open_now)
    _forget_fixed(naked, open_now)
    await _cancel_naked(http, naked, send, speak)
    await _enforce_rules(http, open_now, send, speak)


# ---------------------------------------------------------------------------
#  the trimmer
# ---------------------------------------------------------------------------
_trim_cooldown: dict[str, float] = {}


async def trim(http: httpx.AsyncClient, open_now: dict[str, Position], send, speak) -> None:
    """Cut an oversized position back to the target risk.

    A market entry keeps the quantity sized for a different price, so the
    stop suddenly risks more than RISK_PCT. The stop's true cost includes
    every taker fee on the way: the market entry, the cut's own market
    close, and the stop's close on what is kept.
    """
    if not RISK_TRIM:
        return
    depo = None
    for coin, position in open_now.items():
        if not position.stop_loss or position.size <= 0:
            continue
        if time.monotonic() - _trim_cooldown.get(coin, 0.0) < 10.0:
            continue
        if abs(position.price - position.stop_loss) <= 0:
            continue
        if depo is None:
            depo = await wallet_balance(http)
        if not depo:
            return
        await _trim_cut(http, coin, position, depo, send, speak)


async def _trim_cut(
    http: httpx.AsyncClient, coin: str, position: Position, depo: float, send, speak
) -> None:
    """Cut one position whose stop-out overshoots the target, if it does."""
    stop = position.stop_loss or 0.0
    distance = abs(position.price - stop)
    entry_fee = position.price * TAKER_FEE
    stop_fee = stop * TAKER_FEE
    per_unit = distance + entry_fee + stop_fee
    risk = per_unit * position.size
    target = depo * RISK_TARGET
    if risk <= target:
        return
    step = await _lot(http, coin)
    if step <= 0:
        return
    # target >= entry_fee*size + entry_fee*(size-want) + want*(distance+stop_fee)
    budget = target - 2 * position.size * entry_fee
    want = _fmt_size(math.floor(budget / (distance + stop_fee - entry_fee) / step) * step, step)
    cut = _fmt_size(position.size - want, step)
    if want < step or cut < step:
        return
    _trim_cooldown[coin] = time.monotonic()
    try:
        await hyper.market_close(coin, cut)
        await send(
            f"✂️ {coin}: риск {risk / depo * 100:.2f}% депо при цели "
            f"{RISK_TARGET * 100:.2f}% — режу {position.size:g}→{want:g}"
        )
        await speak(f"{_say(coin)} trimmed")
    # Deliberately broad: one refused cut must not strand the rest.
    except Exception as exc:  # noqa: BLE001
        log.exception("trim failed for %s", coin)
        await send(f"❌ {coin}: не смог подрезать ({exc})")


# ---------------------------------------------------------------------------
#  money formatting (ported verbatim from the Bybit sizer)
# ---------------------------------------------------------------------------
def _sig2(amount: float) -> str:
    """Two leading fraction digits: 0.5320 -> 0.53, 0.04213 -> 0.042."""
    if amount == 0:
        return "0.00"
    decimals = max(2, 1 - math.floor(math.log10(abs(amount))))
    return f"{amount:.{decimals}f}"


def _usd(amount: float) -> str:
    """Signed money: cents normally, two significant fraction digits below."""
    if abs(amount) >= 0.995:
        return f"{amount:+,.2f}"
    if amount > 0:
        return f"+{_sig2(amount)}"
    if amount < 0:
        return f"-{_sig2(abs(amount))}"
    return "+0.00"


def _val(value: float) -> str:
    return f"{value:,.0f}" if value >= 10 else f"{value:,.2f}"


def _arrow(side: str) -> str:
    return "📈" if side == "long" else "📉"


def _share(amount: float, depo: float | None) -> str:
    return f"({amount / depo * 100:+.2f}%)" if depo else ""


# ---------------------------------------------------------------------------
#  charts
# ---------------------------------------------------------------------------
async def _klines(
    http: httpx.AsyncClient, coin: str, bars: int, end_ms: int | None = None
) -> tuple[list[int], list[chart.Candle]]:
    """The last `bars` candles on CHART_INTERVAL, with their open times."""
    span = int(CHART_INTERVAL) * 60_000
    end = end_ms or int(time.time() * 1000)
    rows = await info(
        http,
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": f"{CHART_INTERVAL}m",
                "startTime": end - bars * span,
                "endTime": end,
            },
        },
    )
    times = [int(r["t"]) for r in rows]
    candles = [
        chart.Candle(float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in rows
    ]
    return times, candles


def _bar_of(times: list[int], moment_ms: int) -> int:
    """The bar index a moment falls into; clamped to the edges."""
    for i, t in enumerate(times):
        if moment_ms < t:
            return max(0, i - 1)
    return max(0, len(times) - 1)


async def entry_chart(
    http: httpx.AsyncClient,
    coin: str,
    position: Position,
    entry_note: str = "",
    tp_note: str = "",
    sl_note: str = "",
    breakeven: float | None = None,
) -> bytes | None:
    """A PNG of recent candles with the entry, TP and SL drawn in."""
    try:
        _, candles = await _klines(http, coin, CHART_BARS)
        return chart.render(
            coin,
            position.side,
            candles,
            position.price,
            position.take_profit,
            position.stop_loss,
            entry_index=len(candles) - 1,
            pad_right=max(CHART_BARS // 6, 4),
            timeframe=f"{CHART_INTERVAL}m",
            notes=chart.Notes(entry=entry_note, tp=tp_note, sl=sl_note),
            breakeven=breakeven,
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no chart for %s", coin)
        return None


async def close_chart(
    http: httpx.AsyncClient,
    coin: str,
    was: Position,
    exit_price: float,
    closed_ms: int,
    exit_note: str = "",
) -> bytes | None:
    """A PNG of the finished trade: entry to exit, zone colored by outcome."""
    try:
        span = int(CHART_INTERVAL) * 60_000
        opened = was.created_ms or closed_ms
        trade_bars = (closed_ms - opened) // span
        lead = max(CHART_BARS - trade_bars - 3, 8)
        bars = min(trade_bars + lead + 4, 3000)
        times, candles = await _klines(http, coin, bars, end_ms=closed_ms + 3 * span)
        return chart.render(
            coin,
            was.side,
            candles,
            was.price,
            was.take_profit,
            was.stop_loss,
            entry_index=_bar_of(times, opened),
            exit_at=(_bar_of(times, closed_ms), exit_price),
            pad_right=2,
            timeframe=f"{CHART_INTERVAL}m",
            notes=chart.Notes(exit=exit_note),
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no close chart for %s", coin)
        return None


# ---------------------------------------------------------------------------
#  /positions
# ---------------------------------------------------------------------------
def _chart_notes(target: float | None, at_sl: float | None, depo: float | None) -> tuple[str, str]:
    """The chart's TP and SL annotations, with the deposit they would leave."""
    tp_note = sl_note = ""
    if target is not None:
        tp_note = f"{target:+,.2f}{_share(target, depo)}"
        if depo:
            tp_note += f" = {depo + target:,.2f}$"
    if at_sl is not None:
        sl_note = f"{at_sl:+,.2f}{_share(at_sl, depo)}"
        if depo:
            sl_note += f" = {depo + at_sl:,.2f}$"
    return tp_note, sl_note


def _position_block(
    coin: str, position: Position, fees: float, funding: float, net: float, depo: float | None
) -> tuple[str, float | None, float | None, float | None]:
    """One /positions block, plus (rr, at_sl, at_tp) for the chart notes."""
    head = f"{_arrow(position.side)}{coin} {_val(position.value)}$@{position.price:g}"
    rr = None
    if position.stop_loss and position.take_profit:
        rr = abs(position.take_profit - position.price) / abs(position.price - position.stop_loss)
        head += f" | RR{rr:.2f}"
    exits = []
    at_sl = at_tp = None
    if position.stop_loss:
        at_sl = -abs(position.price - position.stop_loss) * position.size - fees - funding
        exits.append(f"sl{position.stop_loss:g}:<b>{_usd(at_sl)}{_share(at_sl, depo)}</b>")
    if position.take_profit:
        sign = 1 if position.side == "long" else -1
        at_tp = sign * (position.take_profit - position.price) * position.size - fees - funding
        tp_depo = f"=деп{depo + at_tp:,.2f}$" if depo else ""
        exits.append(
            f"tp{position.take_profit:g}:<b>{_usd(at_tp)}{_share(at_tp, depo)}{tp_depo}</b>"
        )
    fund_note = ""
    if funding > 0:
        fund_note = f"−фанд{_sig2(funding)}"
    elif funding < 0:
        fund_note = f"+фанд{_sig2(-funding)}"
    pnl = (
        f"PnL{position.unrealised:+,.2f}−комса{_sig2(fees)}{fund_note}"
        f"=<b>{net:+,.2f}{_share(net, depo)}</b>"
    )
    return "\n".join([head, *exits, pnl]), rr, at_sl, at_tp


async def _position_chart(
    http: httpx.AsyncClient,
    coin: str,
    position: Position,
    real: float | None,
    fees: float,
    funding: float,
    net: float,
    rr: float | None,
    at_sl: float | None,
    at_tp: float | None,
    depo: float | None,
) -> bytes | None:
    """The /positions album chart for one position, breakeven drawn in."""
    be = breakeven_price(
        position.side,
        position.price,
        funding / position.size if position.size else 0.0,
        entry_fee_per_unit=real,
    )
    tp_note, sl_note = _chart_notes(at_tp, at_sl, depo)
    return await entry_chart(
        http,
        coin,
        position,
        entry_note=(
            f"{_val(position.value)}$"
            + (f" ({position.value / depo * 100:.1f}% depo)" if depo else "")
            + (f" | RR {rr:.2f}" if rr is not None else "")
            + f" | PnL {position.unrealised:+,.2f} - fee {fees:.2f}"
            + f" = {net:+,.2f}{_share(net, depo)}"
        ),
        tp_note=tp_note,
        sl_note=sl_note,
        breakeven=be,
    )


async def positions_report(http: httpx.AsyncClient, send_album=None) -> str:
    """Every open position as one block, for /positions; charts as an album."""
    if not enabled():
        return _NO_KEYS
    try:
        open_now = await positions(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("positions report failed")
        return _NO_ANSWER
    if not open_now:
        return "Открытых позиций нет"
    depo = await equity(http)
    lines = []
    pngs = []
    total = 0.0
    total_net = 0.0
    for coin, position in sorted(open_now.items()):
        real = await entry_fee_per_unit(http, coin, position)
        paid = real * position.size if real is not None else TAKER_FEE * position.value
        fees = paid + TAKER_FEE * position.value
        funding = await accrued_funding(http, coin, position.created_ms)
        net = position.unrealised - fees - funding
        block, rr, at_sl, at_tp = _position_block(coin, position, fees, funding, net, depo)
        total += position.unrealised
        total_net += net
        lines.append(block)
        if send_album is not None:
            png = await _position_chart(
                http, coin, position, real, fees, funding, net, rr, at_sl, at_tp, depo
            )
            if png is not None:
                pngs.append(png)
    if len(lines) > 1:
        lines.append(f"ΣPnL{_usd(total)}=<b>{_usd(total_net)}{_share(total_net, depo)}</b>")
    text = "\n".join(lines)
    # Telegram caps a media-group caption at 1024 characters.
    if send_album is not None and pngs and len(text) <= 1024 and await send_album(text, pngs):
        return ""
    return text


# ---------------------------------------------------------------------------
#  changes, notices and the journal
# ---------------------------------------------------------------------------
def diff(
    before: dict[str, Position], after: dict[str, Position]
) -> list[tuple[str, str, Position | None, Position | None]]:
    """(kind, coin, was, now) for every change between two snapshots."""
    changes: list[tuple[str, str, Position | None, Position | None]] = []
    for coin, now in after.items():
        was = before.get(coin)
        if was is None:
            changes.append(("opened", coin, None, now))
        elif was.side != now.side:
            changes.append(("flipped", coin, was, now))
        elif was.size != now.size:
            changes.append(("changed", coin, was, now))
    changes.extend(("closed", coin, was, None) for coin, was in before.items() if coin not in after)
    return changes


def describe(kind: str, coin: str, was: Position | None, now: Position | None) -> tuple[str, str]:
    """One change as (bot line, spoken line)."""
    name = _say(coin)
    if kind == "opened" and now is not None:
        return (
            f"💰{_arrow(now.side)}{coin} {_val(now.value)}$@{now.price:g}",
            f"{name} {now.side} opened",
        )
    if kind == "flipped" and now is not None:
        return (
            f"💰{coin} → {now.side} {now.value:,.0f}@{now.price:g}",
            f"{name} flipped to {now.side}",
        )
    if kind == "changed" and was is not None and now is not None:
        word = "increased" if now.size > was.size else "reduced"
        return (
            f"💰{_arrow(now.side)}{coin} {was.value:,.0f}→{now.value:,.0f}",
            f"{name} {now.side} {word}",
        )
    assert was is not None  # closed  # noqa: S101
    return f"💸{_arrow(was.side)}{coin}", f"{name} {was.side} closed"


def trade_warnings(position: Position, depo: float | None) -> list[str]:
    """RR below MIN_RR — a warning, never an action."""
    warnings = []
    if position.take_profit is not None and position.stop_loss and MIN_RR:
        rr = abs(position.take_profit - position.price) / abs(position.price - position.stop_loss)
        if rr < MIN_RR:
            warnings.append(f"⚠️ RR {rr:.2f} < {MIN_RR:g}")
    return warnings


def _exit_lines(
    now: Position, fees: float, depo: float | None
) -> tuple[float | None, float | None, list[str]]:
    """(at_sl, target, notice lines): what each exit costs or pays, fees off."""
    at_sl = target = None
    exits = []
    if now.stop_loss is not None:
        at_sl = -abs(now.price - now.stop_loss) * now.size - fees
        exits.append(f"sl{now.stop_loss:g}:<b>{_usd(at_sl)}{_share(at_sl, depo)}</b>")
    if now.take_profit is not None:
        target = abs(now.take_profit - now.price) * now.size - fees
        tp_depo = f"=деп{depo + target:,.2f}$" if depo else ""
        exits.append(f"tp{now.take_profit:g}:<b>{_usd(target)}{_share(target, depo)}{tp_depo}</b>")
    return at_sl, target, exits


async def _entry_fee_recent(http: httpx.AsyncClient, coin: str) -> float:
    """Fees paid on the fills of the last minute — the entry that just landed."""
    try:
        fills = await user_fills(http)
        cutoff = time.time() * 1000 - 60_000
        return sum(
            float(f.get("fee") or 0)
            for f in fills
            if f.get("coin") == coin and int(f.get("time") or 0) >= cutoff
        )
    # Deliberately broad: a fee figure is garnish on the entry notice.
    except Exception:  # noqa: BLE001
        log.exception("no entry fee for %s", coin)
        return 0.0


async def _entry_notice(
    http: httpx.AsyncClient, coin: str, now: Position, line: str
) -> tuple[str, bytes | None]:
    """The entry notice with exits, fees and warnings, plus its chart."""
    import html

    fee = await _entry_fee_recent(http, coin)
    depo = await equity(http)
    fees = 2 * (fee or TAKER_FEE * now.value)
    rr = None
    if now.stop_loss is not None and now.take_profit is not None:
        rr = abs(now.take_profit - now.price) / abs(now.price - now.stop_loss)
        line += f" | RR{rr:.2f}"
    at_sl, target, exits = _exit_lines(now, fees, depo)
    if exits:
        line += "\n" + "\n".join(exits)
    if fee:
        line += f"\nкомса{_sig2(fee)}"
    for warn in trade_warnings(now, depo):
        line += f"\n{html.escape(warn)}"
    tp_note, sl_note = _chart_notes(target, at_sl, depo)
    png = await entry_chart(
        http,
        coin,
        now,
        breakeven=breakeven_price(
            now.side,
            now.price,
            entry_fee_per_unit=fee / now.size if fee and now.size else None,
        ),
        entry_note=(
            f"{_val(now.value)}$"
            + (f" ({now.value / depo * 100:.1f}% depo)" if depo else "")
            + (f" | RR {rr:.2f}" if rr is not None else "")
            + (f" | fee {fee:.2f}" if fee else "")
        ),
        tp_note=tp_note,
        sl_note=sl_note,
    )
    return line, png


def closing_result(fills: list[dict], coin: str, since_ms: int) -> tuple[float, float, float, int]:
    """(pnl net of fees, fees, exit price, closed time) from the closing fills.

    Hyperliquid stamps each closing fill with its own closedPnl — gross of
    that fill's fee — so the trade's net is the sum minus the closing fees.
    """
    pnl = fees = exit_px = 0.0
    closed_ms = 0
    for f in fills:
        if f.get("coin") != coin or int(f.get("time") or 0) < since_ms:
            continue
        if not str(f.get("dir", "")).startswith("Close"):
            continue
        pnl += float(f.get("closedPnl") or 0)
        fees += float(f.get("fee") or 0)
        exit_px = float(f.get("px") or 0)
        closed_ms = max(closed_ms, int(f.get("time") or 0))
    return pnl - fees, fees, exit_px, closed_ms


def close_kind(was: Position, exit_price: float, forced: str) -> str:
    """стоп, тейк or руками, judged by where the exit landed.

    Hyperliquid's fills say nothing about which order closed the trade, so
    the level nearest the exit (within 0.2%) gets the credit.
    """
    if forced:
        return "риск-гард"
    for level, label in ((was.stop_loss, "стоп"), (was.take_profit, "тейк")):
        if level and abs(exit_price - level) <= level * 0.002:
            return label
    return "руками"


def journal_row(
    coin: str,
    was: Position,
    pnl: float,
    fees: float,
    exit_price: float,
    depo: float | None = None,
    forced: str = "",
    kind: str = "",
) -> list:
    """One trade as a row of the trading-diary sheet."""
    entry = was.price
    opened = (
        datetime.fromtimestamp(was.created_ms / 1000).strftime("%d.%m.%y") if was.created_ms else ""
    )
    rr = f"принудительно остановлено: {forced}" if forced else ""
    if was.stop_loss and was.take_profit:
        rr = f"1к{abs(was.take_profit - entry) / abs(entry - was.stop_loss):.1f}"
    risk = abs(entry - was.stop_loss) * was.size if was.stop_loss else 0.0
    fact: float = round(pnl / risk, 2) if risk else round(pnl, 2)
    return [
        opened,
        coin,
        "Лонг" if was.side == "long" else "Шорт",
        "win" if pnl >= 0 else "stop",
        rr,
        fact,
        "",
        round(was.value, 2),
        entry,
        exit_price or "",
        float(_sig2(pnl)),
        round(pnl / depo * 100, 2) if depo else "",
        _sig2(fees) if fees else "",
        round(depo, 2) if depo else "",
        "",
        kind,
    ]


async def _close_notice(
    http: httpx.AsyncClient, coin: str, was: Position, spoken_line: str
) -> tuple[str | None, str, bytes | None]:
    """The close notice, its chart and the journal write; None with no fills."""
    import base64

    try:
        fills = await user_fills(http)
    # Deliberately broad: the plain describe() line still goes out.
    except Exception:  # noqa: BLE001
        log.exception("no fills after close of %s", coin)
        return None, spoken_line, None
    since = was.created_ms or int(time.time() * 1000) - 24 * 3600 * 1000
    pnl, fees, exit_px, closed_ms = closing_result(fills, coin, since)
    if not closed_ms:
        return None, spoken_line, None
    funding = await accrued_funding(http, coin, was.created_ms)
    pnl -= funding
    depo = await equity(http)
    line = f"💸<b>{_usd(pnl)}</b>"
    if depo:
        line += f"=<b>{depo:,.2f}$</b>{_share(pnl, depo)}"
    line += f"·{_arrow(was.side)}{coin}"
    spoken_line += f", {'profit' if pnl >= 0 else 'loss'} {abs(pnl):.0f}"
    png = await close_chart(
        http,
        coin,
        was,
        exit_px,
        closed_ms,
        exit_note=f"PnL {pnl + fees:+,.2f} - fee {fees:.2f} = {pnl:+,.2f}{_share(pnl, depo)}",
    )
    forced = _guard_closed.pop(coin, "")
    kind = close_kind(was, exit_px, forced)
    line += f"·{kind}"
    if fees or funding:
        costs = f"комса {_sig2(fees)}"
        if funding:
            costs += f", фанд {_sig2(funding)}"
        line += f" ({costs})"
    entry: dict = {"row": journal_row(coin, was, pnl, fees, exit_px, depo, forced, kind)}
    if png is not None:
        entry["png"] = base64.b64encode(png).decode()
        entry["name"] = f"{coin}-{closed_ms}"
    await sheets.log_close(http, entry)
    return line, spoken_line, png


async def tick(
    http: httpx.AsyncClient, before: dict[str, Position] | None, send, speak, send_photo
) -> dict[str, Position]:
    """One poll: fetch, police, announce every change, hand back the snapshot."""
    after = await positions(http)
    try:
        orders = await open_orders(http)
    # Deliberately broad: no order list only blunts the naked-order rule.
    except Exception:  # noqa: BLE001
        log.exception("no open orders")
        orders = []
    await guard(http, after, orders, send, speak)
    await trim(http, after, send, speak)
    if before is None:
        return after
    for kind, coin, was, now in diff(before, after):
        line, spoken_line = describe(kind, coin, was, now)
        png = None
        if kind in ("opened", "flipped") and now is not None:
            line, png = await _entry_notice(http, coin, now, line)
        elif kind == "closed" and was is not None:
            closed_line, spoken_line, png = await _close_notice(http, coin, was, spoken_line)
            if closed_line is not None:
                line = closed_line
        if png is None or not await send_photo(line, png):
            await send(line)
        await speak(spoken_line)
    return after


async def poll(http: httpx.AsyncClient, send, speak, send_photo) -> None:
    """Announce position changes until cancelled. One failure never ends it."""
    log.info("watching hyperliquid positions every %ss", POLL)
    snapshot: dict[str, Position] | None = None
    while True:
        try:
            snapshot = await tick(http, snapshot, send, speak, send_photo)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: API hiccups and bad JSON must not end the watch.
        except Exception:  # noqa: BLE001
            log.exception("hyperliquid poll failed")
        await asyncio.sleep(POLL)


# ---------------------------------------------------------------------------
#  deposits and withdrawals
# ---------------------------------------------------------------------------
async def money_moves(http: httpx.AsyncClient) -> list[dict]:
    """Deposits and withdrawals of the last 7 days, from the account ledger."""
    since = int((time.time() - 7 * 86400) * 1000)
    rows = await info(
        http,
        {"type": "userNonFundingLedgerUpdates", "user": hyper.ACCOUNT, "startTime": since},
    )
    moves: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        delta = row.get("delta") or {}
        kind = delta.get("type")
        if kind not in ("deposit", "withdraw"):
            continue
        amount = float(delta.get("usdc") or 0)
        moves.append(
            {
                "id": f"{kind}-{row.get('hash') or row.get('time')}",
                "amount": amount if kind == "deposit" else -amount,
                "coin": "USDC",
            }
        )
    return moves


async def money_tick(http: httpx.AsyncClient, seen: set[str] | None, send) -> set[str]:
    """One money poll: announce and journal every move not seen before."""
    moves = await money_moves(http)
    ids = {str(m["id"]) for m in moves}
    if seen is None:
        return ids
    for m in moves:
        if m["id"] in seen:
            continue
        amount = float(m["amount"])
        word = "завел" if amount > 0 else "вывел"
        depo = await equity(http)
        line = f"💵 {word} {amount:+,.2f} {m['coin']}"
        if depo:
            line += f" · деп {depo:,.2f}$"
        await send(line)
        row: list = [datetime.now().strftime("%d.%m.%y"), "перевод"] + [""] * 11
        row += [round(depo, 2) if depo else "", round(amount, 2)]
        await sheets.log_close(http, {"row": row})
    return seen | ids


async def money_poll(http: httpx.AsyncClient, send) -> None:
    """Watch deposits and withdrawals until cancelled."""
    log.info("watching hyperliquid transfers every 60s")
    seen: set[str] | None = None
    while True:
        try:
            seen = await money_tick(http, seen, send)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: the watcher must idle through refusals.
        except Exception:  # noqa: BLE001
            log.exception("money poll failed")
        await asyncio.sleep(60.0)


# ---------------------------------------------------------------------------
#  chat command actions
# ---------------------------------------------------------------------------
_STOPALL_WINDOW = 30.0
_stopall_armed = 0.0


async def close_everything(http: httpx.AsyncClient) -> str:
    """Close every open position at market. Two-step confirm."""
    global _stopall_armed
    if not enabled():
        return _NO_KEYS
    try:
        open_now = await positions(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stopall listing failed")
        return _NO_ANSWER
    if not open_now:
        _stopall_armed = 0.0
        return "Открытых позиций нет — закрывать нечего"
    names = ", ".join(sorted(open_now))
    now = time.monotonic()
    if now - _stopall_armed > _STOPALL_WINDOW:
        _stopall_armed = now
        return (
            f"⚠️ Закрою МАРКЕТОМ {len(open_now)} поз.: {names}\n"
            f"Повтори /stopall в течение {_STOPALL_WINDOW:.0f} секунд для подтверждения."
        )
    _stopall_armed = 0.0
    lines = []
    for coin in sorted(open_now):
        try:
            await hyper.market_close(coin)
            lines.append(f"✅ {coin} закрывается")
        # Deliberately broad: one refused close must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("could not close %s", coin)
            lines.append(f"❌ {coin}: {exc}")
    lines.append("Отчёты 💸 с PnL придут, как позиции закроются.")
    return "\n".join(lines)


async def close_position(http: httpx.AsyncClient, query: str) -> str:
    """Close one position by coin, for /close BTC."""
    if not enabled():
        return _NO_KEYS
    coin = query.strip().upper()
    if not coin:
        return "Какую позицию? /close BTC"
    try:
        open_now = await positions(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("close listing failed")
        return _NO_ANSWER
    if coin not in open_now:
        have = ", ".join(sorted(open_now)) or "ничего"
        return f"Позиции {coin} нет. Открыто: {have}"
    try:
        await hyper.market_close(coin)
    # Deliberately broad: the refusal text is the answer.
    except Exception as exc:  # noqa: BLE001
        log.exception("close failed for %s", coin)
        return f"❌ {coin}: не смог закрыть ({exc})"
    return f"✅ {coin} закрывается — отчёт 💸 придёт следом"


async def force_leverage_one(http: httpx.AsyncClient, query: str = "") -> str:
    """Force 1x leverage, for /lev1 [coin]: named, or every open position."""
    if not enabled():
        return _NO_KEYS
    query = query.strip().upper()
    if query:
        coins = [query]
    else:
        try:
            coins = sorted(await positions(http))
        # Deliberately broad: a chat command must answer, not crash the poller.
        except Exception:  # noqa: BLE001
            log.exception("lev1 listing failed")
            return _NO_ANSWER
        if not coins:
            return "Нет открытых позиций — плечо ставить некому"
    lines = []
    for coin in coins:
        try:
            await hyper.update_leverage(coin, 1)
            lines.append(f"✅ {coin} → 1x")
        # Deliberately broad: one refusal must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("lev1 failed for %s", coin)
            lines.append(f"❌ {coin}: {exc}")
    return "\n".join(lines)


async def stats_report(http: httpx.AsyncClient, send_photo=None, arg: str = "") -> str:
    """Closed results of the last N days (default 30), with the equity curve.

    Hyperliquid's fills window covers the most recent ~2000 fills; a longer
    history simply shows what it reaches.
    """
    if not enabled():
        return _NO_KEYS
    try:
        days = max(1, min(int(arg or 30), 365))
    except ValueError:
        days = 30
    try:
        fills = await user_fills(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stats fills failed")
        return _NO_ANSWER
    start = datetime.now().date() - timedelta(days=days - 1)
    daily = [0.0] * days
    trades = wins = 0
    for f in fills:
        if not str(f.get("dir", "")).startswith("Close"):
            continue
        day = datetime.fromtimestamp(int(f.get("time") or 0) / 1000).date()
        if day < start:
            continue
        net = float(f.get("closedPnl") or 0) - float(f.get("fee") or 0)
        daily[(day - start).days] += net
        trades += 1
        if net >= 0:
            wins += 1
    total = sum(daily)
    depo = await equity(http)
    text = (
        f"📊 за {days} дн.: закрытий {trades}, в плюс {wins}\n"
        f"Σ <b>{_usd(total)}{_share(total, depo)}</b>"
    )
    if send_photo is not None and trades:
        try:
            png = chart.equity_curve(
                daily, title=f"PnL · {days}d", depo=depo, end=datetime.now().date()
            )
            if await send_photo(text, png):
                return ""
        # Deliberately broad: the text still answers without the picture.
        except Exception:  # noqa: BLE001
            log.exception("no stats chart")
    return text


async def market_report(http: httpx.AsyncClient, send_photo=None) -> str:
    """BTC and ETH snapshot charts for /status."""
    if send_photo is None:
        return ""
    try:
        pngs = []
        closes = []
        for coin in ("BTC", "ETH"):
            _, candles = await _klines(http, coin, CHART_BARS)
            closes.append(candles[-1].close)
            pngs.append(
                chart.render(
                    coin,
                    "",
                    candles,
                    candles[-1].close,
                    timeframe=f"{CHART_INTERVAL}m",
                    plain=True,
                )
            )
        caption = f"BTC {closes[0]:,.0f} · ETH {closes[1]:,.0f} · {CHART_INTERVAL}m"
        await send_photo(caption, chart.side_by_side(pngs))
    # Deliberately broad: the market picture is garnish on /status.
    except Exception:  # noqa: BLE001
        log.exception("no market snapshot")
    return ""


async def price_before(http: httpx.AsyncClient, coin: str) -> float | None:
    """The mid a moment ago, for orienting a TV alert's arrow."""
    try:
        mids = await info(http, {"type": "allMids"})
        value = mids.get(coin.removesuffix("USDT").removesuffix("USD"))
        return float(value) if value is not None else None
    # Deliberately broad: no price just means no arrow.
    except Exception:  # noqa: BLE001
        log.exception("no mid for %s", coin)
        return None
