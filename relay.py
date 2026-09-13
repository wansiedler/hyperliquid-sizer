"""The Hyperliquid sizer's main loop: watchers, chat commands, TV alerts.

No user Telegram account here — only the bot API. TradingView cannot route
orders to Hyperliquid (its integration is charts and data only), so entries
are drawn on Hyperliquid's own chart; TradingView ALERTS still arrive over
the webhook and land in the chat like everything else.

    python relay.py --check    verify config, send a test line, exit
    python relay.py            run
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import httpx
from dotenv import load_dotenv

import commands
import hyper
import sheets
import sizer
import speaker
import tv_alerts
import watch

load_dotenv("bipboop")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
RELAY_NAME = os.getenv("RELAY_NAME", "hyperliquid-sizer")
NOTIFY_LIFECYCLE = os.getenv("NOTIFY_LIFECYCLE", "1").lower() not in ("0", "false", "no", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")
# httpx logs the full request URL at INFO, which would print BOT_TOKEN.
logging.getLogger("httpx").setLevel(logging.WARNING)


def require_config() -> None:
    """Fail fast on missing config."""
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("TARGET_CHAT_ID", TARGET_CHAT_ID),
            ("HL_ACCOUNT", hyper.ACCOUNT),
        )
        if not value
    ]
    if missing:
        sys.exit(f"Missing in bipboop: {', '.join(missing)}")


def _accepted(response, what: str) -> bool:
    """Whether Telegram actually took the message: refusals ride in the body."""
    if response.status_code != 200:
        log.error("%s %s: %s", what, response.status_code, response.text)
        return False
    try:
        payload = response.json()
    except ValueError:
        log.error("%s returned no JSON: %s", what, response.text[:200])
        return False
    if not payload.get("ok"):
        log.error("%s refused: %s", what, payload)
        return False
    return True


async def send_via_bot(
    http: httpx.AsyncClient, text: str, html: bool = False, *, quiet: bool = False
) -> bool:
    """Post one line through the bot. Returns True when Telegram accepted it."""
    payload: dict[str, object] = {"chat_id": TARGET_CHAT_ID, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15
        )
    except httpx.HTTPError:
        log.exception("sendMessage failed")
        return False
    if not _accepted(response, "sendMessage"):
        return False
    if not quiet:
        log.info("sent: %s", text)
    return True


async def send_photo_via_bot(http: httpx.AsyncClient, caption: str, png: bytes) -> bool:
    """Post one picture with a caption."""
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": TARGET_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=60,
        )
    except httpx.HTTPError:
        log.exception("sendPhoto failed")
        return False
    if not _accepted(response, "sendPhoto"):
        return False
    log.info("sent photo: %s", caption)
    return True


async def send_album_via_bot(http: httpx.AsyncClient, caption: str, pngs: list[bytes]) -> bool:
    """Post several pictures as one media group; the caption rides first."""
    import json as _json

    media = []
    files = {}
    for i, png in enumerate(pngs[:10]):
        name = f"p{i}"
        item: dict[str, str] = {"type": "photo", "media": f"attach://{name}"}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        files[name] = (f"{name}.png", png, "image/png")
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
            data={"chat_id": TARGET_CHAT_ID, "media": _json.dumps(media)},
            files=files,
            timeout=60,
        )
    except httpx.HTTPError:
        log.exception("sendMediaGroup failed")
        return False
    if not _accepted(response, "sendMediaGroup"):
        return False
    log.info("sent album of %s: %s", len(files), caption.splitlines()[0])
    return True


def human(seconds: float) -> str:
    """Uptime as something readable in a phone notification."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


async def notify(http: httpx.AsyncClient, text: str) -> None:
    """Lifecycle ping. A missed notice must never take the relay down."""
    if not NOTIFY_LIFECYCLE:
        return
    try:
        # Quiet: the up notice carries links, and the log needs none of them.
        await send_via_bot(http, text, quiet=True)
    # Deliberately broad: the shutdown path must not raise on its way out.
    except Exception:  # noqa: BLE001
        log.exception("lifecycle notice failed")


class _Shutdown:
    """SIGINT/SIGTERM turn into an event the main loop can await."""

    def __init__(self):
        self.event = asyncio.Event()
        self.reason = "stopped"

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._fire, sig.name)

    def _fire(self, name: str) -> None:
        self.reason = name
        self.event.set()


