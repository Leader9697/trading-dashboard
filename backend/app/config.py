"""Runtime settings, read from environment variables (see backend/.env.example)."""

import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Comma separated Binance USDT pairs shown in the watchlist bar.
    watchlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().upper()
            for s in _env("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
            if s.strip()
        )
    )
    # binance (live public market data) | demo (synthetic, offline)
    data_mode: str = field(default_factory=lambda: _env("DATA_MODE", "binance").lower())
    # Connection string Vercel sets when an Edge Config store is linked:
    # https://edge-config.vercel.com/<ecfg_id>?token=<read token>
    edge_config: str = field(default_factory=lambda: _env("EDGE_CONFIG"))
    # Telegram signal alerts: bot token from @BotFather. The chat id is found from the
    # latest message sent to the bot when TELEGRAM_CHAT_ID is not set.
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    # Account size and risk used for the position size in alerts.
    alert_equity: float = field(default_factory=lambda: float(_env("ALERT_EQUITY", "55")))
    alert_risk_pct: float = field(default_factory=lambda: float(_env("ALERT_RISK_PCT", "1")))

    @property
    def edge_config_item_url(self) -> tuple[str, str] | None:
        """Return (base item URL, token) for reading items, or None when unset."""
        if not self.edge_config:
            return None
        parsed = urlparse(self.edge_config)
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not token:
            return None
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}/item", token


def get_settings() -> Settings:
    return Settings()
