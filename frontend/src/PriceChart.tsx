import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type Time,
} from 'lightweight-charts';
import { api, type Candle, type CandleResponse, type Signal } from './api';

const MAX_BARS = 25000; // about 2017 to now on 4h

function ema(values: number[], n: number): (number | null)[] {
  const out: (number | null)[] = values.map(() => null);
  if (values.length < n) return out;
  const k = 2 / (n + 1);
  let prev = values.slice(0, n).reduce((a, b) => a + b, 0) / n;
  out[n - 1] = prev;
  for (let i = n; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function PriceChart({ data, signal }: { data: CandleResponse; signal: Signal | null }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!host.current) return;
    const up = css('--up');
    const down = css('--down');
    const chart: IChartApi = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: css('--muted'),
        // Keeps the TradingView attribution link required by the Apache-2.0 NOTICE.
        attributionLogo: true,
      },
      grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
    });

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: up,
      downColor: down,
      wickUpColor: up,
      wickDownColor: down,
      borderVisible: false,
    });
    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' } });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    const emas = ([[20, '--ema20'], [50, '--ema50'], [200, '--ema200']] as const).map(([n, color]) => ({
      n, series: chart.addSeries(LineSeries, { color: css(color), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
    }));
    const upSoft = css('--up-soft'), downSoft = css('--down-soft');

    let bars: Candle[] = data.candles;
    const draw = () => {
      candles.setData(bars.map((c) => ({ ...c, time: c.time as Time })));
      volume.setData(bars.map((c) => ({ time: c.time as Time, value: c.volume, color: c.close >= c.open ? upSoft : downSoft })));
      const closes = bars.map((c) => c.close);
      for (const { n, series } of emas) {
        series.setData(ema(closes, n).flatMap((v, i) => (v === null ? [] : [{ time: bars[i].time as Time, value: v }])));
      }
    };
    draw();

    // Scrolling near the left edge loads the previous 1000 bars, back to the start of Binance history.
    let busy = false, done = false, live = true;
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || range.from > 30 || busy || done || bars.length >= MAX_BARS) return;
      busy = true;
      api.candles(data.symbol, data.interval, bars[0].time).then((older) => {
        if (!live) return;
        const add = older.candles.filter((c) => c.time < bars[0].time);
        if (add.length === 0) { done = true; return; }
        const view = chart.timeScale().getVisibleLogicalRange();
        bars = [...add, ...bars];
        draw();
        if (view) chart.timeScale().setVisibleLogicalRange({ from: view.from + add.length, to: view.to + add.length });
      }, () => { done = true; }).finally(() => { busy = false; });
    });

    if (signal) {
      const marks = [
        { price: signal.entry, color: css('--accent'), title: 'Entry', style: LineStyle.Dotted },
        { price: signal.stop_loss, color: down, title: 'SL', style: LineStyle.Solid },
        { price: signal.take_profit, color: up, title: 'TP', style: LineStyle.Dashed },
      ];
      for (const m of marks) {
        candles.createPriceLine({ price: m.price, color: m.color, lineWidth: 1, lineStyle: m.style, axisLabelVisible: true, title: m.title });
      }
    }

    chart.timeScale().fitContent();
    return () => { live = false; chart.remove(); };
  }, [data, signal]);

  return <div className="chart" ref={host} />;
}