def _links_pair() -> tuple[str, str]:
    """(full links for /links, masked links for the up notice)."""
    links = shown = ""
    if tv_alerts.JOURNAL_URL:
        links = shown = f"\n📒 {tv_alerts.JOURNAL_URL}"
    if tv_alerts.enabled() and tv_alerts.TV_PUBLIC_URL:
        links += f"\n📡 {tv_alerts.TV_PUBLIC_URL}/tv/{tv_alerts.TV_WEBHOOK_SECRET}"
        shown += f"\n📡 {tv_alerts.TV_PUBLIC_URL}/tv/… (/links)"
    return links, shown


def _command_handlers(http: httpx.AsyncClient, links: str) -> commands.Handlers:
    """The Hyperliquid-side callables the chat commands reach."""
    return commands.Handlers(
        positions=lambda: watch.positions_report(
            http, lambda caption, pngs: send_album_via_bot(http, caption, pngs)
        ),
        stop_all=lambda: watch.close_everything(http),
        close_one=lambda arg: watch.close_position(http, arg),
        market=lambda: watch.market_report(
            http, lambda caption, png: send_photo_via_bot(http, caption, png)
        ),
        statistics=lambda arg: watch.stats_report(
            http, lambda caption, png: send_photo_via_bot(http, caption, png), arg
        ),
        links=links.strip(),
        lev_one=lambda arg: watch.force_leverage_one(http, arg),
    )


def _spawn_watchers(http: httpx.AsyncClient) -> set[asyncio.Task]:
    """Start every configured background watcher except the TV webhook."""
    background: set[asyncio.Task] = set()
    if watch.enabled():
        background.add(
            asyncio.create_task(
                watch.poll(
                    http,
                    # The watcher composes its own markup: HTML is safe.
                    lambda text: send_via_bot(http, text, True),
                    speaker.trade,
                    lambda caption, png: send_photo_via_bot(http, caption, png),
                )
            )
        )
        background.add(
            asyncio.create_task(watch.money_poll(http, lambda text: send_via_bot(http, text)))
        )
    if sizer.enabled():
        background.add(asyncio.create_task(sizer.poll(http, lambda text: send_via_bot(http, text))))
    if sheets.enabled():
        background.add(asyncio.create_task(sheets.weekly(http)))
    return background


async def run() -> None:
    require_config()
    shutdown = _Shutdown()
    audio = None
    webhook = None
    uptime = "0s"
    try:
        async with httpx.AsyncClient() as http:
            shutdown.install()
            stats = commands.Stats()
            audio = speaker.serve_forever() if speaker.enabled() else None
            speaking = f" · 🔊 {speaker.hours_text()}" if audio is not None else ""
            started = time.time()
            links, shown = _links_pair()
            mode = "LIVE" if hyper.armed() else "dry-run"
            await notify(http, f"🟢 {RELAY_NAME} up — {mode}{speaking}{shown}")
            if audio is not None:
                await speaker.lifecycle("Relay up")

            answering = asyncio.create_task(
                commands.poll(
                    http,
                    stats,
                    lambda text, html=False: send_via_bot(http, text, html),
                    speaker.announce,
                    speaker.enabled(),
                    _command_handlers(http, links),
                )
            )
            background = {answering} | _spawn_watchers(http)
            if tv_alerts.enabled():
                alerts: asyncio.Queue = asyncio.Queue()
                webhook = tv_alerts.serve(asyncio.get_running_loop(), alerts)
                background.add(
                    asyncio.create_task(
                        tv_alerts.pump(
                            alerts,
                            lambda text: send_via_bot(http, text),
                            speaker.trade,
                            lambda coin: watch.price_before(http, coin),
                        )
                    )
                )
            await shutdown.event.wait()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            uptime = human(time.time() - started)
            await notify(http, f"🔴 {RELAY_NAME} down — {shutdown.reason}, uptime {uptime}")
            if audio is not None:
                await speaker.lifecycle("Relay down")
    finally:
        if audio is not None:
            audio.shutdown()
        if webhook is not None:
            webhook.shutdown()
    log.info("stopped cleanly after %s", uptime)


async def check() -> None:
    """--check: config, one test line through the bot, exit."""
    require_config()
    async with httpx.AsyncClient() as http:
        ok = await send_via_bot(http, f"✅ {RELAY_NAME} check — bot delivery works")
        depo = await watch.equity(http)
        print(f"telegram: {'ok' if ok else 'FAILED'}")
        print(f"hyperliquid equity: {depo if depo is not None else 'no answer'}")


def main() -> None:
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument(
        "--check", action="store_true", help="verify config and send one test line, then exit"
    )
    args = argparser.parse_args()
    asyncio.run(check() if args.check else run())


if __name__ == "__main__":
    main()
