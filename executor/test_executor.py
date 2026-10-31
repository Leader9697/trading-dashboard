import executor as ex

F = {"step": 0.01, "tick": 0.01, "min_notional": 5}
SIG = {"side": "long", "entry": 100.0, "stop_loss": 95.0, "rr": 4.0}
CLOSE = 1793376000  # a 4h boundary
OPEN = CLOSE - ex.STEP  # open time of the candle that closed at CLOSE


def test_floor_step_and_tick():
    assert ex.floor_step(1.23456, 0.001) == 1.234
    assert ex.floor_step(7.9, 1) == 7
    assert ex.round_tick(101.2345, 0.01) == 101.23


def test_plan_sizes_like_the_engine():
    p = ex.plan(SIG, 60, 1, F)
    assert p["side"] == "BUY" and p["qty"] == 0.12 and abs(p["risk_usd"] - 0.6) < 1e-9
    assert p["entry"] == 100.0 and p["sl"] == 95.0 and p["tp"] == 120.0
    assert p["lev"] == 10  # SL 5% -> under 1/(2 x 5%)


def test_plan_short_and_skips():
    p = ex.plan({"side": "short", "entry": 100.0, "stop_loss": 104.0, "rr": 4.0}, 60, 1, F)
    assert p["side"] == "SELL" and p["tp"] == 84.0
    assert "minimal order" in ex.plan(SIG, 60, 1, {**F, "min_notional": 100})["skip"]
    assert "skip" in ex.plan({**SIG, "stop_loss": 101.0}, 60, 1, F)


def test_windows_and_time_exit():
    assert ex.candle_closed(CLOSE + 60) == OPEN
    assert ex.in_entry_window(CLOSE + 60) and not ex.in_entry_window(CLOSE + 3600)
    # filled inside the candle opening at CLOSE: out at the close of the 31st candle after it
    assert ex.exit_due(CLOSE + 100) == CLOSE + 32 * ex.STEP


class FakeBinance:
    def __init__(self, cfg):
        self.calls = []
        self.positions = []

    def call(self, method, path, params=None, signed=True, base=None):
        self.calls.append((method, path, params))
        return {
            "/fapi/v2/positionRisk": self.positions, "/fapi/v1/income": [],
            "/fapi/v1/premiumIndex": {"markPrice": "100.5"},
            "/fapi/v2/balance": [{"asset": "USDT", "balance": "60", "availableBalance": "60"}],
            "/fapi/v1/exchangeInfo": {"symbols": [{"symbol": s, "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01"}, {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in ("SOLUSDT", "XRPUSDT", "ADAUSDT")]},
        }.get(path, {})


def portfolio(max_pos=6, as_of=OPEN):
    row = {"active": True, "side": "long", "entry": 100.0, "stop_loss": 95.0, "take_profit": 120.0, "as_of": as_of}
    return {"available": True, "settings": {"risk_pct": 1.0, "max_pos": max_pos},
            "recommended": ["SOLUSDT", "XRPUSDT", "ADAUSDT"],
            "live": [{**row, "symbol": "SOLUSDT", "score": 50}, {**row, "symbol": "XRPUSDT", "score": 70},
                     {**row, "symbol": "ADAUSDT", "score": 60, "active": False}]}


def setup(tmp_path, monkeypatch, dry="1", data=None):
    cfg = ex.Config({"DRY_RUN": dry})
    cfg.state_file = tmp_path / "state.json"
    fake = FakeBinance(cfg)
    monkeypatch.setattr(ex, "Binance", lambda c: fake)
    monkeypatch.setattr(ex, "http_json", lambda url: data or portfolio())
    sent = []
    monkeypatch.setattr(ex, "telegram", lambda c, t: sent.append(t))
    return cfg, fake, sent


def test_dry_run_once_per_candle_strongest_first(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch)
    ex.run(cfg, now=CLOSE + 120)
    ex.run(cfg, now=CLOSE + 400)
    assert len(sent) == 2 and all(t.startswith("🧪 SIMULASI") for t in sent)
    assert "XRP" in sent[0] and "SOL" in sent[1]
    assert not any(c[0] == "POST" for c in fake.calls)


def test_slot_cap(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    assert "XRP" in sent[0] and "slot penuh" in sent[1]


def test_waits_for_fresh_candle(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, data=portfolio(as_of=OPEN - ex.STEP))
    ex.run(cfg, now=CLOSE + 120)
    assert sent == [] and ex.load_state(cfg)["seen"] == []


def test_live_lifecycle(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    orders = [c[2] for c in fake.calls if c[:2] == ("POST", "/fapi/v1/order")]
    assert orders[0]["type"] == "LIMIT" and orders[0]["price"] == 100.0 and orders[0]["symbol"] == "XRPUSDT"
    assert orders[0]["goodTillDate"] == (CLOSE + 4 * ex.STEP) * 1000
    assert sent[0].startswith("📥")

    # fills: stop and target go in, closing the whole position on the mark price
    fake.positions = [{"symbol": "XRPUSDT", "positionAmt": "0.12", "entryPrice": "100"}]
    fake.calls.clear()
    ex.run(cfg, now=CLOSE + 3600)
    kinds = [c[2]["type"] for c in fake.calls if c[:2] == ("POST", "/fapi/v1/order")]
    assert kinds == ["STOP_MARKET", "TAKE_PROFIT_MARKET"] and sent[-1].startswith("🎯")
    tr = ex.load_state(cfg)["trades"]["XRPUSDT"]
    assert tr["stage"] == "open" and tr["exit_at"] == CLOSE + 32 * ex.STEP

    # time exit: market close, reduce only
    fake.calls.clear()
    ex.run(cfg, now=CLOSE + 32 * ex.STEP + 5)
    close = [c[2] for c in fake.calls if c[:2] == ("POST", "/fapi/v1/order")][-1]
    assert close["type"] == "MARKET" and close["reduceOnly"] == "true" and close["side"] == "SELL"
    assert sent[-1].startswith("⏱️") and ex.load_state(cfg)["trades"] == {}


def test_unfilled_limit_is_cancelled(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    ex.run(cfg, now=CLOSE + 4 * ex.STEP + 5)
    assert sent[-1].startswith("⌛") and ex.load_state(cfg)["trades"] == {}
