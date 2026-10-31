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
    assert sig["auto"] is False and sig["rules"] == "v3" and len(sig["checks"]) == 7
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
