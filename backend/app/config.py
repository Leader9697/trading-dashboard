"""Runtime settings, read from environment variables (see backend/.env.example)."""

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Base URL of a running daily_stock_analysis server, e.g. http://localhost:8000
    dsa_url: str = field(default_factory=lambda: _env("DSA_URL").rstrip("/"))
    # Base URL of the dexter bridge (Tahap 3), e.g. http://localhost:8787
    dexter_url: str = field(default_factory=lambda: _env("DEXTER_URL").rstrip("/"))
    # Comma separated tickers shown in the watchlist bar.
    watchlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().upper()
            for s in _env("WATCHLIST", "BBCA.JK,BBRI.JK,TLKM.JK,AAPL,NVDA").split(",")
            if s.strip()
        )
    )
    # demo | yfinance | dsa. Empty = dsa when DSA_URL is set, otherwise yfinance.
    data_mode: str = field(default_factory=lambda: _env("DATA_MODE").lower())

    @property
    def candle_source(self) -> str:
        if self.data_mode in {"demo", "yfinance", "dsa"}:
            return self.data_mode
        return "dsa" if self.dsa_url else "yfinance"

    @property
    def decision_source(self) -> str:
        return "dsa" if self.dsa_url and self.data_mode != "demo" else "rules"


def get_settings() -> Settings:
    return Settings()
