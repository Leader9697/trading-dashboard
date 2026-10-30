from app import backtest, market
from app.backtest import _run_order, stats

RULES = {"order_bars": 3, "max_hold": 5}


def bar(t, high, low, close=None):
    return {"time": t, "open": low, "high": high, "low": low, "close": close if close is not None else (high + low) / 2}


def long_setup():
    # entry 100, SL 95 (1R = 5), TP1 110 (2R), TP2 115 (3R)
    return {"direction": "long", "score": 40, "checklist": [{"ok": True}] * 4,
            "levels": {"entry": 100.0, "stop_loss": 95.0, "tp1": 110.0, "tp2": 115.0}}


def run(bars):
    return _run_order(long_setup(), [bar(0, 105, 102)] + bars, 0, RULES)


def test_missed_when_target_runs_first():
    t = run([bar(1, 111, 103)])
    assert t["status"] == "missed" and t["r_split"] == 0.0


def test_missed_when_never_filled():
    t = run([bar(i, 106, 101) for i in range(1, 5)])
    assert t["status"] == "missed" and t["exit_index"] == 3


def test_stop_counts_minus_one_r():
    t = run([bar(1, 103, 99), bar(2, 101, 94)])
    assert t["status"] == "stop"
    assert -1.03 < t["r_tp1"] < -1.0 and t["r_tp1"] == t["r_split"]


def test_same_bar_stop_and_target_assumes_stop():
    t = run([bar(1, 103, 99), bar(2, 112, 94)])
    assert t["status"] == "stop"


def test_tp1_then_breakeven():
    t = run([bar(1, 103, 99), bar(2, 111, 104), bar(3, 105, 99.5)])
    assert t["status"] == "breakeven"
    assert round(t["r_tp1"], 1) == 2.0 and round(t["r_split"], 1) == 1.0


def test_tp2_gives_two_and_half_r():
    t = run([bar(1, 103, 99), bar(2, 111, 104), bar(3, 116, 108)])
    assert t["status"] == "tp2" and round(t["r_split"], 1) == 2.5


def test_timeout_marks_to_market():
    t = run([bar(1, 103, 99, 101)] + [bar(i, 104, 99, 102.5) for i in range(2, 8)])
    assert t["status"] == "timeout" and round(t["r_tp1"], 1) == 0.5


def test_stats():
    trades = [{"status": "stop", "r": -1.0}, {"status": "tp2", "r": 2.5}, {"status": "missed", "r": 0.0}]
    s = stats(trades, "r")
    assert s["trades"] == 2 and s["winrate"] == 50.0 and s["profit_factor"] == 2.5 and s["max_drawdown_r"] == 1.0


def test_htf_slice_ignores_unclosed_candles():
    htf = [{"time": t} for t in (0, 100, 200, 300)]
    got = backtest._htf_slice(htf, [0, 100, 200, 300], 100, 250)
    assert [c["time"] for c in got] == [0, 100]


def test_backtest_endpoint(client):
    body = client.get("/api/backtest/BTCUSDT?mode=scalp").json()
    assert body["signals"] >= body["missed"]
    assert body["exit_split"]["trades"] == body["signals"] - body["missed"]
    assert len(body["recent"]) <= 10
    assert client.get("/api/backtest/BTCUSDT?mode=x").status_code == 422


def test_paging_walks_backwards(monkeypatch):
    calls = []

    def fake(params):
        calls.append(params)
        end = params.get("endTime", 10_000_000) // 1000
        return [{"time": end - (params["limit"] - i) * 10} for i in range(params["limit"])]

    monkeypatch.setattr(market, "_klines_request", fake)
    rows = market.binance_klines_paged("BTCUSDT", "1h", 2500)
    times = [r["time"] for r in rows]
    assert len(rows) == 2500 and times == sorted(times) and len(set(times)) == 2500
    assert [c["limit"] for c in calls] == [1000, 1000, 500]
