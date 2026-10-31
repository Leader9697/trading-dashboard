from dataclasses import replace

from app import backtest_v3 as V, main
from app.config import get_settings
from app.market import demo_klines, get_klines
from app.setup import MAX_HOLD_BARS, TP_R, build_setup, build_signal


def _data():
    s = replace(get_settings(), data_mode="demo")
    return {sym: (get_klines(s, sym, "1d", 700), get_klines(s, sym, "4h", 900)) for sym in ("BTCUSDT", "ETHUSDT")}


def test_trades_use_the_live_exits(monkeypatch):
    monkeypatch.setattr(V.D, "make_allow", lambda feat, g: (lambda i, long: True))
    data = _data()
    out = V.run(data, {s: [] for s in data})
    trades = [t for ts in out["coins"].values() for t in ts]
    assert trades and out["rules"]["tp_r"] == TP_R and out["rules"]["max_hold_bars"] == MAX_HOLD_BARS
    for t in trades:
        sign = 1 if t["side"] == "long" else -1
        risk = (t["entry"] - t["stop_loss"]) * sign
        assert risk > 0
        assert abs((t["take_profit"] - t["entry"]) * sign - TP_R * risk) < 1e-3 * t["entry"]
        assert t["signal"] <= t["fill"] < t["end"]
        assert t["end"] - t["fill"] <= (MAX_HOLD_BARS + 2) * 14400
        assert t["status"] in {"tp", "stop", "timeout"}
    # one position per coin: a trade starts only after the previous one ended
    for ts in out["coins"].values():
        assert all(a["end"] <= b["fill"] for a, b in zip(ts, ts[1:]))


def test_trades_endpoint(client, monkeypatch, tmp_path):
    path = tmp_path / "v3.json"
    path.write_text('{"generated": 1, "rules": {}, "coins": {"BTCUSDT": [{"signal": 1, "fill": 2, "end": 3, '
                    '"side": "long", "entry": 10, "stop_loss": 9, "take_profit": 13, "status": "tp", "r": 2.9, '
                    '"sl_pct": 10, "below_min": true}]}}')
    monkeypatch.setattr(V, "PATH", path)
    body = client.get("/api/backtest/btcusdt/trades").json()
    assert body["available"] and body["stats"]["trades"] == 1 and body["below_min"] == 1
    assert client.get("/api/backtest/DOGEUSDT/trades").json()["available"] is False


def test_signal_ignores_funding_and_fear_greed():
    h, l = demo_klines("BTCUSDT", "1d", 300), demo_klines("BTCUSDT", "4h", 300)
    calm = build_setup("BTCUSDT", "swing", h, l, {}, None)
    crowded = build_setup("BTCUSDT", "swing", h, l, {"funding_pct": 0.2, "long_short_ratio": 3}, {"value": 90})
    assert crowded["score"] != calm["score"]
    a, b = build_signal(calm, l, 1), build_signal(crowded, l, 1)
    assert (a["side"], a["entry"], a["checks"][0]["ok"]) == (b["side"], b["entry"], b["checks"][0]["ok"])


def test_setup_skips_the_forming_candle(client, monkeypatch):
    last = demo_klines("ETHUSDT", "4h", 301)
    monkeypatch.setattr(main.time, "time", lambda: last[-1]["time"] + 600)  # 10 minutes into a new candle
    s = client.get("/api/setup/ETHUSDT").json()
    assert s["as_of"] == last[-2]["time"] and s["last_price"] == last[-1]["close"]
    assert s["signal"]["max_hold_bars"] == MAX_HOLD_BARS
