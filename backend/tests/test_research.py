from app import research
from app.market import demo_klines


def test_run_ranks_on_in_sample_and_reports_oos(monkeypatch):
    monkeypatch.setattr(research, "GRID", {**{k: v[:1] for k, v in research.GRID.items()},
                                           "entry": ["limit", "confirm", "market"]})
    data = {s: (demo_klines(s, "1d", 600), demo_klines(s, "4h", 1500)) for s in ("BTCUSDT", "ETHUSDT")}
    out = research.run(data, "swing")
    assert out["variants_tested"] == 3
    assert out["baseline"]["v"]["entry"] == "limit"
    for row in out["top10_is"]:
        assert row["is"]["n"] >= 60


def test_regime_needs_rising_ema():
    flat = [{"time": i, "close": 100.0} for i in range(80)]
    up = [{"time": i, "close": 100.0 + i} for i in range(80)]
    assert set(research.btc_regime(flat)[1][60:]) == {0}
    assert research.btc_regime(up)[1][-1] == 1


def test_manage_stop_first():
    bars = [{"open": 100, "high": 112, "low": 94, "close": 100}]
    r, _, status = research._manage(True, 100.0, 95.0, bars, 0, 5)
    assert status == "stop" and r == -1.0


def test_setup_has_signal_v2(client):
    sig = client.get("/api/setup/ETHUSDT?mode=swing").json()["signal"]
    assert sig["auto"] is False and sig["rules"] == "v3" and len(sig["checks"]) == 8
    if sig["side"] == "long":
        assert sig["stop_loss"] < sig["entry"] < sig["take_profit"]
    else:
        assert sig["stop_loss"] > sig["entry"] > sig["take_profit"]
    risk = abs(sig["entry"] - sig["stop_loss"])
    assert abs(abs(sig["take_profit"] - sig["entry"]) - sig["rr"] * risk) <= 0.01 * risk + 1e-6
    assert sig["sl_pct"] > 0 and sig["min_notional"] == 20
    coin = client.get("/api/snapshot").json()["coins"][0]
    assert len(coin["sig"]) == 6


def test_backtest_rules_v2_and_folds(client):
    v2 = client.get("/api/backtest/SOLUSDT?mode=swing&rules=v2").json()
    v1 = client.get("/api/backtest/SOLUSDT?mode=swing").json()
    assert v2["rules"] == "v2" and len(v2["folds"]) == 4
    assert sum(f["n"] for f in v1["folds"]) == v1["exit_split"]["trades"]
    assert client.get("/api/backtest/SOLUSDT?rules=v9").status_code == 422


def test_btc_chop_gate():
    from app.setup import CHOP_MAX, btc_chop, chop_at, chop_ok

    day = 86400
    # a straight-line trend is far from choppy, a flat zig-zag is very choppy
    trend = [{"time": d * day, "open": 100 + d, "high": 101.5 + d, "low": 99.5 + d, "close": 101 + d} for d in range(30)]
    zig = [{"time": d * day, "open": 100, "high": 102, "low": 98, "close": 101 if d % 2 else 99} for d in range(30)]
    assert btc_chop(trend)[29 * day] < 40 and btc_chop(zig)[29 * day] > CHOP_MAX
    # a 4h bar closing on day 30 sees day 29, the last one complete
    assert chop_at(btc_chop(zig), 30 * day + 4 * 3600) == btc_chop(zig)[29 * day]
    assert chop_ok(None) and chop_ok(50.0) and not chop_ok(CHOP_MAX)
