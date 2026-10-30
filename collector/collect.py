"""Collect Binance futures positioning data that the free API does not keep for long.

Every 5 minutes: funding/mark price, open interest, global long/short account
ratio and taker buy/sell ratio for each symbol. Continuously: every forced
liquidation from the public websocket. Everything is appended to daily CSV files
under data/ so it can be backtested later.

Public endpoints only, no API key. Run:  python collect.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,LTCUSDT").split(",")
DATA = Path(os.environ.get("DATA_DIR", Path(__file__).with_name("data")))
FAPI = "https://fapi.binance.com"
POLL_SECONDS = 300


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def append(kind: str, row: dict) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = DATA / kind / f"{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def get(path: str, **params) -> object:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    with urllib.request.urlopen(f"{FAPI}{path}?{qs}", timeout=15) as r:
        return json.loads(r.read())


def poll_once() -> None:
    now = int(time.time())
    premium = {p["symbol"]: p for p in get("/fapi/v1/premiumIndex")}
    for s in SYMBOLS:
        try:
            p = premium.get(s, {})
            oi = get("/fapi/v1/openInterest", symbol=s)
            ls = get("/futures/data/globalLongShortAccountRatio", symbol=s, period="5m", limit=1)
            tk = get("/futures/data/takerlongshortRatio", symbol=s, period="5m", limit=1)
            append("positioning", {
                "ts": now, "symbol": s,
                "mark": p.get("markPrice"), "funding": p.get("lastFundingRate"),
                "open_interest": oi.get("openInterest"),
                "ls_ratio": ls[0]["longShortRatio"] if ls else "",
                "taker_ratio": tk[0]["buySellRatio"] if tk else "",
            })
        except Exception as exc:  # keep collecting the other symbols
            log(f"{s}: {exc}")


async def poller() -> None:
    while True:
        started = time.time()
        try:
            await asyncio.to_thread(poll_once)
            log(f"positioning saved for {len(SYMBOLS)} symbols")
        except Exception as exc:
            log(f"poll failed: {exc}")
        await asyncio.sleep(max(5, POLL_SECONDS - (time.time() - started)))


async def liquidations() -> None:
    import websockets  # pip install websockets

    wanted = set(SYMBOLS)
    while True:
        try:
            async with websockets.connect("wss://fstream.binance.com/ws/!forceOrder@arr", ping_interval=60) as ws:
                log("liquidation stream connected")
                async for msg in ws:
                    o = json.loads(msg)["o"]
                    if o["s"] in wanted:
                        append("liquidations", {
                            "ts": o["T"] // 1000, "symbol": o["s"], "side": o["S"],
                            "price": o["ap"], "qty": o["z"],
                            "usd": round(float(o["ap"]) * float(o["z"]), 2),
                        })
        except Exception as exc:
            log(f"liquidation stream dropped: {exc}; reconnecting in 10s")
            await asyncio.sleep(10)


async def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    log(f"saving to {DATA.resolve()}")
    await asyncio.gather(poller(), liquidations())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
