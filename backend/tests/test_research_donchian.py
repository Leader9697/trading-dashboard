from app import research_donchian as RD
from app.backtest import FEE_ROUND_TRIP

DAY = 86400
F = {"step": 0.001, "min_notional": 5.0}
COST = FEE_ROUND_TRIP + RD.SLIP_ROUND_TRIP


def _bar(i, o, h, lo, c):
    return {"time": i * RD.STEP, "open": o, "high": h, "low": lo, "close": c}


def test_vol_rank_counts_past_values_at_or_below_current():
    # returns alternate +-1% then the swing triples: the newest value ranks above all past ones
    closes, p = [], 100.0
    for i in range(30):
        p *= 1.01 if i % 2 else 0.99
        closes.append(p)
    for i in range(6):
        p *= 1.03 if i % 2 else 0.97
        closes.append(p)
    l = [_bar(i, c, c, c, c) for i, c in enumerate(closes)]
    vr = RD.vol_rank(l, vol_len=4, rank_len=10)
    assert vr[13] is None and vr[14] is not None
    assert vr[-1] == 1.0


def test_outcome_measures_from_signal_close_and_enters_next_open():
    l = [_bar(0, 100, 101, 99, 100), _bar(1, 101, 102, 100.5, 101.5)]  # entry at 101
    l += [_bar(2, 101.5, 108, 101, 107)]  # target = 100 + 1.5 * 2 * 2.5 = 107.5 not hit; high 108 hits it
    o = RD.outcome(l, 0, "long", 2.0)
    risk = 101 - (100 - 3)
    assert o["status"] == "tp" and o["entry"] == 101
    assert abs(o["r"] - ((107.5 - 101) / risk - COST * 101 / risk)) < 1e-3


def test_outcome_stop_wins_on_the_entry_bar_and_gaps_fill_at_open():
    l = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 108, 96, 100)]  # entry bar touches both: stop
    o = RD.outcome(l, 0, "long", 2.0)
    assert o["status"] == "stop" and o["r"] < -1
    l = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 100.5, 99, 100), _bar(2, 95, 95, 90, 91)]  # gap below 97
    o = RD.outcome(l, 0, "long", 2.0)
    assert o["status"] == "stop" and abs(o["r"] - (-5 / 3 - COST * 100 / 3)) < 1e-3


def test_funding_is_charged_to_longs_and_paid_to_shorts():
    sig = {"t": 0, "end": 3 * DAY, "side": "l", "sl_pct": 2.0, "r": 1.0, "entry": 100, "score": -1}
    out = RD.with_funding([sig, {**sig, "side": "s"}], [(-1, 0.5), (DAY, 0.0001), (2 * DAY, 0.0001), (4 * DAY, 0.5)])
    assert abs(out[0]["r"] - (1 - 0.0002 * 100 / 2)) < 1e-9 and abs(out[1]["r"] - (1 + 0.01)) < 1e-9


def _sig(t, r, hold=2 * DAY, score=-1):
    return {"t": t, "side": "l", "score": score, "end": t + hold, "entry": 100.0, "sl_pct": 3.0, "r": r, "status": "x"}


def test_replay_halves_size_after_a_loss_and_keeps_one_position_per_coin():
    books = {"don": {"AAAUSDT": [_sig(0, -1.0), _sig(3 * DAY, 2.0), _sig(6 * DAY, 1.0)]},
             "v3": {"AAAUSDT": [_sig(DAY, 4.0, score=50)]}}  # same coin, already held by the other strategy
    out = RD.replay(books, {"AAAUSDT": F}, 0, 10 * DAY, 1.0, 6, cut={"don": 0.5})
    assert out["trades"] == 3 and out["skipped"]["busy"] == 1
    # $0.60 risk lost, then half size (0.29 after the loss) x 2R, then full size again
    assert abs(out["final"] - (60 - 0.6 + 2 * 0.29 * 1 + 0.6)) < 0.02
