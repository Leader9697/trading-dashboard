from app import research_deriv as D
from app.market import demo_klines


def fake_inputs(ltf):
    funding = [(c["time"], 0.0001 * ((i % 7) - 3)) for i, c in enumerate(ltf[::2])]
    metrics = []
    for i, c in enumerate(ltf):
        for k in range(48):  # 5-minute rows inside each 4h bar
            metrics.append((c["time"] + k * 300, 1000 + i + k, 1.0 + (i % 5) / 4, 0.9 + (i % 3) / 10))
    return funding, metrics


def test_features_use_only_past_data():
    ltf = demo_klines("BTCUSDT", "4h", 400)
    funding, metrics = fake_inputs(ltf)
    feat = D.features(ltf, 14400, funding, metrics)
    assert feat[0]["oi_chg"] is None  # no 24h history yet
    i = 200
    close = ltf[i]["time"] + 14400
    last = max(m for m in metrics if m[0] <= close)
    assert feat[i]["ls"] == last[2]
    assert feat[i]["funding_rank"] is not None


def test_run_reports_folds_and_baseline():
    data, feats = {}, {}
    for s in ("BTCUSDT", "ETHUSDT"):
        ltf = demo_klines(s, "4h", 1500)
        data[s] = (demo_klines(s, "1d", 600), ltf)
        feats[s] = D.features(ltf, 14400, *fake_inputs(ltf))
    out = D.run(data, feats)
    assert out["variants"] == 2 * 2 * 3 * 2 * 2
    assert out["baseline_v2"]["base"] == "v2" and len(out["baseline_v2"]["folds"]) == 4
