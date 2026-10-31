export type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number };
export type LinePoint = { time: number; value: number };
export type Mode = 'swing' | 'scalp';

export type CandleResponse = {
  symbol: string;
  interval: string;
  candles: Candle[];
  indicators: { ema20: LinePoint[]; ema50: LinePoint[]; ema200: LinePoint[] };
};

export type Levels = {
  entry: number;
  entry_zone: [number, number];
  stop_loss: number;
  tp1: number;
  tp2: number;
  rr_tp1: number;
  rr_tp2: number;
  sl_atr: number;
  hurdle: number | null;
  invalidation: string;
};

export type Setup = {
  symbol: string;
  mode: Mode;
  timeframes: { trend: string; entry: string };
  price: number;
  last_price?: number;
  as_of: number;
  direction: 'long' | 'short' | 'wait';
  plan_side: 'long' | 'short';
  score: number;
  rating: string;
  levels: Levels;
  risk_pct: number;
  checklist: { label: string; ok: boolean }[];
  bull_points: string[];
  bear_points: string[];
  derivatives: { funding_pct: number | null; oi_change_24h_pct: number | null; long_short_ratio: number | null };
  fear_greed: { value: number; label: string } | null;
  signal: Signal;
};

export type Signal = {
  active: boolean;
  side: 'long' | 'short';
  order?: 'limit' | 'market';
  valid_bars?: number;
  valid_hours?: number;
  entry: number;
  stop_loss: number;
  take_profit: number;
  rr: number;
  max_hold_hours?: number;
  sl_pct: number | null;
  min_notional: number;
  checks: { label: string; ok: boolean }[];
  btc_regime: number;
  auto: boolean;
  rules: string;
  stage: 'research' | 'forward_test' | 'validated' | 'rejected';
  note: string;
};

export type SnapshotCoin = {
  s: string;
  px?: number;
  dir?: 'long' | 'short' | 'wait';
  score?: number;
  plan?: ['long' | 'short', [number, number], number, number, number];
  sig?: [boolean, 'long' | 'short', number, number, number, number];
  ok?: number;
  error?: string;
};

export type Snapshot = { t: number; mode: Mode; coins: SnapshotCoin[] };

export type DebateCoin = {
  verdict: string;
  confidence?: string;
  bull: string;
  bear: string;
  manager: string;
  plan_note?: string;
};

export type Debate =
  | { available: false }
  | {
      available: true;
      updated_at: string;
      mode?: Mode;
      market_note?: string;
      risk_note?: string;
      coins: Record<string, DebateCoin>;
    };

export type Config = {
  watchlist: string[];
  data_mode: string;
  modes: Record<Mode, { trend: string; entry: string }>;
  debate_enabled: boolean;
};

export type Stats = {
  trades: number;
  winrate: number | null;
  expectancy_r: number | null;
  total_r: number;
  profit_factor: number | null;
  max_drawdown_r: number;
};

export type Rules = 'v1' | 'v2';

export type Fold = { n: number; wr: number | null; exp: number | null; pf: number | null };

export type Backtest = {
  symbol: string;
  mode: Mode;
  rules: Rules;
  folds: Fold[];
  period: { from: number | null; to: number; bars: number };
  signals: number;
  missed: number;
  exit_tp1?: Stats;
  exit_split: Stats;
  by_quality?: { checks_4plus: Stats; checks_below_4: Stats };
  equity: LinePoint[];
  recent?: { time: number; side: 'long' | 'short'; checks: number; entry: number; stop_loss: number; tp1: number; tp2: number; status: string; r_split: number }[];
};

export type PastTrade = {
  signal: number; fill: number; end: number; side: 'long' | 'short';
  entry: number; stop_loss: number; take_profit: number;
  status: 'tp' | 'stop' | 'timeout'; r: number; sl_pct: number; below_min: boolean;
};

export type PastTrades =
  | { symbol: string; available: false }
  | { symbol: string; available: true; generated: number; stats: Stats; below_min: number; trades: PastTrade[] };

export type JournalResult = {
  ts: number; symbol: string; side: 'long' | 'short'; entry: number; stop_loss: number; tp1: number; tp2: number;
  status: string; r: number | null;
};

export type Journal = {
  enabled: boolean;
  results: JournalResult[];
  summary: Stats & { missed: number };
};

async function request<T>(path: string): Promise<T> {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
  return body as T;
}

export const api = {
  config: () => request<Config>('/api/config'),
  candles: (symbol: string, interval: string, end?: number) =>
    request<CandleResponse>(`/api/candles/${encodeURIComponent(symbol)}?interval=${interval}&limit=${end ? 1000 : 300}${end ? `&end=${end}` : ''}`),
  setup: (symbol: string, mode: Mode) => request<Setup>(`/api/setup/${encodeURIComponent(symbol)}?mode=${mode}`),
  snapshot: (mode: Mode) => request<Snapshot>(`/api/snapshot?mode=${mode}`),
  debate: () => request<Debate>('/api/debate'),
  backtest: (symbol: string, mode: Mode, rules: Rules) =>
    request<Backtest>(`/api/backtest/${encodeURIComponent(symbol)}?mode=${mode}&rules=${rules}`),
  journal: () => request<Journal>('/api/journal'),
  pastTrades: (symbol: string) => request<PastTrades>(`/api/backtest/${encodeURIComponent(symbol)}/trades`),
};

export function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–';
  const digits = n >= 100 ? 2 : n >= 1 ? 4 : 6;
  return n.toLocaleString('en-US', { minimumFractionDigits: Math.min(digits, 2), maximumFractionDigits: digits });
}
