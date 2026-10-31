import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  Logical,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts';
import { fmt, type Candle, type PastTrade } from '../api';

export type ToolType = 'trend' | 'hline' | 'rect' | 'fib' | 'long' | 'short' | 'measure';
export type Tool = 'cursor' | ToolType;
export type Point = { time: number; price: number };
export type Drawing = { id: string; type: ToolType; points: Point[] };

/** Position tool target, matching the live signal rules (one take profit at 4R). */
export const POSITION_RR = 4;
const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const HIT = 6;
const HANDLE = 7;

type Target = Parameters<IPrimitivePaneRenderer['draw']>[0];
type Px = { x: number; y: number };
type Drag = { id: string; handle: number; start: Point; orig: Point[] };

type Options = {
  chart: IChartApi;
  series: ISeriesApi<'Candlestick'>;
  host: HTMLElement;
  bars: () => Candle[];
  step: number;
  symbol: string;
  onTool: (tool: Tool) => void;
  onSelect: (id: string | null) => void;
};

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function storageKey(symbol: string) {
  return `td:drawings:${symbol}`;
}

function load(symbol: string): Drawing[] {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey(symbol)) ?? '[]');
    return Array.isArray(raw) ? raw.filter((d) => d && Array.isArray(d.points)) : [];
  } catch {
    return [];
  }
}

function distToSegment(p: Px, a: Px, b: Px) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const t = len === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function pct(from: number, to: number) {
  const v = ((to - from) / from) * 100;
  return `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}%`;
}

