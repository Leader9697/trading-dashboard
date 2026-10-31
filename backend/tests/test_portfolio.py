import random
from datetime import datetime, timezone

from app import portfolio as P

DAY = 86400
F = {"step": 0.001, "min_notional": 5.0, "volume_m": 100}


def _sig(t, r, entry=100.0, sl_pct=3.0, hold=2 * DAY, score=50):
    return {"t": t, "side": "l", "score": score, "end": t + hold, "entry": entry, "sl_pct": sl_pct, "r": r, "status": "tp"}


def _always(s, t):
    return True


def test_size_respects_lot_step_min_order_and_leverage():
    qty, margin = P.size(_sig(0, 1, entry=100, sl_pct=3), 60, 1, F)
    assert abs(qty - 0.2) < 1e-9  # $0.60 risk / $3 distance
    assert abs(margin - 20 / 16) < 1e-9  # $20 notional at 16x (under 1 / (2 x 3%))
    assert P.size(_sig(0, 1, entry=60000, sl_pct=3), 60, 1, {**F, "min_notional": 100}) is None  # BTC at $60
    assert P.size(_sig(0, 1, entry=3000, sl_pct=3), 60, 1, {**F, "step": 0.01}) is None  # lot step too coarse


def test_replay_limits_positions_and_books_profit_at_exit():
    coins = {
        "AAAUSDT": {"signals": [_sig(10, 4.0), _sig(20, -1.0)]},  # second arrives while the first is open
        "BBBUSDT": {"signals": [_sig(11, -1.0)]},
        "CCCUSDT": {"signals": [_sig(12, 4.0)]},  # no slot left with max_pos 2
        "DDDUSDT": {"signals": [{"t": 13 + 3 * DAY, "side": "s", "score": 50, "end": 13 + 4 * DAY}]},  # unfilled
    }
    filters = {s: F for s in coins}
    out = P.replay(coins, filters, _always, 0, 10 * DAY, risk_pct=1, max_pos=2)
    assert out["trades"] == 2
    assert out["skipped"]["busy"] == 1 and out["skipped"]["slots"] == 1 and out["skipped"]["unfilled"] == 1
    assert abs(out["final"] - (60 + 0.6 * 4 - 0.6)) < 0.01
    assert out["curve"][0] == (0, 60.0) and len(out["curve"]) == 3


def test_replay_logs_why_each_signal_was_taken_or_skipped():
    tiny = {"t": 30, "side": "s", "score": 50, "end": 30 + DAY, "entry": 100.0, "sl_pct": 60.0}  # unfilled, too small
    coins = {
        "AAAUSDT": {"signals": [_sig(10, 4.0), _sig(20, -1.0)]},
        "BBBUSDT": {"signals": [_sig(11, -1.0)]},
        "CCCUSDT": {"signals": [_sig(12, 4.0)]},
        "DDDUSDT": {"signals": [tiny]},
        "EEEUSDT": {"signals": [_sig(13, 4.0)]},
    }
    filters = {s: F for s in coins}
    out = P.replay(coins, filters, lambda s, t: s != "EEEUSDT", 0, 10 * DAY, risk_pct=1, max_pos=2)
    assert [(row["s"], row["d"]) for row in out["log"]] == [
        ("AAAUSDT", "take"), ("BBBUSDT", "take"), ("CCCUSDT", "slots"), ("EEEUSDT", "coin"),
        ("AAAUSDT", "busy"), ("DDDUSDT", "slots"),
    ]
    assert out["log"][0]["pnl"] == 2.4 and out["log"][2]["open"] == 2
    # sized before the order goes out: an unfilled limit below the minimum never held a slot
    free = P.replay({"DDDUSDT": coins["DDDUSDT"]}, filters, _always, 0, 10 * DAY, risk_pct=1, max_pos=2)
    assert free["log"][0]["d"] == "min_order" and free["skipped"]["unfilled"] == 0


def test_coin_stats_only_use_closed_trades():
    coins = {"AAAUSDT": {"signals": [_sig(0, 1.0), _sig(DAY, 2.0), _sig(5 * DAY, -1.0)]}}
    st = P.coin_stats(coins, 4 * DAY)["AAAUSDT"]
    assert st["n"] == 1 and st["exp"] == 1.0  # the overlapping signal and the open trade are left out


def _raw(seed=1):
    rng = random.Random(seed)
    t0 = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
    t1 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    coins, filters = {}, {}
    for k, edge in enumerate((0.3, 0.2, 0.1, -0.2, 0.0)):
        sym = f"C{k}USDT"
        sigs, t = [], t0
        while t < t1:
            t += rng.randint(3, 12) * DAY
            r = 4 - 0.05 if rng.random() < 0.2 + edge / 5 else -1.05
            sigs.append(_sig(t, r, entry=10 + k, sl_pct=rng.uniform(2, 6), hold=rng.randint(1, 5) * DAY))
        coins[sym] = {"first": t0, "last": t1, "signals": sigs}
        filters[sym] = F
    filters["C4USDT"] = {**F, "min_notional": 1000}  # never fits $60
    return {"coins": coins, "filters": filters}


def test_build_picks_settings_in_sample_and_lists_coins():
    out = P.build(_raw())
    st = out["settings"]
    assert st["risk_pct"] in P.GRID["risk_pct"] and st["max_pos"] in P.GRID["max_pos"]
    assert {m["name"] for m in out["methods"]} == {"all", "past_positive", "top8"}
    assert "C4USDT" not in out["recommended"]
    row = next(c for c in out["coins"] if c["symbol"] == "C4USDT")
    assert row["trade"] is False and row["reason"] == "min_order"
    assert out["result"]["curve"][0][1] == P.EQUITY
    assert set(out["result"]["years"]) == {"2023", "2024", "2025", "2026"}
    assert len(out["grid"]) == len(P.GRID["risk_pct"]) * len(P.GRID["max_pos"])


def test_year_picks_only_know_earlier_trades():
    raw = _raw()
    cut = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    later = {s: {**c, "signals": [g if g["t"] < cut else {**g, "r": -1.05} for g in c["signals"]]} for s, c in raw["coins"].items()}
    a = P.select(raw["coins"], raw["filters"], cut, 1.0)
    b = P.select(later, raw["filters"], cut, 1.0)
    assert a == b  # changing what happens after the cut can't change the pick at the cut


def test_portfolio_endpoint(client, monkeypatch, tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"generated": 1, "equity": 60, "settings": {"risk_pct": 1, "max_pos": 3}, "recommended": ["ETHUSDT"]}')
    monkeypatch.setattr(P, "PATH", path)
    body = client.get("/api/portfolio").json()
    assert body["available"] and body["recommended"] == ["ETHUSDT"]
    live = body["live"]
    assert live[0]["symbol"] == "ETHUSDT" and "active" in live[0]
    assert "notional" in live[0] or live[0].get("error")
