import { useEffect, useState, type FormEvent } from 'react';
import { api, type CandleResponse, type Config, type Decision } from './api';
import { PriceChart } from './PriceChart';

const TONE: Record<string, string> = {
  buy: 'up', add: 'up', sell: 'down', reduce: 'down', avoid: 'down', alert: 'down',
};

const SOURCE_LABEL: Record<string, string> = {
  demo: 'data demo (sintetis)',
  yfinance: 'Yahoo Finance',
  daily_stock_analysis: 'daily_stock_analysis',
  dsa: 'daily_stock_analysis',
  rules: 'aturan teknikal MA/RSI',
};

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [symbol, setSymbol] = useState('');
  const [query, setQuery] = useState('');
  const [chart, setChart] = useState<CandleResponse | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.config().then((c) => {
      setConfig(c);
      setSymbol(c.watchlist[0] ?? 'AAPL');
    }, (e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!symbol) return;
    let live = true;
    setLoading(true);
    setError(null);
    Promise.allSettled([api.candles(symbol), api.decision(symbol)]).then(([c, d]) => {
      if (!live) return;
      setChart(c.status === 'fulfilled' ? c.value : null);
      setDecision(d.status === 'fulfilled' ? d.value : null);
      const failed = [c, d].find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined;
      setError(failed ? String(failed.reason?.message ?? failed.reason) : null);
      setLoading(false);
    });
    return () => { live = false; };
  }, [symbol]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const s = query.trim().toUpperCase();
    if (s) setSymbol(s);
    setQuery('');
  };

  const last = chart?.candles.at(-1);
  const prev = chart?.candles.at(-2);
  const change = last && prev ? ((last.close - prev.close) / prev.close) * 100 : null;

  return (
    <div className="app">
      <header className="top">
        <h1>Dashboard Keputusan Trading</h1>
        <nav className="watchlist">
          {config?.watchlist.map((s) => (
            <button key={s} className={s === symbol ? 'chip active' : 'chip'} onClick={() => setSymbol(s)}>{s}</button>
          ))}
          <form onSubmit={submit}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Ticker lain, mis. ASII.JK" aria-label="Cari ticker" />
          </form>
        </nav>
      </header>

      {error && <div className="error">{error}</div>}

      <main className="grid">
        <section className="panel chart-panel">
          <div className="panel-head">
            <div>
              <h2>{symbol}{chart?.name ? ` · ${chart.name}` : ''}</h2>
              {last && (
                <p className="price">
                  {last.close.toFixed(2)}
                  {change !== null && <span className={change >= 0 ? 'up' : 'down'}> {change >= 0 ? '+' : ''}{change.toFixed(2)}%</span>}
                </p>
              )}
            </div>
            <div className="legend">
              <span><i className="swatch ma20" />MA20</span>
              <span><i className="swatch ma50" />MA50</span>
            </div>
          </div>
          {chart ? <PriceChart data={chart} markers={decision?.markers ?? []} /> : <div className="chart empty">{loading ? 'Memuat…' : 'Tidak ada data harga'}</div>}
          <p className="source">Harga: {chart ? SOURCE_LABEL[chart.source] ?? chart.source : '–'}</p>
        </section>

        <aside className="panel decision">
          <h2>Keputusan hari ini</h2>
          {decision ? (
            <>
              <div className={`verdict ${TONE[decision.action] ?? 'neutral'}`}>
                <span className="label">{decision.action_label}</span>
                {decision.score !== null && <span className="score">{decision.score}<small>/100</small></span>}
              </div>
              {decision.trend && <p className="trend">Tren: {decision.trend}</p>}
              <p className="summary">{decision.summary}</p>
              <dl className="levels">
                <div><dt>Masuk</dt><dd>{decision.strategy.entry ?? '–'}</dd></div>
                <div><dt>Stop loss</dt><dd className="down">{decision.strategy.stop_loss ?? '–'}</dd></div>
                <div><dt>Target</dt><dd className="up">{decision.strategy.take_profit ?? '–'}</dd></div>
              </dl>
              {decision.reasons.length > 0 && (<><h3>Alasan</h3><ul>{decision.reasons.map((r) => <li key={r}>{r}</li>)}</ul></>)}
              {decision.risks.length > 0 && (<><h3>Risiko</h3><ul className="risks">{decision.risks.map((r) => <li key={r}>{r}</li>)}</ul></>)}
              <p className="source">Sinyal: {SOURCE_LABEL[decision.source] ?? decision.source}{decision.as_of ? ` · per ${decision.as_of.slice(0, 10)}` : ''}</p>
            </>
          ) : (
            <p className="muted">{loading ? 'Menghitung…' : 'Sinyal tidak tersedia'}</p>
          )}
        </aside>

        <Research symbol={symbol} enabled={Boolean(config?.sources.research)} />
      </main>

      <footer>Alat bantu riset, bukan saran investasi. Keputusan akhir tetap di tanganmu.</footer>
    </div>
  );
}

function Research({ symbol, enabled }: { symbol: string; enabled: boolean }) {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => setAnswer(null), [symbol]);

  const ask = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await api.research(symbol, question);
      setAnswer(typeof res.answer === 'string' ? res.answer : JSON.stringify(res, null, 2));
    } catch (err) {
      setAnswer((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel research">
      <h2>Riset mendalam <small>(dexter)</small></h2>
      {!enabled && <p className="muted">Belum aktif. Isi DEXTER_URL di backend untuk menyambungkan dexter (Tahap 3).</p>}
      <form onSubmit={ask}>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={`Contoh: Bagaimana tren margin laba ${symbol} 5 tahun terakhir?`}
          disabled={!enabled || busy}
          rows={3}
        />
        <button type="submit" disabled={!enabled || busy || question.trim().length < 3}>{busy ? 'Meriset…' : 'Tanya'}</button>
      </form>
      {answer && <pre className="answer">{answer}</pre>}
    </section>
  );
}
