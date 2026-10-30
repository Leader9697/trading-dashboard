import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.indicators import ema, pivots, rsi
from app.market import demo_klines
from app.setup import build_levels, build_setup, compact, rating


def test_config(client):
    body = client.get("/api/config").json()
    assert body["watchlist"][0] == "BTCUSDT"
    assert body["modes"]["swing"] == {"trend": "1d", "entry": "4h"}
    assert body["debate_enabled"] is False


def test_candles_have_unix_times_and_emas(client):
    body = client.get("/api/candles/btcusdt?interval=1h&limit=300").json()
    assert len(body["candles"]) == 300
    assert isinstance(body["candles"][0]["time"], int)
    assert len(body["indicators"]["ema200"]) == 101


@pytest.mark.parametrize("mode", ["swing", "scalp"])
def test_setup_levels_are_consistent(client, mode):
    s = client.get(f"/api/setup/ETHUSDT?mode={mode}").json()
    lv = s["levels"]
    assert s["direction"] in {"long", "short", "wait"}
    if s["plan_side"] == "long":
        assert lv["stop_loss"] < lv["entry"] < lv["tp1"] < lv["tp2"]
    else:
        assert lv["tp2"] < lv["tp1"] < lv["entry"] < lv["stop_loss"]
    assert 0.8 <= lv["sl_atr"] <= 2.5 + 1e-6
    assert len(s["checklist"]) == 6


def test_snapshot_is_compact(client):
    body = client.get("/api/snapshot").json()
    assert [c["s"] for c in body["coins"]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    assert len(client.get("/api/snapshot").content) < 2500


def test_bad_inputs(client):
    assert client.get("/api/setup/BTC-USDT").status_code == 422
    assert client.get("/api/setup/BTCUSDT?mode=yolo").status_code == 422


def test_debate_unconfigured(client):
    assert client.get("/api/debate").json() == {"available": False}


def test_debate_reads_edge_config(monkeypatch):
    monkeypatch.setenv("EDGE_CONFIG", "https://edge-config.vercel.com/ecfg_abc?token=tok")
    seen = {}

    def fake_get(url, headers=None, timeout=None, params=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return httpx.Response(200, json={"updated_at": "2026-10-30T12:00:00Z", "coins": {}})

    monkeypatch.setattr(main.httpx, "get", fake_get)
    body = TestClient(main.app).get("/api/debate").json()
    assert body["available"] is True and body["updated_at"].startswith("2026")
    assert seen == {"url": "https://edge-config.vercel.com/ecfg_abc/item/debate", "auth": "Bearer tok"}


def test_indicator_basics():
    assert ema([1, 2, 3], 3)[-1] == 2
    assert rsi([float(i) for i in range(30)])[-1] == 100.0
    bars = [{"high": h, "low": h - 1} for h in [1, 2, 3, 9, 3, 2, 1, 2, 3]]
    highs, lows = pivots(bars, width=3)
    assert highs == [9]


def test_long_levels_use_nearest_support():
    ltf = {"atr": 10.0, "ema20": 990.0}
    lv = build_levels("long", 1000.0, ltf, highs=[1100.0], lows=[900.0, 960.0, 985.0])
    # anchor = max(985 support, 990 ema20) = 990; SL just beyond the 985 support
    assert lv["entry"] == pytest.approx(991.5)
    assert lv["stop_loss"] == pytest.approx(982.0)
    assert lv["tp1"] == pytest.approx(991.5 + 2 * 9.5)
    assert lv["hurdle"] is None


def test_strong_uptrend_gives_long():
    up = [{"time": i, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 10.0}
          for i in range(300)]
    s = build_setup("TEST", "swing", up, up, {"funding_pct": 0.01}, None)
    assert s["direction"] == "long" and rating(s["score"]) in {"Buy", "Overweight"}
    assert compact(s)["plan"][0] == "long"


def test_demo_klines_deterministic():
    assert demo_klines("BTCUSDT", "4h", 50) == demo_klines("BTCUSDT", "4h", 50)
