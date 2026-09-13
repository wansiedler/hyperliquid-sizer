"""Test-wide isolation from the developer's own `bipboop`.

Modules read their configuration at import time; whatever the developer
runs in real life must not decide what the suite asserts against, and a
machine with a speaker configured must not bind the real TTS port.
"""

import pytest

import commands
import hyper
import sheets
import sizer
import speaker
import tv_alerts
import watch


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(speaker, "CAST_HOST", "")
    monkeypatch.setattr(speaker, "TTS_HOST", "")
    monkeypatch.setattr(hyper, "ACCOUNT", "")
    monkeypatch.setattr(hyper, "SECRET", "")
    monkeypatch.setattr(hyper, "DRY_RUN", False)
    hyper.set_exchange(None)
    monkeypatch.setattr(watch, "RISK_TARGET", 0.005)
    monkeypatch.setattr(watch, "MAKER_FEE", 0.00015)
    monkeypatch.setattr(watch, "TAKER_FEE", 0.00045)
    monkeypatch.setattr(watch, "MIN_RR", 2.0)
    # The guard would market-close most fixture positions (no stop); tests
    # that exercise it flip it back on themselves.
    monkeypatch.setattr(watch, "RISK_GUARD", False)
    monkeypatch.setattr(watch, "RISK_TRIM", False)
    monkeypatch.setattr(watch, "GUARD_MAX_LEVERAGE", 1.0)
    monkeypatch.setattr(watch, "GUARD_GRACE", 45.0)
    watch._guard_seen.clear()
    watch._guard_closed.clear()
    watch._trim_cooldown.clear()
    watch._births.clear()
    watch._sz_decimals.clear()
    monkeypatch.setattr(sizer, "RISK_PCT", 0.005)
    monkeypatch.setattr(sizer, "FALLBACK_SL_PCT", 0.0)
    monkeypatch.setattr(sizer, "MAX_LEVERAGE", 5.0)
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 1)
    sizer._settling.clear()
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "")
    monkeypatch.setattr(tv_alerts, "JOURNAL_URL", "")
    monkeypatch.setattr(tv_alerts, "TV_PUBLIC_URL", "")
    monkeypatch.setattr(sheets, "SHEETS_URL", "")
    monkeypatch.setattr(sheets, "SHEETS_SECRET", "")
    monkeypatch.setattr(commands, "WATCH_USERS", [], raising=False)
    monkeypatch.setattr(commands, "POLL_TIMEOUT", 25)
