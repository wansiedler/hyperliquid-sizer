"""The Hyperliquid API layer: signed actions and public info reads.

Reads go straight to the /info endpoint over httpx — they need no auth,
only the account address. Actions (orders, leverage) go through the
official SDK's Exchange, signed by an agent wallet: a key generated in
the exchange UI (Settings > API) that can trade but never withdraw.

    HL_ACCOUNT=0x...   # the main wallet address the bot watches
    HL_SECRET=0x...    # the agent wallet's private key; empty = read-only
    HL_TESTNET=0       # 1 flips both the info and exchange endpoints
    DRY_RUN=1          # log actions instead of sending them

The SDK is synchronous; every action is pushed onto a worker thread so
the watchers keep polling while an order is in flight.
"""

import asyncio
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.hyper")

load_dotenv("bipboop")

TESTNET = os.getenv("HL_TESTNET", "0") == "1"
API_URL = "https://api.hyperliquid-testnet.xyz" if TESTNET else "https://api.hyperliquid.xyz"
ACCOUNT = os.getenv("HL_ACCOUNT", "")
SECRET = os.getenv("HL_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"


def enabled() -> bool:
    """Watching needs only the address; trading needs the agent key too."""
    return bool(ACCOUNT)


def armed() -> bool:
    return bool(ACCOUNT and SECRET and not DRY_RUN)


async def info(http: httpx.AsyncClient, payload: dict) -> Any:
    """One /info read. Raises on transport errors and non-200 answers."""
    response = await http.post(f"{API_URL}/info", json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


_exchange = None


def exchange():
    """The SDK Exchange, built once from the agent key.

    Imported lazily: the SDK (and its eth_account machinery) stays out of
    the way for read-only runs and for the test suite, which injects a fake
    through set_exchange().
    """
    global _exchange
    if _exchange is None:
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        _exchange = Exchange(Account.from_key(SECRET), base_url=API_URL, account_address=ACCOUNT)
    return _exchange


def set_exchange(fake) -> None:
    """Test seam: hand the module a stand-in Exchange."""
    global _exchange
    _exchange = fake


def _ok(result: dict, what: str) -> dict:
    """Raise on an exchange refusal; Hyperliquid answers 200 with an error
    body, and treating that as success would lose orders silently."""
    if result.get("status") != "ok":
        raise RuntimeError(f"{what}: {result}")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if isinstance(status, dict) and "error" in status:
            raise RuntimeError(f"{what}: {status['error']}")
    return result


async def _act(what: str, call, *args, **kwargs) -> dict | None:
    """One signed action on the worker thread, honouring DRY_RUN."""
    if DRY_RUN:
        log.info("[dry-run] %s %s %s", what, args, kwargs)
        return None
    result = await asyncio.to_thread(call, *args, **kwargs)
    return _ok(result, what)


async def place_limit(
    coin: str, is_buy: bool, size: float, price: str, reduce_only: bool = False
) -> dict | None:
    return await _act(
        f"limit {coin}",
        exchange().order,
        coin,
        is_buy,
        size,
        float(price),
        {"limit": {"tif": "Gtc"}},
        reduce_only,
    )


async def market_close(coin: str, size: float | None = None) -> dict | None:
    """Close a position (or part of it) with an aggressive market order."""
    return await _act(f"close {coin}", exchange().market_close, coin, size)


async def modify_order(
    oid: int, coin: str, is_buy: bool, size: float, price: str, reduce_only: bool = False
) -> dict | None:
    return await _act(
        f"modify {coin} #{oid}",
        exchange().modify_order,
        oid,
        coin,
        is_buy,
        size,
        float(price),
        {"limit": {"tif": "Gtc"}},
        reduce_only,
    )


async def cancel_order(coin: str, oid: int) -> dict | None:
    return await _act(f"cancel {coin} #{oid}", exchange().cancel, coin, oid)


async def update_leverage(coin: str, leverage: int) -> dict | None:
    return await _act(f"lev {coin}", exchange().update_leverage, leverage, coin, True)
