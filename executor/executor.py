"""Places the v3 portfolio engine's signals as Binance USDT-M futures orders.

Runs 24/7 on a small server. Shortly after each 4h close it reads the engine from the
dashboard (/api/portfolio: coin list, risk, max positions and this candle's signal per
coin) and trades it the way the backtest does:

- entry is a LIMIT order at the signal's entry price, valid 4 candles (16h), then cancelled
- as soon as any of it fills, a stop loss and a take profit (4R) that close the whole
  position go in, both triggered on the mark price
- a position still open 31 candles after its fill candle is closed at market (time exit)
- one position per coin, at most MAX_POSITIONS positions and pending entries together,
  strongest signal first, size = RISK_PCT of the wallet, isolated margin with leverage
  under 1/(2 x SL%), coins whose order would be below Binance's minimum are skipped

Every order, fill, cancel, exit and error is sent to Telegram. Standard library only.
Settings come from a .env file next to this script.

  python3 executor.py check    # connection, key permissions, balance; places nothing
  python3 executor.py status   # what the executor is tracking now
  python3 executor.py loop     # what the server runs: a tick every 30 seconds
  python3 executor.py run      # one tick

DRY_RUN=1 (the default) reports what it would do without ordering.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEP = 4 * 3600          # 4h candles
ENTRY_WINDOW = 30 * 60   # only place new entries within 30 minutes of a close
LIMIT_BARS = 4           # an unfilled entry is cancelled after 4 candles (16h)
MAX_HOLD_BARS = 30       # backtest: exit at the close of the 31st candle after the fill candle
MAX_LEVERAGE = 20
TICK_SECONDS = 30


def load_env(path: Path = HERE / ".env") -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **{k: os.environ[k] for k in env if k in os.environ}}


class Config:
    def __init__(self, env: dict):
        self.key = env.get("BINANCE_API_KEY", "")
        self.secret = env.get("BINANCE_API_SECRET", "")
        self.testnet = env.get("BINANCE_TESTNET", "0") == "1"
        self.base = "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        self.dashboard = env.get("DASHBOARD_URL", "https://trading-dashboard-two-orcin.vercel.app").rstrip("/")
        # Empty = take the coin list, risk and max positions from the engine on the dashboard
        self.symbols = [s.strip().upper() for s in env.get("SYMBOLS", "").split(",") if s.strip()]
        self.risk_pct = float(env["RISK_PCT"]) if env.get("RISK_PCT") else None
        self.max_pos = int(env["MAX_POSITIONS"]) if env.get("MAX_POSITIONS") else None
        self.dry_run = env.get("DRY_RUN", "1") != "0"
        self.tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = env.get("TELEGRAM_CHAT_ID", "")
        self.state_file = HERE / "state.json"


class BinanceError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"{code} {msg}")
        self.code = code


class Binance:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def call(self, method: str, path: str, params: dict | None = None, signed: bool = True, base: str | None = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.cfg.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
        else:
            query = urllib.parse.urlencode(params)
        url = f"{base or self.cfg.base}{path}"
        data = None
        if method == "GET" or method == "DELETE":
            url += f"?{query}" if query else ""
        else:
            data = query.encode()
        req = urllib.request.Request(url, data=data, method=method, headers={"X-MBX-APIKEY": self.cfg.key})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except ValueError:
                raise BinanceError(exc.code, exc.reason) from exc
            raise BinanceError(body.get("code", exc.code), body.get("msg", "")) from exc


# ---- pure helpers (tested) ----------------------------------------------------

def _decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step)))) if 0 < step < 1 else 0


def floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(math.floor(value / step + 1e-9) * step, _decimals(step))


def round_tick(value: float, tick: float) -> float:
    return round(round(value / tick) * tick, _decimals(tick))


def plan(sig: dict, equity: float, risk_pct: float, filters: dict) -> dict:
    """Limit order for a signal, sized like the engine's replay: whole lot steps at the
    planned risk, leverage under 1/(2 x SL%) so liquidation sits beyond the stop.
    Returns {"skip": reason} when Binance would refuse the order."""
    long = sig["side"] == "long"
    entry = round_tick(sig["entry"], filters["tick"])
    sl = round_tick(sig["stop_loss"], filters["tick"])
    risk_per_unit = (entry - sl) if long else (sl - entry)
    if risk_per_unit <= 0:
        return {"skip": "stop loss tidak valid"}
    tp = round_tick(entry + (1 if long else -1) * sig["rr"] * risk_per_unit, filters["tick"])
    qty = floor_step(equity * risk_pct / 100 / risk_per_unit, filters["step"])
    notional = qty * entry
    if qty <= 0 or notional < filters["min_notional"]:
        return {"skip": f"di bawah minimal order {filters['min_notional']:g} USDT"}
    sl_pct = 100 * risk_per_unit / entry
    lev = max(1, min(MAX_LEVERAGE, math.floor(100 / (2 * sl_pct))))
    return {
        "side": "BUY" if long else "SELL", "close_side": "SELL" if long else "BUY",
        "qty": qty, "entry": entry, "sl": sl, "tp": tp, "lev": lev,
        "notional": notional, "margin": notional / lev, "risk_usd": qty * risk_per_unit,
    }


def candle_closed(now: float) -> int:
    """Open time of the 4h candle that closed most recently."""
    return int(now // STEP * STEP) - STEP


def in_entry_window(now: float) -> bool:
    return now - (candle_closed(now) + STEP) <= ENTRY_WINDOW


def exit_due(fill_time: float) -> int:
    """When a position filled at `fill_time` is closed by the 5-day time exit."""
    return int(fill_time // STEP * STEP) + (MAX_HOLD_BARS + 2) * STEP


# ---- I/O ------------------------------------------------------------------------

def http_json(url: str):
    with urllib.request.urlopen(url, timeout=90) as resp:
        return json.loads(resp.read())


def telegram(cfg: Config, text: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), text, flush=True)
    if not (cfg.tg_token and cfg.tg_chat):
        return
    tag = "[TESTNET] " if cfg.testnet else ""
    body = urllib.parse.urlencode({"chat_id": cfg.tg_chat, "text": tag + text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage", data=body, timeout=15)
    except urllib.error.URLError as exc:
        print(f"telegram gagal: {exc}", file=sys.stderr)


def load_state(cfg: Config) -> dict:
    try:
        state = json.loads(cfg.state_file.read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("seen", [])
    state.setdefault("trades", {})
    return state


def save_state(cfg: Config, state: dict) -> None:
    state["seen"] = state["seen"][-300:]
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(cfg.state_file)


def symbol_filters(bn: Binance) -> dict:
    out = {}
    for s in bn.call("GET", "/fapi/v1/exchangeInfo", signed=False)["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        out[s["symbol"]] = {
            "step": float(f["LOT_SIZE"]["stepSize"]),
            "tick": float(f["PRICE_FILTER"]["tickSize"]),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
        }
    return out


def balances(bn: Binance) -> tuple[float, float]:
    """(wallet, available) USDT."""
    for b in bn.call("GET", "/fapi/v2/balance"):
        if b["asset"] == "USDT":
            return float(b["balance"]), float(b["availableBalance"])
    return 0.0, 0.0


def open_positions(bn: Binance) -> dict:
    return {p["symbol"]: p for p in bn.call("GET", "/fapi/v2/positionRisk") if float(p["positionAmt"]) != 0}


def mark_price(bn: Binance, symbol: str) -> float:
    return float(bn.call("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)["markPrice"])


def conditional(bn: Binance, symbol: str, side: str, kind: str, price: float):
    """Stop loss / take profit that closes the whole position on the mark price. Binance
    moved these order types to the algo endpoint in late 2025, so fall back on -4120."""
    try:
        return bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": side, "type": kind, "stopPrice": price,
                                                  "closePosition": "true", "workingType": "MARK_PRICE"})
    except BinanceError as exc:
        if exc.code != -4120:
            raise
    return bn.call("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": kind,
                                                  "triggerPrice": price, "closePosition": "true", "workingType": "MARK_PRICE"})


def cancel_all(bn: Binance, symbol: str) -> None:
    for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
        try:
            bn.call("DELETE", path, {"symbol": symbol})
        except BinanceError:
            pass


def cancel_entry(bn: Binance, tr: dict) -> None:
    try:
        bn.call("DELETE", "/fapi/v1/order", {"symbol": tr["symbol"], "origClientOrderId": tr["cid"]})
    except BinanceError as exc:
        if exc.code not in (-2011, -2013):  # already gone
            raise


def realized_since(bn: Binance, symbol: str, since: float) -> float:
    rows = bn.call("GET", "/fapi/v1/income", {"symbol": symbol, "startTime": int(since * 1000), "limit": 1000})
    return sum(float(r["income"]) for r in rows if r["incomeType"] in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))


def set_leverage(bn: Binance, symbol: str, lev: int) -> int:
    try:
        bn.call("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except BinanceError as exc:
        if exc.code != -4046:  # already isolated
            raise
    for x in sorted({lev, min(lev, 10), min(lev, 5)}, reverse=True):
        try:
            bn.call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": x})
            return x
        except BinanceError as exc:
            last = exc
    raise last


def coin(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def fmt(p: dict, symbol: str) -> str:
    return (f"{'LONG' if p['side'] == 'BUY' else 'SHORT'} {coin(symbol)} {p['qty']:g} limit {p['entry']:g}, "
            f"SL {p['sl']:g}, TP {p['tp']:g}, {p['lev']}x isolated, nilai ${p['notional']:.2f}, "
            f"risiko ${p['risk_usd']:.2f}")


# ---- the engine's steps ------------------------------------------------------------

def manage(cfg: Config, bn: Binance, state: dict, positions: dict, now: float) -> None:
    """Fills, stop/target, unfilled cancels, closes and the time exit for tracked trades."""
    for symbol, tr in list(state["trades"].items()):
        if tr["stage"] == "sim":
            if now >= tr["expires"]:
                del state["trades"][symbol]
            continue
        pos = positions.get(symbol)
        if pos and not tr.get("protected"):
            if tr["stage"] == "pending":
                tr["stage"], tr["filled_at"] = "open", now
                tr["exit_at"] = exit_due(now)
            try:
                conditional(bn, symbol, tr["close_side"], "STOP_MARKET", tr["sl"])
            except BinanceError as exc:
                # Never leave a position without a stop
                cancel_all(bn, symbol)
                bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": tr["close_side"], "type": "MARKET",
                                                   "quantity": abs(float(pos["positionAmt"])), "reduceOnly": "true"})
                del state["trades"][symbol]
                telegram(cfg, f"⚠️ {coin(symbol)}: stop loss gagal dipasang ({exc}), posisi langsung ditutup.")
                continue
            tr["protected"] = True
            msg = f"🎯 {coin(symbol)} terisi {abs(float(pos['positionAmt'])):g} @ {float(pos['entryPrice']):g}. SL {tr['sl']:g} terpasang"
            try:
                conditional(bn, symbol, tr["close_side"], "TAKE_PROFIT_MARKET", tr["tp"])
                msg += f", TP {tr['tp']:g} terpasang."
            except BinanceError as exc:
                msg += f".\n⚠️ TP gagal dipasang ({exc}), pasang manual di {tr['tp']:g}."
            telegram(cfg, msg + f" Batas waktu: {time.strftime('%d/%m %H:%M', time.gmtime(tr['exit_at'] + 7 * 3600))} WIB.")
        if tr["stage"] == "open" and pos and now >= tr["expires"] and not tr.get("entry_done"):
            cancel_entry(bn, tr)  # partly filled: the rest of the limit lapses like an unfilled one
            tr["entry_done"] = True
        if tr["stage"] == "pending" and now >= tr["expires"]:
            cancel_entry(bn, tr)
            if symbol in open_positions(bn):  # filled in the last moments
                continue
            cancel_all(bn, symbol)
            del state["trades"][symbol]
            telegram(cfg, f"⌛ {coin(symbol)}: limit {tr['entry']:g} tidak terisi dalam 16 jam, dibatalkan.")
        elif tr["stage"] == "open" and not pos:
            cancel_all(bn, symbol)
            pnl = realized_since(bn, symbol, tr["filled_at"] - 60)
            del state["trades"][symbol]
            telegram(cfg, f"{'✅' if pnl >= 0 else '🛑'} {coin(symbol)} {tr['dir']} selesai: {'+' if pnl >= 0 else ''}{pnl:.2f} USDT "
                          f"(SL/TP kena atau ditutup manual).")
        elif tr["stage"] == "open" and pos and now >= tr["exit_at"]:
            cancel_all(bn, symbol)
            bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": tr["close_side"], "type": "MARKET",
                                               "quantity": abs(float(pos["positionAmt"])), "reduceOnly": "true"})
            pnl = realized_since(bn, symbol, tr["filled_at"] - 60)
            del state["trades"][symbol]
            telegram(cfg, f"⏱️ {coin(symbol)} {tr['dir']} ditutup karena sudah 5 hari (time exit): "
                          f"{'+' if pnl >= 0 else ''}{pnl:.2f} USDT.")


def engine(cfg: Config) -> dict:
    data = http_json(f"{cfg.dashboard}/api/portfolio")
    if not data.get("available"):
        raise RuntimeError("engine portofolio di dashboard belum tersedia")
    st = data["settings"]
    return {
        "symbols": cfg.symbols or data["recommended"],
        "risk_pct": cfg.risk_pct or st["risk_pct"],
        "max_pos": cfg.max_pos or st["max_pos"],
        "live": data["live"],  # active first, then most checks passed, strongest score
    }


def enter_new(cfg: Config, bn: Binance, state: dict, positions: dict, now: float) -> None:
    closed = candle_closed(now)
    eng = engine(cfg)
    rows = [r for r in eng["live"] if r["symbol"] in eng["symbols"] and f"{r['symbol']}:{closed}" not in state["seen"]]
    if not rows:
        return
    if any(r.get("as_of") is not None and r["as_of"] != closed for r in rows):
        return  # the dashboard still serves the previous candle; try again next tick
    rows.sort(key=lambda r: -r.get("score", 0))
    filters = symbol_filters(bn)
    wallet, available = balances(bn)
    for r in rows:
        symbol = r["symbol"]
        state["seen"].append(f"{symbol}:{closed}")
        if not r.get("active"):
            continue
        if symbol in positions or symbol in state["trades"]:
            telegram(cfg, f"⏭️ {coin(symbol)}: sinyal {r['side']} dilewati, koin ini sudah punya posisi/order.")
            continue
        busy = len(set(positions) | set(state["trades"]))
        if busy >= eng["max_pos"]:
            telegram(cfg, f"⏭️ {coin(symbol)}: sinyal {r['side']} dilewati, slot penuh ({busy}/{eng['max_pos']}).")
            continue
        sig = {"side": r["side"], "entry": r["entry"], "stop_loss": r["stop_loss"],
               "rr": round(abs(r["take_profit"] - r["entry"]) / abs(r["entry"] - r["stop_loss"]), 2)}
        p = plan(sig, wallet, eng["risk_pct"], filters[symbol])
        if "skip" in p:
            telegram(cfg, f"⏭️ {coin(symbol)}: sinyal {r['side']} dilewati, {p['skip']}.")
            continue
        if p["margin"] > available * 0.95:
            telegram(cfg, f"⏭️ {coin(symbol)}: sinyal {r['side']} dilewati, margin tidak cukup "
                          f"(butuh ${p['margin']:.2f}, tersedia ${available:.2f}).")
            continue
        mark = mark_price(bn, symbol)
        if (mark <= p["sl"]) if p["side"] == "BUY" else (mark >= p["sl"]):
            telegram(cfg, f"⏭️ {coin(symbol)}: sinyal {r['side']} dilewati, harga sudah melewati stop loss.")
            continue
        tr = {"symbol": symbol, "dir": "LONG" if p["side"] == "BUY" else "SHORT", "close_side": p["close_side"],
              "entry": p["entry"], "sl": p["sl"], "tp": p["tp"], "qty": p["qty"], "signal_t": closed + STEP,
              "expires": closed + STEP + LIMIT_BARS * STEP, "cid": f"v3{symbol[:8]}{closed + STEP}"}
        if cfg.dry_run:
            state["trades"][symbol] = {**tr, "stage": "sim"}
            available -= p["margin"]
            telegram(cfg, f"🧪 SIMULASI (tidak ada order): {fmt(p, symbol)}. Berlaku 16 jam.")
            continue
        try:
            p["lev"] = set_leverage(bn, symbol, p["lev"])
            order = {"symbol": symbol, "side": p["side"], "type": "LIMIT", "quantity": p["qty"], "price": p["entry"],
                     "newClientOrderId": tr["cid"]}
            try:
                bn.call("POST", "/fapi/v1/order", {**order, "timeInForce": "GTD", "goodTillDate": tr["expires"] * 1000})
            except BinanceError:  # GTD not accepted here: plain GTC, the executor cancels it itself
                bn.call("POST", "/fapi/v1/order", {**order, "timeInForce": "GTC"})
        except BinanceError as exc:
            telegram(cfg, f"❌ {coin(symbol)}: order gagal ({exc}).")
            continue
        state["trades"][symbol] = {**tr, "stage": "pending"}
        available -= p["margin"]
        save_state(cfg, state)
        telegram(cfg, f"📥 Order limit terpasang: {fmt(p, symbol)}. Batal otomatis kalau tidak terisi dalam 16 jam.")


def run(cfg: Config, now: float | None = None) -> None:
    now = time.time() if now is None else now
    bn = Binance(cfg)
    state = load_state(cfg)
    positions = open_positions(bn)
    try:
        manage(cfg, bn, state, positions, now)
        if in_entry_window(now):
            enter_new(cfg, bn, state, open_positions(bn) if not cfg.dry_run else positions, now)
    finally:
        save_state(cfg, state)


def loop(cfg: Config) -> None:
    telegram(cfg, f"▶️ Eksekutor v3 jalan ({'testnet' if cfg.testnet else 'akun asli'}, "
                  f"{'SIMULASI' if cfg.dry_run else 'ORDER SUNGGUHAN'}).")
    last_error = ""
    while True:
        try:
            run(cfg)
            last_error = ""
        except Exception as exc:  # keep running; report each new kind of error once
            traceback.print_exc()
            text = f"{type(exc).__name__}: {exc}"
            if text != last_error:
                telegram(cfg, f"❌ Eksekutor error: {text[:300]}")
                last_error = text
        time.sleep(TICK_SECONDS - time.time() % TICK_SECONDS)


def status(cfg: Config) -> None:
    state = load_state(cfg)
    print(f"Mode: {'testnet' if cfg.testnet else 'akun asli'}, {'SIMULASI' if cfg.dry_run else 'ORDER SUNGGUHAN'}")
    if not state["trades"]:
        print("Tidak ada order/posisi yang sedang diurus.")
    for s, tr in state["trades"].items():
        until = tr.get("exit_at") if tr["stage"] == "open" else tr["expires"]
        print(f"{s}: {tr['stage']} {tr['dir']} entry {tr['entry']} SL {tr['sl']} TP {tr['tp']} "
              f"sampai {time.strftime('%d/%m %H:%M', time.gmtime(until + 7 * 3600))} WIB")


def check(cfg: Config) -> None:
    bn = Binance(cfg)
    print(f"Server: {cfg.base} ({'testnet' if cfg.testnet else 'akun asli'}), mode: {'SIMULASI' if cfg.dry_run else 'ORDER SUNGGUHAN'}")
    print("Selisih jam VPS vs Binance (ms):", bn.call("GET", "/fapi/v1/time", signed=False)["serverTime"] - int(time.time() * 1000))
    wallet, available = balances(bn)
    print(f"Saldo USDT futures: {wallet:.2f} (tersedia {available:.2f})")
    dual = bn.call("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"]
    print("Mode posisi:", "HEDGE (ubah ke One-way di Binance!)" if dual else "One-way (benar)")
    if not cfg.testnet:
        r = bn.call("GET", "/sapi/v1/account/apiRestrictions", base="https://api.binance.com")
        print("Withdraw:", "NYALA, matikan sekarang di Binance!" if r.get("enableWithdrawals") else "mati (benar)")
        print("Batas IP:", "ya (benar)" if r.get("ipRestrict") else "TIDAK, kunci ke IP VPS di Binance!")
        print("Futures:", "nyala" if r.get("enableFutures") else "MATI, centang Enable Futures")
    print("Posisi terbuka:", ", ".join(open_positions(bn)) or "tidak ada")
    eng = engine(cfg)
    print(f"Engine: {len(eng['symbols'])} koin, risiko {eng['risk_pct']}%, maks {eng['max_pos']} posisi")
    print("Sinyal aktif sekarang:", ", ".join(r["symbol"] for r in eng["live"] if r.get("active")) or "tidak ada")
    telegram(cfg, f"🔌 Eksekutor tersambung ke Binance ({'testnet' if cfg.testnet else 'akun asli'}, "
                  f"{'simulasi' if cfg.dry_run else 'order sungguhan'}), saldo {wallet:.2f} USDT.")


if __name__ == "__main__":
    config = Config(load_env())
    if not (config.key and config.secret):
        sys.exit("Isi BINANCE_API_KEY dan BINANCE_API_SECRET di file .env dulu.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"check": check, "run": run, "loop": loop, "status": status}.get(cmd, run)(config)
