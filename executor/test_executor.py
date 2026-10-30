import executor as ex

F = {"step": 0.001, "tick": 0.01, "min_notional": 5}
SIG = {"side": "long", "entry": 100.0, "stop_loss": 95.0, "rr": 2.5}


def test_floor_step_and_tick():
    assert ex.floor_step(1.23456, 0.001) == 1.234
    assert ex.floor_step(7.9, 1) == 7
    assert ex.round_tick(101.2345, 0.01) == 101.23


def test_plan_sizes_to_risk_and_keeps_rr():
    p = ex.plan(SIG, 100.0, 55, 1, F)
    assert p["side"] == "BUY" and p["qty"] == 0.11 and abs(p["risk_usd"] - 0.55) < 1e-9
    assert p["tp"] == 112.5 and p["sl"] == 95.0 and p["lev"] == 1


def test_plan_short_and_skips():
    short = {"side": "short", "entry": 100.0, "stop_loss": 104.0, "rr": 2.5}
    p = ex.plan(short, 100.0, 55, 1, F)
    assert p["side"] == "SELL" and p["tp"] == 90.0
    assert "skip" in ex.plan(SIG, 94.0, 55, 1, F)                         # past the stop
    assert "skip" in ex.plan(SIG, 104.0, 55, 1, F)                        # chased too far
    assert "minimal order" in ex.plan(SIG, 100.0, 55, 1, {**F, "min_notional": 100})["skip"]


def test_entry_window():
    close = 1793376000
    assert ex.in_entry_window(close + 60) == close
    assert ex.in_entry_window(close + 3600) is None


class FakeBinance:
    def __init__(self, cfg):
        self.calls = []

    def call(self, method, path, params=None, signed=True):
        self.calls.append((method, path, params))
        return {
            "/fapi/v2/positionRisk": [], "/fapi/v1/income": [],
            "/fapi/v1/premiumIndex": {"markPrice": "100"},
            "/fapi/v2/balance": [{"asset": "USDT", "balance": "55"}],
            "/fapi/v1/exchangeInfo": {"symbols": [{"symbol": "SOLUSDT", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01"}, {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]},
        }.get(path, {})


def test_run_dry_once_per_candle(tmp_path, monkeypatch):
    cfg = ex.Config({"SYMBOLS": "SOLUSDT", "DRY_RUN": "1"})
    cfg.state_file = tmp_path / "state.json"
    monkeypatch.setattr(ex, "Binance", FakeBinance)
    monkeypatch.setattr(ex, "http_json", lambda url: {"signal": {**SIG, "active": True}})
    sent = []
    monkeypatch.setattr(ex, "telegram", lambda c, t: sent.append(t))
    close = 1793376000
    ex.run(cfg, now=close + 120)
    ex.run(cfg, now=close + 400)
    assert len(sent) == 1 and sent[0].startswith("🧪 SIMULASI") and "SOL" in sent[0]
