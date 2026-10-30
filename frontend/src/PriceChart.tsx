import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts';
import type { CandleResponse, Marker } from './api';

const BULLISH = new Set(['buy', 'add']);
const BEARISH = new Set(['sell', 'reduce', 'avoid']);

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function PriceChart({ data, markers }: { data: CandleResponse; markers: Marker[] }) {
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
      timeScale: { borderVisible: false },
    });

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: up,
      downColor: down,
      wickUpColor: up,
      wickDownColor: down,
      borderVisible: false,
    });
    candles.setData(data.candles.map((c) => ({ ...c, time: c.time as Time })));

    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' } });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(
      data.candles.map((c) => ({
        time: c.time as Time,
        value: c.volume ?? 0,
        color: c.close >= c.open ? css('--up-soft') : css('--down-soft'),
      })),
    );

    const lines = [
      { points: data.indicators.sma20, color: css('--ma20') },
      { points: data.indicators.sma50, color: css('--ma50') },
    ];
    for (const { points, color } of lines) {
      const s = chart.addSeries(LineSeries, { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
      s.setData(points.map((p) => ({ ...p, time: p.time as Time })));
    }

    // A report can be dated on a non-trading day; pin it to the last candle on or before that date.
    const times = data.candles.map((c) => c.time);
    const snap = (t: string) => {
      let found: string | null = null;
      for (const ct of times) {
        if (ct > t) break;
        found = ct;
      }
      return found;
    };
    const seriesMarkers: SeriesMarker<Time>[] = markers
      .map((m) => ({ ...m, time: snap(m.time) }))
      .filter((m): m is Marker => m.time !== null)
      .map((m) => {
        const bull = BULLISH.has(m.action);
        const bear = BEARISH.has(m.action);
        const marker: SeriesMarker<Time> = {
          time: m.time as Time,
          position: bull ? 'belowBar' : 'aboveBar',
          shape: bull ? 'arrowUp' : bear ? 'arrowDown' : 'circle',
          color: bull ? up : bear ? down : css('--muted'),
          text: m.label,
        };
        return marker;
      })
      .sort((a, b) => String(a.time).localeCompare(String(b.time)));
    createSeriesMarkers(candles, seriesMarkers);

    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [data, markers]);

  return <div className="chart" ref={host} />;
}
