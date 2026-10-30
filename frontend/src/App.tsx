import { useEffect, useState, type FormEvent } from 'react';
import { api, fmt, type CandleResponse, type Config, type Debate, type DebateCoin, type Mode, type Setup, type Snapshot } from './api';
import { PriceChart } from './PriceChart';

const DIR_LABEL = { long: 'LONG', short: 'SHORT', wait: 'TUNGGU' } as const;
const DIR_TONE = { long: 'up', short: 'down', wait: 'neutral' } as const;

function wib(unixOrIso: number | string) {
  const d = typeof unixOrIso === 'number' ? new Date(unixOrIso * 1000) : new Date(unixOrIso);
  return d.toLocaleString('id-ID', { timeZone: 'Asia/Jakarta', dateStyle: 'medium', timeStyle: 'short' }) + ' WIB';
}

function base(symbol: string) {
  return symbol.replace(/USDT$/, '');
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [mode, setMode] = useState<Mode>('swing');
  const [query, setQuery] = useState('');
  const [chart, setChart] = useState<CandleResponse | null>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [debate, setDebate] = useState<Debate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.config().then((c) => {
      setConfig(c);
      setSymbol(c.watchlist[0] ?? 'BTCUSDT');
    }, (e: Error) => setError(e.message));
    api.debate().then(setDebate, () => setDebate({ available: false }));
  }, []);

  useEffect(() => {
    api.snapshot(mode).then(setSnapshot, () => setSnapshot(null));
  }, [mode]);

  useEffect(() => {
    if (!config) return;
    let live = true;
    setLoading(true);
    setError(null);
    const interval = config.modes[mode].entry;
    Promise.allSettled([api.candles(symbol, interval), api.setup(symbol, mode)]).then(([c, s]) => {
      if (!live) return;
      setChart(c.status === 'fulfilled' ? c.value : null);
      setSetup(s.status === 'fulfilled' ? s.value : null);
      const failed = [c, s].find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined;
      setError(failed ? String(failed.reason?.message ?? failed.reason) : null);
      setLoading(false);
    });
    return () => { live = false; };
  }, [symbol, mode, config]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    let s = query.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (s && !s.endsWith('USDT')) s += 'USDT';
    if (s) setSymbol(s);
    setQuery('');
  };

  const tf = config?.modes[mode];
  const coinDebate = debate?.available ? debate.coins[symbol] : undefined;

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>Crypto Trade Setup</h1>
          {config?.data_mode === 'demo' && <span className="badge">data demo</span>}
        </div>
        <div className="controls">
          <div className="segmented" role="tablist" aria-label="Mode">
            {(['swing', 'scalp'] as Mode[]).map((m) => (
              <button key={m} className={m === mode ? 'on' : ''} onClick={() => setMode(m)}>
                {m === 'swing' ? 'Swing' : 'Scalp'} <small>{config?.modes[m].trend}/{config?.modes[m].entry}</small>
              </button>
            ))}
          </div>
          <form onSubmit={submit}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Koin lain, mis. DOGE" aria-label="Cari koin" />
          </form>
        </div>
      </header>

      <SummaryTable snapshot={snapshot} active={symbol} onPick={setSymbol} />

      {error && <div className="error">{error}</div>}

      <main className="grid">
        <section className="panel chart-panel">
          <div className="panel-head">
            <div>
              <h2>{base(symbol)}<small>/USDT · {tf?.entry}</small></h2>
              {setup && <p className="price">{fmt(setup.price)}</p>}
            </div>
            <div className="legend">
              <span><i className="swatch ema20" />EMA20</span>
              <span><i className="swatch ema50" />EMA50</span>
              <span><i className="swatch ema200" />EMA200</span>
            </div>
          </div>
          {chart ? <PriceChart data={chart} levels={setup?.levels ?? null} /> : <div className="chart empty">{loading ? 'Memuat…' : 'Tidak ada data'}</div>}
          {setup && <Context setup={setup} />}
        </section>

        <aside className="panel setup">
          <h2>Trade setup <small>{mode}</small></h2>
          {setup ? <SetupCard setup={setup} /> : <p className="muted">{loading ? 'Menghitung…' : 'Setup tidak tersedia'}</p>}
        </aside>

        <DebatePanel debate={debate} coin={coinDebate} symbol={symbol} setup={setup} />
      </main>

      <footer>Analisis riset, bukan saran keuangan. Harga bisa menembus level mana pun; keputusan dan eksekusi tetap di tanganmu.</footer>
    </div>
  );
}

