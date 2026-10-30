"""Telegram alerts for v3 signals, sent right after each 4h candle closes.

A scheduled GitHub Actions job calls /api/notify a few minutes after every close.
The endpoint is public, so it only sends inside the window after a close and at
most once per coin per candle (per warm instance).
"""

from __future__ import annotations

import math
import time

import httpx

from .config import Settings
from .market import INTERVAL_SECONDS
from .setup import MODES

API = "https://api.telegram.org/bot{token}/{method}"
WINDOW = 90 * 60  # GitHub's scheduler can run late
DASHBOARD = "https://trading-dashboard-two-orcin.vercel.app"
_sent: set[tuple[str, int]] = set()
_last_test = [0.0]


def _call(settings: Settings, method: str, **params) -> dict:
    resp = httpx.post(API.format(token=settings.telegram_token, method=method), json=params, timeout=15)
    return resp.json()


def chat_id(settings: Settings) -> str | None:
    if settings.telegram_chat_id:
        return settings.telegram_chat_id
    updates = _call(settings, "getUpdates").get("result") or []
    for u in reversed(updates):
        msg = u.get("message") or u.get("channel_post")
        if msg:
            return str(msg["chat"]["id"])
    return None


def sizing(sig: dict, equity: float, risk_pct: float) -> dict | None:
    """Same maths as the dashboard calculator."""
    if not sig.get("sl_pct"):
        return None
    risk = equity * risk_pct / 100
    notional = risk / (sig["sl_pct"] / 100)
    max_lev = max(1, math.floor(100 / (2 * sig["sl_pct"])))
    lev = min(max_lev, max(1, math.ceil(notional / (0.3 * equity))))
    return {"risk": risk, "notional": notional, "lev": lev, "too_small": notional < sig["min_notional"]}


def message(symbol: str, sig: dict, settings: Settings) -> str:
    coin = symbol.removesuffix("USDT")
    side = "LONG" if sig["side"] == "long" else "SHORT"
    lines = [
        f"{'🟢' if side == 'LONG' else '🔴'} {side} {coin} sekarang (order market)",
        f"Entry ± {sig['entry']}",
        f"Stop loss {sig['stop_loss']} ({sig['sl_pct']}%)",
        f"Take profit {sig['take_profit']} (RR 1:{sig['rr']}, tutup semua)",
    ]
    z = sizing(sig, settings.alert_equity, settings.alert_risk_pct)
    if z:
        lines.append(f"Modal ${settings.alert_equity:g}, risiko {settings.alert_risk_pct:g}%: posisi ${z['notional']:.1f}, "
                     f"leverage {z['lev']}x isolated, rugi ${z['risk']:.2f} kalau kena SL")
        if z["too_small"]:
            lines.append(f"Posisi di bawah minimal order Binance ({sig['min_notional']} USDT): lewati.")
    lines.append("Kalau harga sudah jauh dari entry, lewati.")
    lines.append(DASHBOARD)
    return "\n".join(lines)


def run(settings: Settings, setup_for, now: float | None = None, test: bool = False) -> dict:
    """`setup_for(symbol)` returns the dashboard setup (with its signal) for one coin."""
    if not settings.telegram_token:
        return {"ok": False, "reason": "TELEGRAM_BOT_TOKEN belum diisi"}
    chat = chat_id(settings)
    if chat is None:
        return {"ok": False, "reason": "Kirim pesan apa saja ke bot dulu supaya chat id ketemu"}
    now = time.time() if now is None else now
    if test:
        if now - _last_test[0] < 60:
            return {"ok": False, "reason": "Tunggu 1 menit"}
        _last_test[0] = now
        _call(settings, "sendMessage", chat_id=chat, text="Bot sinyal tersambung. Notif dikirim tiap ada sinyal 6/6 setelah candle 4 jam tutup.")
        return {"ok": True, "test": True}
    step = INTERVAL_SECONDS[MODES["swing"]["ltf"]]
    closed = int(now // step * step)
    if now - closed > WINDOW:
        return {"ok": True, "sent": [], "reason": "di luar jendela setelah candle tutup"}
    sent = []
    for symbol in settings.watchlist:
        if (symbol, closed) in _sent:
            continue
        try:
            sig = setup_for(symbol)["signal"]
        except Exception:  # one coin failing must not block the others
            continue
        if sig["active"]:
            _call(settings, "sendMessage", chat_id=chat, text=message(symbol, sig, settings), disable_web_page_preview=True)
            _sent.add((symbol, closed))
            sent.append(symbol)
    return {"ok": True, "sent": sent}