function duration(seconds: number) {
  const s = Math.abs(seconds);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}h ${h}j`;
  if (h) return `${h}j ${m}m`;
  return `${m}m`;
}

/**
 * All user drawings for one chart, rendered by a single series primitive.
 * Points are stored as (unix time, price) so they survive reloads, timeframe switches and older history loading in.
 */
export class DrawingLayer implements ISeriesPrimitive<Time> {
  drawings: Drawing[];
  tool: Tool = 'cursor';
  selected: string | null = null;
  magnet = false;
  hidden = false;
  /** Past v3 trades, drawn as read-only position boxes under the user's drawings. */
  trades: PastTrade[] = [];

  private o: Options;
  private hover: string | null = null;
  private draft: Drawing | null = null;
  private draftAnchor: Px | null = null;
  private pressing = false;
  private measure: Drawing | null = null;
  private drag: Drag | null = null;
  private requestUpdate: () => void = () => {};
  private colors = { accent: '', up: '', down: '', text: '', muted: '', panel: '', upSoft: '', downSoft: '' };
  private views: IPrimitivePaneView[];

  constructor(o: Options) {
    this.o = o;
    this.drawings = load(o.symbol);
    this.views = [{ zOrder: () => 'top', renderer: () => ({ draw: (t: Target) => this.draw(t) }) }];
    o.host.addEventListener('pointerdown', this.onDown, true);
    o.host.addEventListener('pointermove', this.onMove, true);
    window.addEventListener('pointerup', this.onUp, true);
    window.addEventListener('keydown', this.onKey);
  }

  attached({ requestUpdate }: SeriesAttachedParameter<Time>) {
    this.requestUpdate = requestUpdate;
    this.colors = {
      accent: css('--accent'), up: css('--up'), down: css('--down'), text: css('--text'),
      muted: css('--muted'), panel: css('--panel'), upSoft: css('--up-soft'), downSoft: css('--down-soft'),
    };
  }

  detached() {
    this.requestUpdate = () => {};
  }

  destroy() {
    this.o.host.removeEventListener('pointerdown', this.onDown, true);
    this.o.host.removeEventListener('pointermove', this.onMove, true);
    window.removeEventListener('pointerup', this.onUp, true);
    window.removeEventListener('keydown', this.onKey);
  }

  paneViews() {
    return this.views;
  }

  priceAxisViews(): ISeriesPrimitiveAxisView[] {
    if (this.hidden) return [];
    const lines = [...this.drawings, ...(this.draft ? [this.draft] : [])].filter((d) => d.type === 'hline');
    return lines.map((d) => ({
      coordinate: () => this.y(d.points[0].price) ?? -100,
      text: () => fmt(d.points[0].price),
      textColor: () => '#ffffff',
      backColor: () => this.colors.accent,
    }));
  }

  // ---- public controls ----

  setTool(tool: Tool) {
    this.tool = tool;
    this.draft = null;
    this.draftAnchor = null;
    if (tool !== 'cursor') this.select(null);
    this.lockChart(tool !== 'cursor');
    this.o.host.style.cursor = tool === 'cursor' ? '' : 'crosshair';
    this.requestUpdate();
  }

  setHidden(hidden: boolean) {
    this.hidden = hidden;
    if (hidden) this.select(null);
    this.requestUpdate();
  }

  setTrades(trades: PastTrade[]) {
    this.trades = trades;
    this.requestUpdate();
  }

  removeSelected() {
    if (!this.selected) return;
    this.drawings = this.drawings.filter((d) => d.id !== this.selected);
    this.select(null);
    this.save();
  }

  clear() {
    this.drawings = [];
    this.measure = null;
    this.select(null);
    this.save();
  }

  // ---- coordinates ----

  /** Bar index for a timestamp, fractional between bars and extrapolated past either end. */
  private logical(time: number): number {
    const b = this.o.bars(), n = b.length, step = this.o.step;
    if (!n) return 0;
    if (time <= b[0].time) return (time - b[0].time) / step;
    if (time >= b[n - 1].time) return n - 1 + (time - b[n - 1].time) / step;
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) {
      const m = (lo + hi) >> 1;
      if (b[m].time <= time) lo = m; else hi = m;
    }
    return lo + (time - b[lo].time) / (b[hi].time - b[lo].time);
  }

  private timeAt(logical: number): number {
    const b = this.o.bars(), n = b.length, step = this.o.step;
    if (!n) return 0;
    if (logical <= 0) return b[0].time + logical * step;
    if (logical >= n - 1) return b[n - 1].time + (logical - (n - 1)) * step;
    const i = Math.floor(logical);
    return b[i].time + (logical - i) * (b[i + 1].time - b[i].time);
  }

  private x(time: number) {
    return this.o.chart.timeScale().logicalToCoordinate(this.logical(time) as Logical);
  }

  private y(price: number) {
    return this.o.series.priceToCoordinate(price);
  }

  private px(p: Point): Px | null {
    const x = this.x(p.time), y = this.y(p.price);
    return x === null || y === null ? null : { x, y };
  }

  /** Pointer position inside the main pane, snapped to a bar, and to its high/low/open/close when the magnet is on. */
  private pointAt(e: PointerEvent): { at: Px; point: Point } | null {
    const r = this.o.host.getBoundingClientRect();
    const at = { x: e.clientX - r.left, y: e.clientY - r.top };
    const ts = this.o.chart.timeScale();
    if (at.x < 0 || at.y < 0 || at.x > ts.width() || at.y > this.o.chart.panes()[0].getHeight()) return null;
    const l = ts.coordinateToLogical(at.x);
    let price: number | null = this.o.series.coordinateToPrice(at.y);
    if (l === null || price === null) return null;
    const idx = Math.round(l);
    const bar = this.o.bars()[idx];
    if (this.magnet && bar && this.tool !== 'measure') {
      price = [bar.open, bar.high, bar.low, bar.close].reduce((best, v) =>
        Math.abs((this.y(v) ?? 1e9) - at.y) < Math.abs((this.y(best) ?? 1e9) - at.y) ? v : best);
    }
    return { at, point: { time: this.timeAt(idx), price } };
  }

  // ---- shapes ----

  private shape(type: ToolType, a: Point, b: Point): Point[] {
    if (type === 'hline') return [a];
    if (type !== 'long' && type !== 'short') return [a, b];
    const dir = type === 'long' ? 1 : -1;
    const risk = Math.abs(a.price - b.price) || a.price * 0.01;
    const end = b.time > a.time ? b.time : a.time + 20 * this.o.step;
    return [a, { time: end, price: a.price - dir * risk }, { time: end, price: a.price + dir * risk * POSITION_RR }];
  }

  private hit(at: Px): { id: string; handle: number } | null {
    if (this.hidden) return null;
    for (let i = this.drawings.length - 1; i >= 0; i--) {
      const d = this.drawings[i];
      const pts = d.points.map((p) => this.px(p));
      if (pts.some((p) => p === null)) continue;
      const ps = pts as Px[];
      if (d.type !== 'hline') {
        const h = ps.findIndex((p) => Math.hypot(p.x - at.x, p.y - at.y) <= HANDLE);
        if (h >= 0) return { id: d.id, handle: h };
      }
      if (d.type === 'hline' && Math.abs(ps[0].y - at.y) <= HIT) return { id: d.id, handle: -1 };
      if (d.type === 'trend' && distToSegment(at, ps[0], ps[1]) <= HIT) return { id: d.id, handle: -1 };
      if (d.type !== 'hline' && d.type !== 'trend') {
        const xs = ps.map((p) => p.x), ys = ps.map((p) => p.y);
        if (at.x >= Math.min(...xs) - 4 && at.x <= Math.max(...xs) + 4 && at.y >= Math.min(...ys) - 4 && at.y <= Math.max(...ys) + 4) {
          return { id: d.id, handle: -1 };
        }
      }
    }
    return null;
  }

  // ---- pointer handling ----

  private lockChart(lock: boolean) {
    this.o.chart.applyOptions({ handleScroll: !lock, handleScale: !lock });
  }

  private select(id: string | null) {
    if (this.selected === id) return;
    this.selected = id;
    this.o.onSelect(id);
  }

  private onDown = (e: PointerEvent) => {
    if (e.button !== 0) return;
    const hitPoint = this.pointAt(e);
    if (!hitPoint) return;
    const { at, point } = hitPoint;
    if (this.measure) { this.measure = null; this.requestUpdate(); }

    if (this.tool !== 'cursor') {
      e.preventDefault();
      e.stopPropagation();
      const type = this.tool;
      if (!this.draft) {
        this.draft = { id: crypto.randomUUID?.() ?? String(Date.now()), type, points: this.shape(type, point, point) };
        this.draftAnchor = at;
        this.pressing = true;
        if (type === 'hline') this.commit();
      } else {
        this.draft.points = this.shape(type, this.draft.points[0], point);
        this.commit();
      }
      this.requestUpdate();
      return;
    }

    const h = this.hit(at);
    if (!h) { this.select(null); this.requestUpdate(); return; }
    e.preventDefault();
    e.stopPropagation();
    this.select(h.id);
    const d = this.drawings.find((x) => x.id === h.id)!;
    this.drag = { id: h.id, handle: h.handle, start: point, orig: d.points.map((p) => ({ ...p })) };
    this.lockChart(true);
    this.requestUpdate();
  };

  private onMove = (e: PointerEvent) => {
    const hp = this.pointAt(e);
    if (this.draft && hp) {
      this.draft.points = this.shape(this.draft.type, this.draft.points[0], hp.point);
      this.requestUpdate();
      return;
    }
    if (this.drag && hp) {
      const d = this.drawings.find((x) => x.id === this.drag!.id);
      if (!d) return;
      const { handle, start, orig } = this.drag;
      if (handle >= 0) {
        d.points = orig.map((p) => ({ ...p }));
        d.points[handle] = hp.point;
        if ((d.type === 'long' || d.type === 'short') && handle > 0) d.points[1].time = d.points[2].time = hp.point.time;
      } else {
        const dl = this.logical(hp.point.time) - this.logical(start.time);
        const dp = hp.point.price - start.price;
        d.points = orig.map((p) => ({ time: this.timeAt(this.logical(p.time) + dl), price: p.price + dp }));
      }
      this.requestUpdate();
      return;
    }
    if (this.tool === 'cursor') {
      const h = hp ? this.hit(hp.at) : null;
      const id = h?.id ?? null;
      this.o.host.style.cursor = h ? (h.handle >= 0 ? 'move' : 'pointer') : '';
      if (id !== this.hover) { this.hover = id; this.requestUpdate(); }
    }
  };

  private onUp = (e: PointerEvent) => {
    const pressing = this.pressing;
    this.pressing = false;
    if (this.draft && this.draftAnchor) {
      // Press, drag and release draws in one gesture; a plain click waits for the second click.
      if (!pressing) return;
      const r = this.o.host.getBoundingClientRect();
      const moved = Math.hypot(e.clientX - r.left - this.draftAnchor.x, e.clientY - r.top - this.draftAnchor.y);
      if (moved > HANDLE) this.commit();
      return;
    }
    if (this.drag) {
      this.drag = null;
      this.lockChart(false);
      this.save();
    }
  };

  private onKey = (e: KeyboardEvent) => {
    const el = e.target as HTMLElement | null;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
    if (e.key === 'Escape') {
      this.measure = null;
      if (this.tool !== 'cursor') this.o.onTool('cursor');
      else this.select(null);
      this.requestUpdate();
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && this.selected) {
      e.preventDefault();
      this.removeSelected();
    }
  };

  private commit() {
    const d = this.draft;
    if (!d) return;
    this.draft = null;
    this.draftAnchor = null;
    const [a, b] = d.points;
    const empty = b && b.time === a.time && b.price === a.price && d.type !== 'long' && d.type !== 'short';
    if (empty) {
      // A double click on one spot draws nothing.
    } else if (d.type === 'measure') {
      this.measure = d;
    } else {
      this.drawings = [...this.drawings, d];
      this.select(d.id);
      this.save();
    }
    this.o.onTool('cursor');
    this.requestUpdate();
  }

  private save() {
    try {
      if (this.drawings.length) localStorage.setItem(storageKey(this.o.symbol), JSON.stringify(this.drawings));
      else localStorage.removeItem(storageKey(this.o.symbol));
    } catch {
      // Storage can be blocked (private mode); drawings then last until reload.
    }
    this.requestUpdate();
  }

  // ---- rendering ----

  private draw(target: Target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      ctx.save();
      ctx.font = '12px "IBM Plex Sans", system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      for (const t of this.trades) this.pastTrade(ctx, t, mediaSize.width);
      if (!this.hidden) {
        for (const d of this.drawings) this.paint(ctx, d, mediaSize.width, d.id === this.selected || d.id === this.hover);
      }
      if (this.draft) this.paint(ctx, this.draft, mediaSize.width, true);
      if (this.measure) this.paint(ctx, this.measure, mediaSize.width, false);
      ctx.restore();
    });
  }

  private paint(ctx: CanvasRenderingContext2D, d: Drawing, width: number, active: boolean) {
    const c = this.colors;
    const pts = d.points.map((p) => this.px(p));
    if (pts.some((p) => p === null)) return;
    const ps = pts as Px[];
    ctx.lineWidth = active ? 2 : 1.5;
    ctx.strokeStyle = c.accent;
    ctx.setLineDash([]);

    switch (d.type) {
      case 'trend':
        this.line(ctx, ps[0], ps[1]);
        break;
      case 'hline':
        this.line(ctx, { x: 0, y: ps[0].y }, { x: width, y: ps[0].y });
        break;
      case 'rect': {
        ctx.fillStyle = this.alpha(c.accent, 0.12);
        const [x, y, w, h] = this.box(ps[0], ps[1]);
        ctx.fillRect(x, y, w, h);
        ctx.strokeRect(x, y, w, h);
        break;
      }
      case 'fib':
        this.fib(ctx, d, ps);
        break;
      case 'long':
      case 'short':
        this.position(ctx, d, ps);
        break;
      case 'measure':
        this.ruler(ctx, d, ps);
        break;
    }
    if (active && d.type !== 'hline' && d.type !== 'measure') {
      for (const p of ps) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = c.panel;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
  }

  private fib(ctx: CanvasRenderingContext2D, d: Drawing, ps: Px[]) {
    const [a, b] = d.points;
    const x0 = Math.min(ps[0].x, ps[1].x), x1 = Math.max(ps[0].x, ps[1].x);
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = this.colors.muted;
    this.line(ctx, ps[0], ps[1]);
    ctx.setLineDash([]);
    let prevY: number | null = null;
    FIB_LEVELS.forEach((lvl, i) => {
      const price = b.price + (a.price - b.price) * lvl;
      const y = this.y(price);
      if (y === null) return;
      if (prevY !== null && i % 2 === 0) {
        ctx.fillStyle = this.alpha(this.colors.accent, 0.06);
        ctx.fillRect(x0, Math.min(prevY, y), x1 - x0, Math.abs(y - prevY));
      }
      prevY = y;
      ctx.strokeStyle = lvl === 0.5 || lvl === 0.618 ? this.colors.accent : this.colors.muted;
      this.line(ctx, { x: x0, y }, { x: x1, y });
      ctx.fillStyle = this.colors.muted;
      // Labels sit left of the levels, or just inside them when the drawing starts near the chart's left edge.
      const text = `${lvl} (${fmt(price)})`;
      const outside = ctx.measureText(text).width + 10 < x0;
      ctx.textAlign = outside ? 'right' : 'left';
      ctx.fillText(text, outside ? x0 - 6 : x0 + 4, outside ? y : y - 8);
    });
  }

  private position(ctx: CanvasRenderingContext2D, d: Drawing, ps: Px[]) {
    const c = this.colors;
    const [entry, stop, target] = d.points;
    const x0 = Math.min(ps[0].x, ps[1].x), w = Math.max(Math.abs(ps[1].x - ps[0].x), 40);
    ctx.fillStyle = c.upSoft;
    ctx.fillRect(x0, Math.min(ps[0].y, ps[2].y), w, Math.abs(ps[2].y - ps[0].y));
    ctx.fillStyle = c.downSoft;
    ctx.fillRect(x0, Math.min(ps[0].y, ps[1].y), w, Math.abs(ps[1].y - ps[0].y));
    ctx.strokeStyle = c.text;
    ctx.lineWidth = 1;
    this.line(ctx, { x: x0, y: ps[0].y }, { x: x0 + w, y: ps[0].y });

    const risk = Math.abs(entry.price - stop.price);
    const rr = risk ? Math.abs(target.price - entry.price) / risk : 0;
    const label = (text: string, y: number, bg: string) => {
      ctx.font = '12px "IBM Plex Sans", system-ui, sans-serif';
      const tw = ctx.measureText(text).width + 12;
      const lx = x0 + w / 2 - tw / 2;
      ctx.fillStyle = bg;
      ctx.fillRect(lx, y - 10, tw, 20);
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'left';
      ctx.fillText(text, lx + 6, y);
    };
    const above = ps[2].y < ps[1].y;
    label(`Target ${fmt(target.price)} (${pct(entry.price, target.price)})`, ps[2].y + (above ? -14 : 14), c.up);
    label(`Stop ${fmt(stop.price)} (${pct(entry.price, stop.price)})`, ps[1].y + (above ? 14 : -14), c.down);
    label(`${d.type === 'long' ? 'Long' : 'Short'} ${fmt(entry.price)}, RR ${rr.toFixed(2)}`, ps[0].y, c.text);
  }

  private pastTrade(ctx: CanvasRenderingContext2D, t: PastTrade, width: number) {
    const x0 = this.x(t.fill), x1 = this.x(t.end);
    const yE = this.y(t.entry), yS = this.y(t.stop_loss), yT = this.y(t.take_profit);
    if (x0 === null || x1 === null || yE === null || yS === null || yT === null) return;
    if (x1 < 0 || x0 > width) return;
    const c = this.colors, w = Math.max(x1 - x0, 2);
    ctx.globalAlpha = t.below_min ? 0.55 : 1;
    ctx.fillStyle = c.upSoft;
    ctx.fillRect(x0, Math.min(yE, yT), w, Math.abs(yT - yE));
    ctx.fillStyle = c.downSoft;
    ctx.fillRect(x0, Math.min(yE, yS), w, Math.abs(yS - yE));
    // The side that was hit gets an outline, so wins and losses read at a glance.
    const hitY = t.status === 'tp' ? yT : t.status === 'stop' ? yS : null;
    if (hitY !== null) {
      ctx.strokeStyle = t.status === 'tp' ? c.up : c.down;
      ctx.lineWidth = 1;
      ctx.strokeRect(x0 + 0.5, Math.min(yE, hitY) + 0.5, w - 1, Math.abs(hitY - yE) - 1);
    }
    ctx.strokeStyle = c.text;
    ctx.lineWidth = 1;
    this.line(ctx, { x: x0, y: yE }, { x: x0 + w, y: yE });
    const text = `${t.r > 0 ? '+' : '−'}${Math.abs(t.r).toFixed(1)}R`;
    // The result sits inside the box's top edge, only when it fits, so neighbouring trades never overlap.
    const tw = ctx.measureText(text).width + 10;
    if (w >= tw) {
      const top = Math.min(yT, yS);
      ctx.fillStyle = t.r > 0 ? c.up : c.down;
      ctx.fillRect(x0, top, tw, 18);
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'left';
      ctx.fillText(text, x0 + 5, top + 9);
    }
    ctx.globalAlpha = 1;
  }

  private ruler(ctx: CanvasRenderingContext2D, d: Drawing, ps: Px[]) {
    const [a, b] = d.points;
    const upMove = b.price >= a.price;
    const color = upMove ? this.colors.up : this.colors.down;
    const [x, y, w, h] = this.box(ps[0], ps[1]);
    ctx.fillStyle = upMove ? this.colors.upSoft : this.colors.downSoft;
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    this.line(ctx, { x: x + w / 2, y: ps[0].y }, { x: x + w / 2, y: ps[1].y });
    this.line(ctx, { x: ps[0].x, y: y + h / 2 }, { x: ps[1].x, y: y + h / 2 });

    const bars = Math.round(this.logical(b.time) - this.logical(a.time));
    const lines = [
      `${upMove ? '+' : '−'}${fmt(Math.abs(b.price - a.price))} (${pct(a.price, b.price)})`,
      `${bars} bar, ${duration(b.time - a.time)}`,
    ];
    const tw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16;
    const lx = x + w / 2 - tw / 2;
    const ly = upMove ? y - 48 : y + h + 8;
    ctx.fillStyle = color;
    ctx.fillRect(lx, ly, tw, 40);
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'center';
    lines.forEach((l, i) => ctx.fillText(l, lx + tw / 2, ly + 11 + i * 18));
  }

  private line(ctx: CanvasRenderingContext2D, a: Px, b: Px) {
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }

  private box(a: Px, b: Px): [number, number, number, number] {
    return [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)];
  }

  private alpha(color: string, a: number) {
    const m = /^#([0-9a-f]{6})$/i.exec(color);
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
}
