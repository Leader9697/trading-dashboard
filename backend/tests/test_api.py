import httpx
import pytest
from fastapi.testclient import TestClient

from app import signals
from app.main import app
from app.providers import demo_candles


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.delenv("DSA_URL", raising=False)
    monkeypatch.delenv("DEXTER_URL", raising=False)
    return TestClient(app)


def test_config_reports_demo_sources(client):
    body = client.get("/api/config").json()
    assert body["sources"] == {"candles": "demo", "decision": "rules", "research": None}
    assert "BBCA.JK" in body["watchlist"]


def test_candles_are_deterministic_and_have_indicators(client):
    a = client.get("/api/candles/bbca.jk?days=120").json()
    b = client.get("/api/candles/BBCA.JK?days=120").json()
    assert a == b
    assert len(a["candles"]) == 120
    assert len(a["indicators"]["sma20"]) == 101
    assert len(a["indicators"]["sma50"]) == 71


def test_decision_from_rules(client):
    body = client.get("/api/decision/AAPL").json()
    assert body["source"] == "rules"
    assert body["action"] in signals.ACTION_LABELS
    assert 0 <= body["score"] <= 100
    assert body["strategy"]["stop_loss"]
    for m in body["markers"]:
        assert m["action"] in {"buy", "sell"}


def test_invalid_ticker_rejected(client):
    assert client.get("/api/candles/AA$PL").status_code == 422


def test_research_needs_dexter(client):
    r = client.post("/api/research", json={"symbol": "AAPL", "question": "margin 5 tahun"})
    assert r.status_code == 503


def test_sma_and_rsi_basics():
    assert signals.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    rising = [float(i) for i in range(30)]
    assert signals.rsi(rising)[-1] == 100.0


def test_rule_decision_needs_history():
    with pytest.raises(signals.ProviderError):
        signals.rule_decision(demo_candles("X", 30))


def test_dsa_decision_maps_report(monkeypatch):
    monkeypatch.setenv("DSA_URL", "http://dsa")
    monkeypatch.delenv("DATA_MODE", raising=False)

    def fake_get(url, params=None, timeout=None):
        req = httpx.Request("GET", url)
        if url.endswith("/api/v1/history"):
            return httpx.Response(200, request=req, json={"items": [
                {"id": 7, "action": "buy", "action_label": "买入", "created_at": "2026-10-29T18:00:00"},
                {"id": 6, "action": "hold", "created_at": "2026-10-28T18:00:00"},
            ]})
        assert url.endswith("/api/v1/history/7")
        return httpx.Response(200, request=req, json={
            "meta": {"created_at": "2026-10-29T18:00:00"},
            "summary": {"action": "buy", "sentiment_score": 78, "trend_prediction": "看多",
                        "analysis_summary": "Momentum kuat", "operation_advice": "Beli bertahap\nJaga stop"},
            "strategy": {"ideal_buy": "9100", "stop_loss": "8800", "take_profit": "9800"},
            "details": {"raw_result": {"dashboard": {"intelligence": {"risk_alerts": ["Rupiah melemah"]}}}},
        })

    monkeypatch.setattr(signals.httpx, "get", fake_get)
    body = TestClient(app).get("/api/decision/BBCA.JK").json()
    assert body["source"] == "daily_stock_analysis"
    assert body["action"] == "buy" and body["score"] == 78
    assert body["reasons"] == ["Beli bertahap", "Jaga stop"]
    assert body["risks"] == ["Rupiah melemah"]
    assert [m["time"] for m in body["markers"]] == ["2026-10-28", "2026-10-29"]
    assert body["strategy"]["stop_loss"] == "8800"
