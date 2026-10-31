import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts';
import { api, fmt, type Candle, type CandleResponse, type PastTrades, type Signal } from './api';
import { DrawingLayer, type Tool } from './chart/drawings';
import { bollinger, ema, rsi } from './chart/indicators';

const MAX_BARS = 25000; // about 2017 to now on 4h
export const INTERVALS = ['5m', '15m', '30m', '1h', '4h', '1d', '1w'];
const STEP: Record<string, number> = { '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800 };

type IndicatorKey = 'ema20' | 'ema50' | 'ema200' | 'bb' | 'volume' | 'rsi';
type Indicators = Record<IndicatorKey, boolean>;
const INDICATORS: { key: IndicatorKey; label: string; swatch: string }[] = [
  { key: 'ema20', label: 'EMA 20', swatch: 'ema20' },
  { key: 'ema50', label: 'EMA 50', swatch: 'ema50' },
  { key: 'ema200', label: 'EMA 200', swatch: 'ema200' },
  { key: 'bb', label: 'Bollinger 20, 2', swatch: 'bb' },
  { key: 'volume', label: 'Volume', swatch: 'vol' },
  { key: 'rsi', label: 'RSI 14', swatch: 'rsi' },
];
// Market-data-only stream first, main stream as fallback. Both need no API key.
const STREAMS = ['wss://data-stream.binance.vision/ws', 'wss://stream.binance.com:9443/ws'];
const DEFAULT_INDICATORS: Indicators = { ema20: true, ema50: true, ema200: true, bb: false, volume: true, rsi: false };

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function stored<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function remember(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage blocked */ }
}

type Line = ISeriesApi<'Line'>;
type Live = {
  chart: IChartApi;
  candles: ISeriesApi<'Candlestick'>;
  volume: ISeriesApi<'Histogram'>;
  emas: { key: IndicatorKey; n: number; series: Line }[];
  bb: Line[];
  rsi: Line | null;
  layer: DrawingLayer;
  bars: () => Candle[];
  draw: () => void;
};

const TOOLS: { tool: Tool; label: string; key: string; icon: ReactNode }[] = [
  { tool: 'cursor', label: 'Kursor', key: 'Esc', icon: <path d="M5 3l12 7-5.5 1.5L9 17z" /> },
  { tool: 'trend', label: 'Garis tren', key: 'T', icon: <><path d="M4 16L16 4" /><circle cx="4" cy="16" r="1.6" /><circle cx="16" cy="4" r="1.6" /></> },
  { tool: 'hline', label: 'Garis horizontal', key: 'H', icon: <><path d="M2 10h16" /><circle cx="10" cy="10" r="1.6" /></> },
  { tool: 'rect', label: 'Kotak', key: 'R', icon: <rect x="3.5" y="5.5" width="13" height="9" /> },
  { tool: 'fib', label: 'Fibonacci retracement', key: 'F', icon: <path d="M3 4h14M3 8h14M3 12h14M3 16h14M5 16L15 4" /> },
  { tool: 'long', label: 'Posisi long', key: 'L', icon: <><rect x="3.5" y="3.5" width="13" height="6" /><rect x="3.5" y="9.5" width="13" height="4" strokeDasharray="2 2" /><path d="M10 17V12" /></> },
  { tool: 'short', label: 'Posisi short', key: 'S', icon: <><rect x="3.5" y="6.5" width="13" height="4" strokeDasharray="2 2" /><rect x="3.5" y="10.5" width="13" height="6" /><path d="M10 3v5" /></> },
  { tool: 'measure', label: 'Ukur harga dan waktu', key: 'M', icon: <path d="M3 13l10-10 4 4-10 10zM6 10l2 2M9 7l2 2M12 4l1.5 1.5" /> },
];

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

type Props = {
  data: CandleResponse;
  signal: Signal | null;
  /** Stream new trades into the last candle (off for the offline demo feed). */
  streaming: boolean;
  interval: string;
  onInterval: (interval: string) => void;
};

