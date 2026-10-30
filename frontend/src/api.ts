export type Candle = { time: string; open: number; high: number; low: number; close: number; volume: number | null };
export type LinePoint = { time: string; value: number };
export type Marker = { time: string; action: string; label: string };

export type CandleResponse = {
  symbol: string;
  name: string | null;
  source: string;
  candles: Candle[];
  indicators: { sma20: LinePoint[]; sma50: LinePoint[] };
};

export type Decision = {
  symbol: string;
  source: string;
  action: string;
  action_label: string;
  score: number | null;
  trend: string | null;
  summary: string;
  reasons: string[];
  risks: string[];
  strategy: { entry: string | null; stop_loss: string | null; take_profit: string | null };
  markers: Marker[];
  as_of: string | null;
};

export type Config = {
  watchlist: string[];
  sources: { candles: string; decision: string; research: string | null };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
  return body as T;
}

export const api = {
  config: () => request<Config>('/api/config'),
  candles: (symbol: string) => request<CandleResponse>(`/api/candles/${encodeURIComponent(symbol)}?days=180`),
  decision: (symbol: string) => request<Decision>(`/api/decision/${encodeURIComponent(symbol)}`),
  research: (symbol: string, question: string) =>
    request<{ answer?: string; [k: string]: unknown }>('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, question }),
    }),
};
