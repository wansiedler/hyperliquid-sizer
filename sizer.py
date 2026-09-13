"""Resize resting Hyperliquid entry orders so the stop always costs RISK_PCT.

Draw the entry on Hyperliquid's own chart (their UI is a TradingView chart
with order placement) with a stop attached; the sizer rewrites the order's
quantity so that stopping out costs exactly RISK_PCT of the account — the
maker entry fee and the stop's taker close budgeted in:

    qty = equity * RISK_PCT / (|entry - stop| + entry*MAKER_FEE + stop*TAKER_FEE)

The stop comes from the order's own trigger child when there is one, from
the account's stop trigger on the same coin otherwise, and from
FALLBACK_SL_PCT as the last resort (0 skips such orders).
"""

import asyncio
import logging
import math
import os

import httpx

import hyper
import watch

log = logging.getLogger("relay.sizer")

RISK_PCT = float(os.getenv("RISK_PCT", "1")) / 100
FALLBACK_SL_PCT = float(os.getenv("FALLBACK_SL_PCT", "0")) / 100
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "1"))
POLL = float(os.getenv("POLL_SEC", "2"))
# Passes the entry and stop must hold still before the quantity is rewritten.
SETTLE_POLLS = int(os.getenv("SETTLE_POLLS", "2"))

_settling: dict[int, tuple[str, int]] = {}


def enabled() -> bool:
    return hyper.enabled() and bool(hyper.SECRET)


def _stop_of(order: dict, orders: list[dict]) -> float | None:
    """The stop price guarding this entry, wherever it lives."""
    for child in order.get("children") or []:
        if "Stop" in str(child.get("orderType", "")):
            price = float(child.get("triggerPx") or 0)
            if price > 0:
                return price
    for other in orders:
        if (
            other.get("coin") == order.get("coin")
            and other.get("isTrigger")
            and other.get("reduceOnly")
            and "Stop" in str(other.get("orderType", ""))
        ):
            price = float(other.get("triggerPx") or 0)
            if price > 0:
                return price
    if FALLBACK_SL_PCT > 0:
        entry = float(order.get("limitPx") or 0)
        sign = 1 - FALLBACK_SL_PCT if order.get("side") == "B" else 1 + FALLBACK_SL_PCT
        return entry * sign
    return None


def target_qty(order: dict, stop: float, equity: float, step: float) -> float | None:
    """The quantity whose stop-out costs exactly RISK_PCT, fees included."""
    entry = float(order.get("limitPx") or 0)
    if entry <= 0 or step <= 0:
        return None
    distance = abs(entry - stop)
    if distance <= 0:
        return None
    per_unit = distance + entry * watch.MAKER_FEE + stop * watch.TAKER_FEE
    qty = (equity * RISK_PCT) / per_unit
    qty = min(qty, (equity * MAX_LEVERAGE) / entry)  # notional ceiling
    qty = math.floor(qty / step) * step
    return watch._fmt_size(qty, step) or None


def has_settled(order: dict) -> bool:
    """Whether the order held still for SETTLE_POLLS passes.

    Dragging a stop on the chart amends the order every mouse step; the
    sizer waits until the picture stops moving.
    """
    oid = int(order.get("oid") or 0)
    signature = f"{order.get('limitPx')}|{order.get('sz')}"
    seen_signature, count = _settling.get(oid, ("", 0))
    count = count + 1 if signature == seen_signature else 1
    _settling[oid] = (signature, count)
    return count >= SETTLE_POLLS


async def tick(http: httpx.AsyncClient, send) -> None:
    """One pass: resize every settled entry order that is the wrong size."""
    orders = await watch.open_orders(http)
    live = {int(o.get("oid") or 0) for o in orders}
    for stale in _settling.keys() - live:
        del _settling[stale]
    entries = [o for o in orders if not o.get("isTrigger") and not o.get("reduceOnly")]
    entries = [o for o in entries if has_settled(o)]
    if not entries:
        return
    equity = await watch.wallet_balance(http)
    if not equity:
        return
    for order in entries:
        await _resize_order(http, order, orders, equity, send)


async def _resize_order(
    http: httpx.AsyncClient, order: dict, orders: list[dict], equity: float, send
) -> None:
    """Fit one entry order to the target risk."""
    coin = order.get("coin", "")
    stop = _stop_of(order, orders)
    if stop is None:
        log.info("%s #%s: no stop-loss, skipping", coin, order.get("oid"))
        return
    step = await watch._lot(http, coin)
    want = target_qty(order, stop, equity, step)
    if want is None:
        return
    have = float(order.get("sz") or 0)
    if abs(want - have) < step:
        return
    entry = float(order.get("limitPx") or 0)
    try:
        await hyper.modify_order(
            int(order["oid"]), coin, order.get("side") == "B", want, str(order["limitPx"])
        )
    # Deliberately broad: one refused amend must not strand the rest.
    except Exception as exc:  # noqa: BLE001
        log.exception("resize failed for %s", coin)
        await send(f"❌ {coin}: не смог пересайзить лимитку ({exc})")
        return
    distance_pct = abs(entry - stop) / entry * 100

    def described(qty: float) -> str:
        value = qty * entry
        dollars = f"{value:,.0f}$" if value >= 10 else f"{value:,.2f}$"
        return f"{qty:g} ({dollars}, {value / equity * 100:.1f}% депо)"

    await send(
        f"⚖️ {coin} {'Buy' if order.get('side') == 'B' else 'Sell'} limit @ {order['limitPx']}\n"
        f"stop {stop:g} ({distance_pct:.2f}%) → qty {described(have)} → {described(want)}"
    )


async def poll(http: httpx.AsyncClient, send) -> None:
    """Resize orders until cancelled. Never lets one failure end the loop."""
    mode = "dry-run" if hyper.DRY_RUN else "LIVE"
    log.info("sizing orders every %ss | risk %s%% | %s", POLL, RISK_PCT * 100, mode)
    await send(f"⚖️ sizer up — {mode}, risk {RISK_PCT * 100:g}%, max leverage {MAX_LEVERAGE:g}x")
    last_error = ""
    while True:
        try:
            await tick(http, send)
            last_error = ""
        except asyncio.CancelledError:
            raise
        # Deliberately broad: the loop outlives refusals and hiccups; only a
        # NEW error is worth a line in the chat.
        except Exception as exc:  # noqa: BLE001
            log.exception("sizer tick failed")
            if str(exc) != last_error:
                last_error = str(exc)
                await send(f"⚖️❌ sizer: {exc}")
        await asyncio.sleep(POLL)