export function PriceChart({ data, signal, streaming, interval, onInterval }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const live = useRef<Live | null>(null);
  const [tool, setTool] = useState<Tool>('cursor');
  const [selected, setSelected] = useState<string | null>(null);
  const [count, setCount] = useState(0);
  const [hidden, setHidden] = useState(false);
  const [magnet, setMagnet] = useState(() => stored('td:chart', { magnet: false }).magnet);
  const [showPast, setShowPast] = useState(() => stored('td:past', { on: false }).on);
  const [past, setPast] = useState<PastTrades | null>(null);
  const [ind, setInd] = useState<Indicators>(() => stored('td:indicators', DEFAULT_INDICATORS));
  const [menu, setMenu] = useState(false);
  const [max, setMax] = useState(false);
  const [hover, setHover] = useState<CandlestickData | null>(null);
  const [last, setLast] = useState<Candle | null>(null);

  // Chart, series and drawing layer: rebuilt when the coin or timeframe changes.
  useEffect(() => {
    if (!host.current) return;
    const up = css('--up');
    const down = css('--down');
    const chart: IChartApi = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: css('--muted'),
        fontFamily: css('--font'),
        // Keeps the TradingView attribution link required by the Apache-2.0 NOTICE.
        attributionLogo: true,
        panes: { separatorColor: css('--border') },
      },
      grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, rightOffset: 8 },
      crosshair: { mode: CrosshairMode.Normal },
    });

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: up,
      downColor: down,
      wickUpColor: up,
      wickDownColor: down,
      borderVisible: false,
    });
    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    const quiet = { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false } as const;
    const emas = ([['ema20', 20], ['ema50', 50], ['ema200', 200]] as const).map(([key, n]) => ({
      key, n, series: chart.addSeries(LineSeries, { ...quiet, color: css(`--${key}`) }),
    }));
    const bb = [0, 1, 2].map((i) => chart.addSeries(LineSeries, {
      ...quiet, color: css('--bb'), lineStyle: i === 0 ? LineStyle.Dashed : LineStyle.Solid,
    }));
    const upSoft = css('--up-soft'), downSoft = css('--down-soft');

    let bars: Candle[] = data.candles;
    const line = (values: (number | null)[]) =>
      values.flatMap((v, i) => (v === null ? [] : [{ time: bars[i].time as Time, value: v }]));
    const state: Live = {
      chart, candles, volume, emas, bb, rsi: null, bars: () => bars,
      layer: null as unknown as DrawingLayer,
      draw: () => {
        candles.setData(bars.map((c) => ({ ...c, time: c.time as Time })));
        volume.setData(bars.map((c) => ({ time: c.time as Time, value: c.volume, color: c.close >= c.open ? upSoft : downSoft })));
        const closes = bars.map((c) => c.close);
        for (const { n, series } of emas) series.setData(line(ema(closes, n)));
        const bands = bollinger(closes);
        [bands.mid, bands.upper, bands.lower].forEach((v, i) => bb[i].setData(line(v)));
        state.rsi?.setData(line(rsi(closes)));
      },
    };
    state.layer = new DrawingLayer({
      chart, series: candles, host: host.current, bars: () => bars, step: STEP[data.interval] ?? 14400, symbol: data.symbol,
      onTool: setTool,
      onSelect: (id) => { setSelected(id); setCount(state.layer?.drawings.length ?? 0); },
    });
    candles.attachPrimitive(state.layer);
    live.current = state;
    setCount(state.layer.drawings.length);
    state.draw();

    // Scrolling near the left edge loads the previous 1000 bars, back to the start of Binance history.
    let busy = false, done = false, alive = true;
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || range.from > 30 || busy || done || bars.length >= MAX_BARS) return;
      busy = true;
      api.candles(data.symbol, data.interval, bars[0].time).then((older) => {
        if (!alive) return;
        const add = older.candles.filter((c) => c.time < bars[0].time);
        if (add.length === 0) { done = true; return; }
        const view = chart.timeScale().getVisibleLogicalRange();
        bars = [...add, ...bars];
        state.draw();
        if (view) chart.timeScale().setVisibleLogicalRange({ from: view.from + add.length, to: view.to + add.length });
      }, () => { done = true; }).finally(() => { busy = false; });
    });

    chart.subscribeCrosshairMove((p) => {
      setHover((p.time && (p.seriesData.get(candles) as CandlestickData | undefined)) || null);
    });

    // Live candle: each kline message replaces the forming bar or appends a new one.
    let ws: WebSocket | null = null, retry: ReturnType<typeof setTimeout> | undefined, attempt = 0;
    const lastPoint = (values: (number | null)[]) => {
      const v = values[values.length - 1];
      return v === null || v === undefined ? null : { time: bars[bars.length - 1].time as Time, value: v };
    };
    const tick = (c: Candle) => {
      const prev = bars[bars.length - 1];
      if (!prev || c.time < prev.time) return;
      bars = c.time === prev.time ? [...bars.slice(0, -1), c] : [...bars, c];
      candles.update({ ...c, time: c.time as Time });
      volume.update({ time: c.time as Time, value: c.volume, color: c.close >= c.open ? upSoft : downSoft });
      const closes = bars.map((b) => b.close);
      const put = (series: Line | null, values: (number | null)[]) => { const p = lastPoint(values); if (series && p) series.update(p); };
      for (const { n, series } of emas) put(series, ema(closes, n));
      const bands = bollinger(closes);
      [bands.mid, bands.upper, bands.lower].forEach((v, i) => put(bb[i], v));
      put(state.rsi, rsi(closes));
      setLast(c);
    };
    const connect = () => {
      ws = new WebSocket(`${STREAMS[attempt % STREAMS.length]}/${data.symbol.toLowerCase()}@kline_${data.interval}`);
      ws.onmessage = (m) => {
        const k = JSON.parse(m.data)?.k;
        if (k) tick({ time: k.t / 1000, open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v });
      };
      ws.onopen = () => { attempt = 0; };
      ws.onclose = () => {
        if (!alive) return;
        attempt += 1;
        retry = setTimeout(connect, Math.min(30000, 2000 * attempt));
      };
    };
    if (streaming) connect();

    const shown = Math.min(bars.length, 160);
    chart.timeScale().setVisibleLogicalRange({ from: bars.length - shown, to: bars.length + 8 });
    return () => {
      alive = false;
      clearTimeout(retry);
      if (ws) { ws.onclose = null; ws.close(); }
      live.current = null;
      state.layer.destroy();
      chart.remove();
    };
  }, [data, streaming]);

  // Entry, SL and TP of the current signal.
  useEffect(() => {
    const s = live.current;
    if (!s || !signal) return;
    const marks = [
      { price: signal.entry, color: css('--accent'), title: 'Entry', style: LineStyle.Dotted },
      { price: signal.stop_loss, color: css('--down'), title: 'SL', style: LineStyle.Solid },
      { price: signal.take_profit, color: css('--up'), title: 'TP', style: LineStyle.Dashed },
    ];
    const lines: IPriceLine[] = marks.map((m) =>
      s.candles.createPriceLine({ price: m.price, color: m.color, lineWidth: 1, lineStyle: m.style, axisLabelVisible: true, title: m.title }));
    return () => { if (live.current === s) lines.forEach((l) => s.candles.removePriceLine(l)); };
  }, [data, signal]);

  // Past v3 trades as position boxes, fetched once per coin while the toggle is on.
  useEffect(() => {
    remember('td:past', { on: showPast });
    if (!showPast) return;
    let alive = true;
    setPast(null);
    api.pastTrades(data.symbol).then((p) => { if (alive) setPast(p); }, () => { if (alive) setPast({ symbol: data.symbol, available: false }); });
    return () => { alive = false; };
  }, [data.symbol, showPast]);

  useEffect(() => {
    live.current?.layer.setTrades(showPast && past?.available && past.symbol === data.symbol ? past.trades : []);
  }, [data, past, showPast]);

  // Indicator toggles. RSI gets its own pane below the price.
  useEffect(() => {
    const s = live.current;
    if (!s) return;
    for (const e of s.emas) e.series.applyOptions({ visible: ind[e.key] });
    for (const b of s.bb) b.applyOptions({ visible: ind.bb });
    s.volume.applyOptions({ visible: ind.volume });
    if (ind.rsi && !s.rsi) {
      s.rsi = s.chart.addSeries(LineSeries, { color: css('--rsi'), lineWidth: 1, priceLineVisible: false }, 1);
      for (const level of [70, 30]) s.rsi.createPriceLine({ price: level, color: css('--muted'), lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' });
      s.chart.panes()[1]?.setHeight(110);
      s.draw();
    } else if (!ind.rsi && s.rsi) {
      s.chart.removeSeries(s.rsi);
      s.rsi = null;
    }
    remember('td:indicators', ind);
  }, [data, ind]);

  useEffect(() => { live.current?.layer.setTool(tool); }, [data, tool]);
  useEffect(() => { live.current?.layer.setHidden(hidden); }, [data, hidden]);
  useEffect(() => {
    const s = live.current;
    if (!s) return;
    s.layer.magnet = magnet;
    s.chart.applyOptions({ crosshair: { mode: magnet ? CrosshairMode.MagnetOHLC : CrosshairMode.Normal } });
    remember('td:chart', { magnet });
  }, [data, magnet]);

  // Keyboard shortcuts for tools, as listed in each button's tooltip.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.ctrlKey || e.metaKey || e.altKey || (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'))) return;
      const t = TOOLS.find((x) => x.key.toLowerCase() === e.key.toLowerCase() && x.key.length === 1);
      if (t) setTool(t.tool);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    const onChange = () => setMax(document.fullscreenElement === wrap.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleMax = () => {
    const el = wrap.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.requestFullscreen) el.requestFullscreen().catch(() => setMax((m) => !m));
    else setMax((m) => !m); // iPhone Safari: fill the window instead
  };

  const latest = last ?? data.candles[data.candles.length - 1];
  const bar = hover ?? (latest ? { ...latest, time: latest.time as Time } : null);
  const change = bar ? bar.close - bar.open : 0;

  return (
    <div className={`chart-wrap${max ? ' is-max' : ''}`} ref={wrap}>
      <div className="chart-bar">
        <div className="seg" role="group" aria-label="Timeframe">
          {INTERVALS.map((tf) => (
            <button key={tf} type="button" aria-pressed={tf === interval} onClick={() => onInterval(tf)}>{tf}</button>
          ))}
        </div>
        <div className="menu">
          <button type="button" className="tb" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            <Icon><path d="M3 15l4-5 3 3 4-6 3 4" /></Icon>Indikator
          </button>
          {menu && (
            <div className="menu-pop" role="group" aria-label="Indikator">
              {INDICATORS.map((i) => (
                <label key={i.key}>
                  <input type="checkbox" checked={ind[i.key]} onChange={() => setInd({ ...ind, [i.key]: !ind[i.key] })} />
                  <i className={`swatch ${i.swatch}`} />{i.label}
                </label>
              ))}
            </div>
          )}
        </div>
        <button type="button" className="tb" aria-pressed={magnet} onClick={() => setMagnet(!magnet)} title="Magnet: titik gambar menempel ke open, high, low, close">
          <Icon><path d="M5 3v7a5 5 0 0010 0V3M5 6h3M12 6h3" /></Icon>Magnet
        </button>
        <button type="button" className="tb" aria-pressed={showPast} onClick={() => setShowPast(!showPast)} title="Kotak entry, SL dan TP untuk setiap trade v3 di masa lalu">
          <Icon><rect x="3.5" y="4.5" width="13" height="5" /><rect x="3.5" y="9.5" width="13" height="5" strokeDasharray="2 2" /></Icon>Trade lalu
        </button>
        <button type="button" className="tb icon-only" onClick={() => live.current?.chart.timeScale().scrollToRealTime()} title="Ke candle terbaru" aria-label="Ke candle terbaru">
          <Icon><path d="M4 10h11M11 6l4 4-4 4M17 4v12" /></Icon>
        </button>
        <button type="button" className="tb icon-only" onClick={toggleMax} aria-pressed={max} title={max ? 'Keluar layar penuh' : 'Layar penuh'} aria-label={max ? 'Keluar layar penuh' : 'Layar penuh'}>
          <Icon>{max ? <path d="M8 3v5H3M12 3v5h5M8 17v-5H3M12 17v-5h5" /> : <path d="M3 8V3h5M17 8V3h-5M3 12v5h5M17 12v5h-5" />}</Icon>
        </button>
      </div>

      <div className="chart-body">
        <div className="tools" role="toolbar" aria-label="Alat gambar" aria-orientation="vertical">
          {TOOLS.map((t) => (
            <button key={t.tool} type="button" aria-pressed={tool === t.tool} onClick={() => setTool(t.tool)} title={`${t.label} (${t.key})`} aria-label={t.label}>
              <Icon>{t.icon}</Icon>
            </button>
          ))}
          <hr />
          <button type="button" aria-pressed={hidden} onClick={() => setHidden(!hidden)} title={hidden ? 'Tampilkan gambar' : 'Sembunyikan gambar'} aria-label={hidden ? 'Tampilkan gambar' : 'Sembunyikan gambar'}>
            <Icon>{hidden ? <path d="M3 3l14 14M8 5.3A8 8 0 0118 10a9 9 0 01-2 2.6M12 14.6A8 8 0 012 10a9 9 0 013-3.4" /> : <><path d="M2 10s3-6 8-6 8 6 8 6-3 6-8 6-8-6-8-6z" /><circle cx="10" cy="10" r="2.5" /></>}</Icon>
          </button>
          <button type="button" disabled={!selected} onClick={() => live.current?.layer.removeSelected()} title="Hapus gambar terpilih (Delete)" aria-label="Hapus gambar terpilih">
            <Icon><path d="M4 6h12M8 6V4h4v2M6 6l1 11h6l1-11" /></Icon>
          </button>
          <button
            type="button"
            disabled={count === 0}
            onClick={() => { if (window.confirm('Hapus semua gambar di koin ini?')) { live.current?.layer.clear(); setCount(0); } }}
            title="Hapus semua gambar"
            aria-label="Hapus semua gambar"
          >
            <Icon><path d="M4 6h12M6 6l1 11h6l1-11M9 9l2 5M11 9l-2 5" /></Icon>
          </button>
        </div>
        <div className="chart-area">
          {bar && (
            <div className="ohlc num" aria-live="off">
              <span>O <b>{fmt(bar.open)}</b></span>
              <span>H <b>{fmt(bar.high)}</b></span>
              <span>L <b>{fmt(bar.low)}</b></span>
              <span>C <b>{fmt(bar.close)}</b></span>
              <span className={change >= 0 ? 'up' : 'down'}>{change >= 0 ? '+' : '−'}{fmt(Math.abs(change))} ({((change / bar.open) * 100).toFixed(2)}%)</span>
            </div>
          )}
          <div className={`chart${ind.rsi ? ' with-rsi' : ''}`} ref={host} />
        </div>
      </div>
      {showPast && <PastSummary past={past} symbol={data.symbol} />}
    </div>
  );
}

function PastSummary({ past, symbol }: { past: PastTrades | null; symbol: string }) {
  if (!past || past.symbol !== symbol) return <p className="note past-note">Memuat trade lalu…</p>;
  if (!past.available) return <p className="note past-note">Belum ada data trade lalu untuk koin ini. Backtest v3 hanya untuk BTC, ETH, SOL, BNB dan XRP.</p>;
  const s = past.stats;
  const from = past.trades[0] ? new Date(past.trades[0].fill * 1000).toLocaleDateString('id-ID', { month: 'short', year: 'numeric' }) : '';
  const total = `${s.total_r > 0 ? '+' : ''}${s.total_r.toFixed(1)}R`;
  return (
    <p className="note past-note">
      {s.trades} trade v3 sejak {from}: winrate {s.winrate ?? '–'}%, total <span className={s.total_r >= 0 ? 'up' : 'down'}>{total}</span>, max drawdown −{s.max_drawdown_r}R.
      {' '}Hijau = zona TP, merah = zona SL, garis tepi = sisi yang kena; tanpa garis tepi = ditutup karena waktu habis (5 hari). Kotak pudar ({past.below_min}) = posisi di bawah minimal order Binance untuk modal $55, risiko 1%, jadi di akun live akan dilewati.
      {' '}Geser grafik ke kiri untuk melihat trade lebih lama.
    </p>
  );
}