function SummaryTable({ snapshot, active, onPick }: { snapshot: Snapshot | null; active: string; onPick: (s: string) => void }) {
  if (!snapshot) return <div className="panel summary muted">Memuat ringkasan…</div>;
  return (
    <div className="panel summary">
      <table>
        <thead>
          <tr><th>Koin</th><th>Harga</th><th>Bias</th><th>Entry</th><th>SL</th><th>TP1 / TP2</th><th>Checklist</th></tr>
        </thead>
        <tbody>
          {snapshot.coins.map((c) => (
            <tr key={c.s} className={c.s === active ? 'active' : ''} onClick={() => onPick(c.s)}>
              <td><strong>{base(c.s)}</strong></td>
              {c.error || !c.plan ? (
                <td colSpan={6} className="muted">{c.error ?? 'tidak ada data'}</td>
              ) : (
                <>
                  <td>{fmt(c.px)}</td>
                  <td><span className={`pill ${DIR_TONE[c.dir!]}`}>{DIR_LABEL[c.dir!]}</span> <small>{c.score}</small></td>
                  <td>{fmt(c.plan[1][0])}–{fmt(c.plan[1][1])}</td>
                  <td className="down">{fmt(c.plan[2])}</td>
                  <td className="up">{fmt(c.plan[3])} / {fmt(c.plan[4])}</td>
                  <td>{c.ok}/6</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="source">Per {wib(snapshot.t)}. Klik baris untuk membuka grafik.</p>
    </div>
  );
}

function SetupCard({ setup }: { setup: Setup }) {
  const lv = setup.levels;
  const waiting = setup.direction === 'wait';
  return (
    <>
      <div className={`verdict ${DIR_TONE[setup.direction]}`}>
        <span className="label">{DIR_LABEL[setup.direction]}</span>
        <span className="score">{setup.score > 0 ? '+' : ''}{setup.score}<small> · {setup.rating}</small></span>
      </div>
      {waiting && (
        <p className="note">Bias belum cukup kuat. Level di bawah adalah rencana sisi {setup.plan_side === 'long' ? 'long' : 'short'} jika konfirmasi muncul.</p>
      )}
      <dl className="levels">
        <div className="wide"><dt>Zona entry ({setup.plan_side})</dt><dd>{fmt(lv.entry_zone[0])} – {fmt(lv.entry_zone[1])}</dd></div>
        <div><dt>Stop loss</dt><dd className="down">{fmt(lv.stop_loss)}</dd><small>{lv.sl_atr}× ATR</small></div>
        <div><dt>TP1</dt><dd className="up">{fmt(lv.tp1)}</dd><small>R:R 1:{lv.rr_tp1}</small></div>
        <div><dt>TP2</dt><dd className="up">{fmt(lv.tp2)}</dd><small>R:R 1:{lv.rr_tp2}</small></div>
        <div><dt>Risiko/trade</dt><dd>{setup.risk_pct}%</dd><small>dari modal</small></div>
      </dl>
      <p className="invalid">Batal jika: {lv.invalidation}{lv.hurdle ? `. Waspadai level ${fmt(lv.hurdle)} sebelum TP1.` : '.'}</p>
      <h3>Checklist</h3>
      <ul className="checklist">
        {setup.checklist.map((c) => <li key={c.label} className={c.ok ? 'ok' : 'no'}>{c.label}</li>)}
      </ul>
      <p className="source">Tren {setup.timeframes.trend}, entry {setup.timeframes.entry} · candle {wib(setup.as_of)}</p>
    </>
  );
}

function Context({ setup }: { setup: Setup }) {
  const d = setup.derivatives;
  const items = [
    ['Funding', d.funding_pct === null ? '–' : `${d.funding_pct.toFixed(4)}%`],
    ['OI 24 jam', d.oi_change_24h_pct === null ? '–' : `${d.oi_change_24h_pct > 0 ? '+' : ''}${d.oi_change_24h_pct}%`],
    ['Long/Short', d.long_short_ratio ?? '–'],
    ['Fear & Greed', setup.fear_greed ? `${setup.fear_greed.value} · ${setup.fear_greed.label}` : '–'],
  ];
  return (
    <div className="context">
      {items.map(([k, v]) => <div key={k}><span>{k}</span><strong>{v}</strong></div>)}
    </div>
  );
}

function DebatePanel({ debate, coin, symbol, setup }: { debate: Debate | null; coin: DebateCoin | undefined; symbol: string; setup: Setup | null }) {
  const live = debate?.available ? debate : null;
  return (
    <section className="panel debate">
      <div className="panel-head">
        <h2>Debat AI <small>bull vs bear vs manajer risiko</small></h2>
        {live && <span className="source">Diperbarui {wib(live.updated_at)}</span>}
      </div>
      {live?.market_note && <p className="market-note">{live.market_note}</p>}
      {coin ? (
        <div className="debate-grid">
          <div className="side bull"><h3>Bull</h3><p>{coin.bull}</p></div>
          <div className="side bear"><h3>Bear</h3><p>{coin.bear}</p></div>
          <div className="side manager">
            <h3>Keputusan: {coin.verdict}{coin.confidence ? ` · keyakinan ${coin.confidence}` : ''}</h3>
            <p>{coin.manager}</p>
            {coin.plan_note && <p className="note">{coin.plan_note}</p>}
          </div>
        </div>
      ) : (
        <div className="debate-grid">
          <p className="muted span">
            {live ? `Belum ada debat untuk ${base(symbol)}. Fakta otomatis dari data:` : 'Debat AI belum tersedia. Sementara ini, fakta otomatis dari data:'}
          </p>
          <div className="side bull"><h3>Poin bull</h3><ul>{setup?.bull_points.map((p) => <li key={p}>{p}</li>)}</ul></div>
          <div className="side bear"><h3>Poin bear</h3><ul>{setup?.bear_points.map((p) => <li key={p}>{p}</li>)}</ul></div>
        </div>
      )}
      {live?.risk_note && <p className="note">{live.risk_note}</p>}
    </section>
  );
}
