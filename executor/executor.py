"""Places the dashboard's v3 signals as Binance USDT-M futures orders.

Runs on a small always-on server (cron every 5 minutes). Shortly after each 4h
close it reads the signal for every coin from the dashboard; for an active one it
opens a market position sized to RISK_PCT of the wallet, then a stop loss and a
take profit that close the whole position. Between closes it only cleans up
orders left over from positions that have closed.

Standard library only. Settings come from a .env file next to this script.

  python3 executor.py check   # connection, permissions, balance; places nothing
  python3 executor.py run     # what cron calls

DRY_RUN=1 (the default) logs and reports what it would do without ordering.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEP = 4 * 3600          # 4h candles
ENTRY_WINDOW = 30 * 60   # only enter within 30 minutes of a close
WIB = 7 * 3600


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
        self.symbols = [s.strip().upper() for s in env.get("SYMBOLS", "ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",") if s.strip()]
        self.risk_pct = float(env.get("RISK_PCT", "1"))
        self.max_losses_per_day = int(env.get("MAX_LOSSES_PER_DAY", "3"))
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

    def call(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.cfg.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.cfg.base}{path}"
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

def floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(value / step + 1e-9) * step, decimals)


def round_tick(value: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(round(value / tick) * tick, decimals)


def plan(signal: dict, price: float, equity: float, risk_pct: float, filters: dict) -> dict:
    """Quantity, leverage and protective prices for a signal filled near `price`.
    Returns {"skip": reason} when the trade should not be taken."""
    long = signal["side"] == "long"
    sl = signal["stop_loss"]
    risk_per_unit = (price - sl) if long else (sl - price)
    planned = abs(signal["entry"] - sl)
    if risk_per_unit <= 0:
        return {"skip": "harga sudah melewati stop loss"}
    if risk_per_unit > 1.5 * planned:
        return {"skip": "harga sudah jauh dari entry sinyal"}
    risk_usd = equity * risk_pct / 100
    qty = floor_step(risk_usd / risk_per_unit, filters["step"])
    notional = qty * price
    if qty <= 0 or notional < filters["min_notional"]:
        need = filters["min_notional"] * risk_per_unit / price / equity * 100
        return {"skip": f"posisi di bawah minimal order {filters['min_notional']:g} USDT (butuh risiko {need:.1f}%)"}
    sl_pct = 100 * risk_per_unit / price
    max_lev = max(1, math.floor(100 / (2 * sl_pct)))
    lev = min(max_lev, max(1, math.ceil(notional / (0.3 * equity))))
    if notional / lev > equity * 0.9:
        return {"skip": "margin tidak cukup"}
    tp = price + signal["rr"] * risk_per_unit if long else price - signal["rr"] * risk_per_unit
    return {
        "side": "BUY" if long else "SELL",
        "close_side": "SELL" if long else "BUY",
        "qty": qty, "lev": lev, "notional": notional, "risk_usd": qty * risk_per_unit,
        "sl": round_tick(sl, filters["tick"]), "tp": round_tick(tp, filters["tick"]),
    }


def in_entry_window(now: float) -> int | None:
    """The open time of the 4h candle that just closed, if we are inside the entry window."""
    closed = int(now // STEP * STEP)
    return closed if now - closed <= ENTRY_WINDOW else None


# ---- I/O ------------------------------------------------------------------------

def http_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def telegram(cfg: Config, text: str) -> None:
    print(text)
    if not (cfg.tg_token and cfg.tg_chat):
        return
    body = urllib.parse.urlencode({"chat_id": cfg.tg_chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage", data=body, timeout=15)
    except urllib.error.URLError as exc:
        print(f"telegram gagal: {exc}", file=sys.stderr)


def load_state(cfg: Config) -> dict:
    try:
        return json.loads(cfg.state_file.read_text())
    except (OSError, ValueError):
        return {"entered": []}


def save_state(cfg: Config, state: dict) -> None:
    state["entered"] = state["entered"][-200:]
    cfg.state_file.write_text(json.dumps(state))


def symbol_filters(bn: Binance) -> dict:
    out = {}
    for s in bn.call("GET", "/fapi/v1/exchangeInfo", signed=False)["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        out[s["symbol"]] = {
            "step": float(f["MARKET_LOT_SIZE"]["stepSize"]) if "MARKET_LOT_SIZE" in f else float(f["LOT_SIZE"]["stepSize"]),
            "tick": float(f["PRICE_FILTER"]["tickSize"]),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
        }
    return out


def wallet_usdt(bn: Binance) -> float:
    for b in bn.call("GET", "/fapi/v2/balance"):
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0.0


def open_positions(bn: Binance) -> dict:
    return {p["symbol"]: float(p["positionAmt"]) for p in bn.call("GET", "/fapi/v2/positionRisk") if float(p["positionAmt"]) != 0}


def losses_today(bn: Binance) -> int:
    now = time.time()
    day_start = int(((now + WIB) // 86400 * 86400 - WIB) * 1000)
    rows = bn.call("GET", "/fapi/v1/income", {"incomeType": "REALIZED_PNL", "startTime": day_start, "limit": 1000})
    return sum(1 for r in rows if float(r["income"]) < 0)


def conditional(bn: Binance, symbol: str, side: str, kind: str, price: float):
    """Stop loss / take profit that closes the whole position. Binance moved these
    order types to the algo endpoint in late 2025, so fall back to it on -4120."""
    try:
        return bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": side, "type": kind, "stopPrice": price,
                                                  "closePosition": "true", "workingType": "MARK_PRICE"})
    except BinanceError as exc:
        if exc.code != -4120:
            raise
    return bn.call("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": kind,
                                                  "triggerPrice": price, "closePosition": "true", "workingType": "MARK_PRICE"})


def cancel_leftovers(bn: Binance, symbol: str) -> None:
    for method, path in (("DELETE", "/fapi/v1/allOpenOrders"), ("DELETE", "/fapi/v1/algoOpenOrders")):
        try:
            bn.call(method, path, {"symbol": symbol})
        except BinanceError:
            pass


def enter(cfg: Config, bn: Binance, symbol: str, sig: dict, filters: dict, equity: float) -> str:
    price = float(bn.call("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)["markPrice"])
    p = plan(sig, price, equity, cfg.risk_pct, filters)
    coin = symbol.removesuffix("USDT")
    if "skip" in p:
        return f"⏭️ {coin}: sinyal {sig['side']} dilewati, {p['skip']}."
    summary = (f"{'LONG' if p['side'] == 'BUY' else 'SHORT'} {coin} {p['qty']:g} @ ~{price:g}, "
               f"SL {p['sl']:g}, TP {p['tp']:g}, leverage {p['lev']}x isolated, risiko ${p['risk_usd']:.2f}")
    if cfg.dry_run:
        return f"🧪 SIMULASI (tidak ada order): {summary}"
    try:
        bn.call("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except BinanceError as exc:
        if exc.code != -4046:  # already isolated
            raise
    bn.call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": p["lev"]})
    bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": p["side"], "type": "MARKET", "quantity": p["qty"]})
    try:
        conditional(bn, symbol, p["close_side"], "STOP_MARKET", p["sl"])
    except BinanceError as exc:
        # Never leave a position without a stop
        bn.call("POST", "/fapi/v1/order", {"symbol": symbol, "side": p["close_side"], "type": "MARKET",
                                           "quantity": p["qty"], "reduceOnly": "true"})
        return f"⚠️ {coin}: stop loss gagal dipasang ({exc}), posisi langsung ditutup."
    try:
        conditional(bn, symbol, p["close_side"], "TAKE_PROFIT_MARKET", p["tp"])
    except BinanceError as exc:
        return f"✅ {summary}\n⚠️ TP gagal dipasang ({exc}), pasang manual di {p['tp']:g}."
    return f"✅ Order terpasang: {summary}"


def run(cfg: Config, now: float | None = None) -> None:
    now = time.time() if now is None else now
    bn = Binance(cfg)
    positions = open_positions(bn)
    for symbol in cfg.symbols:
        if symbol not in positions:
            cancel_leftovers(bn, symbol)
    closed = in_entry_window(now)
    if closed is None:
        return
    state = load_state(cfg)
    todo = [s for s in cfg.symbols if f"{s}:{closed}" not in state["entered"] and s not in positions]
    if not todo:
        return
    if losses_today(bn) >= cfg.max_losses_per_day:
        telegram(cfg, f"⛔ Sudah {cfg.max_losses_per_day} kali rugi hari ini, entry baru dihentikan sampai besok.")
        state["entered"] += [f"{s}:{closed}" for s in todo]
        save_state(cfg, state)
        return
    filters = symbol_filters(bn)
    for symbol in todo:
        try:
            sig = http_json(f"{cfg.dashboard}/api/setup/{symbol}")["signal"]
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            print(f"{symbol}: dashboard gagal ({exc})", file=sys.stderr)
            continue
        state["entered"].append(f"{symbol}:{closed}")
        if not sig.get("active"):
            continue
        try:
            msg = enter(cfg, bn, symbol, sig, filters[symbol], wallet_usdt(bn))
        except BinanceError as exc:
            msg = f"❌ {symbol.removesuffix('USDT')}: order gagal ({exc})."
        telegram(cfg, msg)
    save_state(cfg, state)


def check(cfg: Config) -> None:
    bn = Binance(cfg)
    print(f"Server: {cfg.base} ({'testnet' if cfg.testnet else 'akun asli'}), mode: {'SIMULASI' if cfg.dry_run else 'ORDER SUNGGUHAN'}")
    print("Waktu server Binance:", bn.call("GET", "/fapi/v1/time", signed=False)["serverTime"])
    print("Saldo USDT futures:", wallet_usdt(bn))
    dual = bn.call("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"]
    print("Mode posisi:", "HEDGE (ubah ke One-way di Binance!)" if dual else "One-way (benar)")
    print("Posisi terbuka:", open_positions(bn) or "tidak ada")
    print("Rugi hari ini:", losses_today(bn))
    print("Dashboard:", http_json(f"{cfg.dashboard}/api/setup/{cfg.symbols[0]}")["signal"]["checks"][0]["label"])
    telegram(cfg, f"🔌 Eksekutor tersambung ke Binance ({'testnet' if cfg.testnet else 'akun asli'}, "
                  f"{'simulasi' if cfg.dry_run else 'order sungguhan'}).")


if __name__ == "__main__":
    config = Config(load_env())
    if not (config.key and config.secret):
        sys.exit("Isi BINANCE_API_KEY dan BINANCE_API_SECRET di file .env dulu.")
    {"check": check, "run": run}.get(sys.argv[1] if len(sys.argv) > 1 else "run", run)(config)
