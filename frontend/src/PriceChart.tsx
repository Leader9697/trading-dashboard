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
import type { CandleResponse, Signal } from './api';

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
    candles.setData(data.candles.map((c) => ({ ...c, time: c.time as Time })));

    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' } });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    volume.setData(
      data.candles.map((c) => ({
        time: c.time as Time,
        value: c.volume,
        color: c.close >= c.open ? css('--up-soft') : css('--down-soft'),
      })),
    );

    const lines = [
      { points: data.indicators.ema20, color: css('--ema20') },
      { points: data.indicators.ema50, color: css('--ema50') },
      { points: data.indicators.ema200, color: css('--ema200') },
    ];
    for (const { points, color } of lines) {
      const s = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      s.setData(points.map((p) => ({ ...p, time: p.time as Time })));
    }

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
    return () => chart.remove();
  }, [data, signal]);

  return <div className="chart" ref={host} />;
}
