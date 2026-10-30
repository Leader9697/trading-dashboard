import httpx
from fastapi.testclient import TestClient

from app import journal, main
from app.market import demo_klines


def bars(n, high=105.0, low=102.0, start=0, step=100):
    return [{"time": start + i * step, "open": low, "high": high, "low": low, "close": (high + low) / 2} for i in range(n)]


ROW = [50, "BTCUSDT", "L", 100.0, 95.0, 110.0, 115.0]


def test_pending_while_order_window_open():
    assert journal.evaluate(ROW, bars(3))["status"] == "pending"


def test_missed_after_window():
    r = journal.evaluate(ROW, bars(10))
    assert r["status"] == "missed" and r["r"] == 0.0


def test_open_then_closed_trade():
    c = bars(3) + [{"time": 300, "open": 101, "high": 102, "low": 99, "close": 101}]
    assert journal.evaluate(ROW, c)["status"] == "open"
    c.append({"time": 400, "open": 101, "high": 101, "low": 94, "close": 95})
    r = journal.evaluate(ROW, c)
    assert r["status"] == "stop" and r["r"] < -1


def test_summary_skips_open_rows():
    s = journal.summarize([{"status": "open", "r": 1.0}, {"status": "tp2", "r": 2.5}, {"status": "missed", "r": 0.0}])
    assert s["trades"] == 1 and s["missed"] == 1


def test_journal_endpoint(monkeypatch):
    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.setenv("EDGE_CONFIG", "https://edge-config.vercel.com/ecfg_abc?token=tok")
    main._cache.clear()
    t = demo_klines("BTCUSDT", "4h", 300)[-20]["time"]

    def fake_get(url, headers=None, timeout=None, params=None):
        assert url.endswith("/item/journal")
        return httpx.Response(200, json=[[t, "BTCUSDT", "S", 1e9, 2e9, 1.0, 0.5]])

    monkeypatch.setattr(main.httpx, "get", fake_get)
    body = TestClient(main.app).get("/api/journal").json()
    assert body["enabled"] and body["results"][0]["status"] == "missed"
    assert "BTCUSDT" in body["candidates"] and len(body["candidates"]["ETHUSDT"]) == 7


def test_journal_unconfigured(client):
    body = client.get("/api/journal").json()
    assert body["enabled"] is False and body["rows"] == [] and body["candidates"]
