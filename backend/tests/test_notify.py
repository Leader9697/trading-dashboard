from dataclasses import replace

from app import notify
from app.config import get_settings

SIG = {"active": True, "side": "long", "entry": 100.0, "stop_loss": 95.0, "take_profit": 112.5, "rr": 2.5,
       "sl_pct": 5.0, "min_notional": 5}


def _settings(**kw):
    return replace(get_settings(), telegram_token="t", telegram_chat_id="42", watchlist=("SOLUSDT", "BTCUSDT"), **kw)


def test_sends_once_per_coin_per_candle(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_call", lambda s, method, **p: calls.append((method, p)) or {})
    notify._sent.clear()
    setups = {"SOLUSDT": {"signal": SIG}, "BTCUSDT": {"signal": {**SIG, "active": False}}}
    close = 1793376000  # a 4h boundary
    first = notify.run(_settings(), setups.__getitem__, now=close + 300)
    again = notify.run(_settings(), setups.__getitem__, now=close + 600)
    late = notify.run(_settings(), setups.__getitem__, now=close + 3 * 3600)
    assert first["sent"] == ["SOLUSDT"] and again["sent"] == [] and late["sent"] == []
    assert len(calls) == 1 and calls[0][1]["chat_id"] == "42"
    text = calls[0][1]["text"]
    assert "LONG SOL" in text and "112.5" in text and "posisi $11.0" in text and "1x isolated" in text


def test_sizing_flags_below_minimum_order():
    z = notify.sizing({**SIG, "min_notional": 100}, 55, 1)
    assert z["too_small"] and round(z["notional"], 1) == 11.0


def test_without_token_does_nothing(client):
    assert client.get("/api/notify").json()["ok"] is False


def test_reports_telegram_rejection(monkeypatch):
    monkeypatch.setattr(notify, "_call", lambda s, m, **p: {"ok": False, "error_code": 401, "description": "Unauthorized"})
    out = notify.run(replace(_settings(), telegram_chat_id=""), lambda s: {})
    assert out["ok"] is False and "401 Unauthorized" in out["reason"]
