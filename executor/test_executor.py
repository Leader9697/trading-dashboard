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
        self.algo = []
        self.fail = {}

    def call(self, method, path, params=None, signed=True, base=None):
        self.calls.append((method, path, params))
        if self.fail.get((method, path)):
            raise self.fail[(method, path)].pop(0)
        return {
            "/fapi/v1/openAlgoOrders": self.algo, "/fapi/v1/openOrders": [],
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
    monkeypatch.setattr(ex, "HEARTBEAT_HOUR_UTC", 24)  # off unless a test turns it on
    monkeypatch.setattr(ex, "HERE", tmp_path)
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


POS = [{"symbol": "XRPUSDT", "positionAmt": "0.12", "entryPrice": "100"}]


def posts(fake, path="/fapi/v1/order"):
    return [c[2] for c in fake.calls if c[:2] == ("POST", path)]


def test_lost_reply_still_tracked_and_protected(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    fake.fail[("POST", "/fapi/v1/order")] = [TimeoutError("read timed out")]
    ex.run(cfg, now=CLOSE + 120)
    assert ex.load_state(cfg)["trades"]["XRPUSDT"]["stage"] == "pending" and sent[0].startswith("⚠️")
    fake.positions = POS
    fake.calls.clear()
    ex.run(cfg, now=CLOSE + 3600)
    assert [o["type"] for o in posts(fake)] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_rejected_order_is_forgotten(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    fake.fail[("POST", "/fapi/v1/order")] = [ex.BinanceError(-2019, "Margin is insufficient")] * 2
    ex.run(cfg, now=CLOSE + 120)
    assert "XRPUSDT" not in ex.load_state(cfg)["trades"] and sent[0].startswith("❌")


def test_watchdog_replaces_missing_stop_only(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    fake.positions = POS
    ex.run(cfg, now=CLOSE + 3600)
    fake.calls.clear()
    fake.algo = [{"symbol": "XRPUSDT", "orderType": "TAKE_PROFIT_MARKET"}]
    ex.run(cfg, now=CLOSE + 3600 + 60)  # too soon to re-check
    assert posts(fake) == []
    ex.run(cfg, now=CLOSE + 3600 + ex.WATCH_EVERY + 1)
    assert [o["type"] for o in posts(fake)] == ["STOP_MARKET"] and sent[-1].startswith("🛡️")


def test_existing_stop_not_duplicated_on_fill(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    fake.positions = POS
    fake.algo = [{"symbol": "XRPUSDT", "orderType": "STOP_MARKET"}]
    fake.calls.clear()
    ex.run(cfg, now=CLOSE + 3600)
    assert [o["type"] for o in posts(fake)] == ["TAKE_PROFIT_MARKET"]


def test_untracked_position_reported_once(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    fake.positions = [{"symbol": "DOGEUSDT", "positionAmt": "50", "entryPrice": "0.2"}]
    ex.run(cfg, now=CLOSE + 3600)
    ex.run(cfg, now=CLOSE + 3700)
    assert sum("DOGE" in t for t in sent) == 1


def test_pause_file_blocks_new_entries(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    (tmp_path / ex.PAUSE_FILE).touch()
    ex.run(cfg, now=CLOSE + 120)
    assert posts(fake) == [] and sent[0].startswith("⏸️")


def test_min_balance_blocks_new_entries(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    cfg.min_balance = 70
    ex.run(cfg, now=CLOSE + 120)
    assert posts(fake) == [] and "MIN_BALANCE" in sent[0]


def test_errored_coin_retried_and_stale_data_reported(tmp_path, monkeypatch):
    data = portfolio()
    data["live"][0] = {"symbol": "SOLUSDT", "active": False, "error": "timeout"}
    cfg, fake, sent = setup(tmp_path, monkeypatch, data=data)
    ex.run(cfg, now=CLOSE + 120)
    assert "SOLUSDT:%d" % OPEN not in ex.load_state(cfg)["seen"]
    # next candle: the dashboard never serves it fresh -> one warning after the window
    monkeypatch.setattr(ex, "http_json", lambda url: portfolio(as_of=OPEN))
    ex.run(cfg, now=CLOSE + ex.STEP + 60)
    ex.run(cfg, now=CLOSE + ex.STEP + 3600)
    ex.run(cfg, now=CLOSE + ex.STEP + 3700)
    assert sum("terlewat" in t for t in sent) == 1


def test_daily_heartbeat(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ex, "HEARTBEAT_HOUR_UTC", 0)
    ex.run(cfg, now=CLOSE + 3600)
    ex.run(cfg, now=CLOSE + 3700)
    assert sum(t.startswith("💓") for t in sent) == 1


def test_monthly_report_on_the_first(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ex, "HEARTBEAT_HOUR_UTC", 0)
    first = 1793491200  # 2026-11-01 00:00 UTC
    assert ex.time.strftime("%Y-%m-%d", ex.time.gmtime(first)) == "2026-11-01"
    ex.run(cfg, now=first + 600)
    assert any(t.startswith("📊 Hasil bulan 10/2026") for t in sent)
    q = [c[2] for c in fake.calls if c[1] == "/fapi/v1/income"][0]
    assert ex.time.gmtime(q["startTime"] / 1000)[:3] == (2026, 10, 1)


def test_watchdog_does_nothing_when_algo_list_unreadable(tmp_path, monkeypatch):
    cfg, fake, sent = setup(tmp_path, monkeypatch, dry="0", data=portfolio(max_pos=1))
    ex.run(cfg, now=CLOSE + 120)
    fake.positions = POS
    ex.run(cfg, now=CLOSE + 3600)
    fake.calls.clear()
    fake.fail[("GET", "/fapi/v1/openAlgoOrders")] = [ex.BinanceError(-1000, "unknown")]
    ex.run(cfg, now=CLOSE + 3600 + ex.WATCH_EVERY + 1)
    assert posts(fake) == [] and "XRPUSDT" in ex.load_state(cfg)["trades"]
