import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react';
import { api, fmt, type CandleResponse, type Config, type Debate, type DebateCoin, type Journal, type Setup, type Signal, type Snapshot, type Stats } from './api';
import { PriceChart } from './PriceChart';

const SIDE_LABEL = { long: 'Long', short: 'Short' } as const;
const MODE = 'swing';

function wib(unixOrIso: number | string) {
  const d = typeof unixOrIso === 'number' ? new Date(unixOrIso * 1000) : new Date(unixOrIso);
  return d.toLocaleString('id-ID', { timeZone: 'Asia/Jakarta', dateStyle: 'medium', timeStyle: 'short' }) + ' WIB';
}

function base(symbol: string) {
  return symbol.replace(/USDT$/, '');
}

function signed(n: number) {
  return `${n > 0 ? '+' : ''}${n}`;
}

function Mark() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" aria-hidden="true">
      <path className="mark-wick" d="M7 3v18M17 3v18" strokeWidth="1.5" />
      <rect className="mark-up" x="4" y="6" width="6" height="10" rx="1" />
      <rect className="mark-down" x="14" y="9" width="6" height="9" rx="1" />
    </svg>
  );
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [query, setQuery] = useState('');
  const [chart, setChart] = useState<CandleResponse | null>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [debate, setDebate] = useState<Debate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tfChoice, setTfChoice] = useState<string | null>(null);

  useEffect(() => {
    api.config().then((c) => {
      setConfig(c);
      setSymbol(c.watchlist[0] ?? 'BTCUSDT');
      setTfChoice(c.modes[MODE].entry);
    }, (e: Error) => setError(e.message));
    api.debate().then(setDebate, () => setDebate({ available: false }));
  }, []);

  useEffect(() => {
    api.snapshot(MODE).then(setSnapshot, () => setSnapshot(null));
  }, []);

  useEffect(() => {
    if (!config) return;
    let live = true;
    setError(null);
    api.setup(symbol, MODE).then((s) => { if (live) setSetup(s); }, (e: Error) => {
      if (!live) return;
      setSetup(null);
      setError(e.message);
    });
    return () => { live = false; };
  }, [symbol, config]);

  useEffect(() => {
    if (!tfChoice) return;
    let live = true;
    setLoading(true);
    api.candles(symbol, tfChoice).then((c) => { if (live) setChart(c); }, (e: Error) => {
      if (!live) return;
      setChart(null);
      setError(e.message);
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [symbol, tfChoice]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    let s = query.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (s && !s.endsWith('USDT')) s += 'USDT';
    if (s) setSymbol(s);
    setQuery('');
  };

  const tf = config?.modes[MODE];
  const coinDebate = debate?.available ? debate.coins[symbol] : undefined;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <Mark />
          <div>
            <div className="title">
              <h1>Crypto Trade Setup</h1>
              {config?.data_mode === 'demo' && <span className="badge">data demo</span>}
            </div>
            <p>{snapshot ? `Data per ${wib(snapshot.t)}` : 'Memuat data pasar'}</p>
          </div>
        </div>
        <div className="controls">
          <form onSubmit={submit}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Koin lain, mis. DOGE" aria-label="Cari koin" />
          </form>
        </div>
      </header>

      <SummaryTable snapshot={snapshot} active={symbol} onPick={setSymbol} />

      {error && <div className="error" role="alert">{error}</div>}

      <main className="grid">
        <div className="col">
          <section className="panel chart-panel">
            <div className="panel-head">
              <div>
                <h2>{base(symbol)}<small>/USDT, {tfChoice}</small></h2>
                {setup && <p className="price">{fmt(setup.price)}</p>}
              </div>
              {tfChoice && tfChoice !== tf?.entry && <p className="note">Sinyal dihitung di {tf?.entry}. Garis entry, SL dan TP tetap sama di timeframe ini.</p>}
            </div>
            {chart && tfChoice
              ? <PriceChart data={chart} signal={setup?.signal ?? null} streaming={config?.data_mode !== 'demo'} interval={tfChoice} onInterval={setTfChoice} />
              : <div className="chart empty">{loading ? 'Memuat grafik…' : 'Grafik tidak tersedia untuk koin ini'}</div>}
            {setup && <Context setup={setup} />}
          </section>

          <DebatePanel debate={debate} coin={coinDebate} symbol={symbol} setup={setup} />
        </div>

        <aside className="panel setup">
          <div className="panel-head">
            <h2>Trade setup<small>swing {tf?.trend}/{tf?.entry}</small></h2>
          </div>
          {setup ? <SetupCard setup={setup} /> : <p className="muted">{loading ? 'Menghitung setup…' : 'Setup tidak tersedia'}</p>}
        </aside>

        <ResearchPanel />
        <JournalPanel />
      </main>

      <footer>Analisis riset, bukan saran keuangan. Harga bisa menembus level mana pun; keputusan dan eksekusi tetap di tanganmu.</footer>
    </div>
  );
}

function SummaryTable({ snapshot, active, onPick }: { snapshot: Snapshot | null; active: string; onPick: (s: string) => void }) {
  if (!snapshot) return <div className="panel summary muted">Memuat ringkasan watchlist…</div>;
  const key = (s: string) => (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(s); }
  };
  return (
    <div className="panel summary">
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Koin</th><th className="num">Harga</th><th>Sinyal</th><th className="num">Entry</th><th className="num">Stop loss</th><th className="num">Take profit</th><th className="num">Syarat</th></tr>
          </thead>
          <tbody>
            {snapshot.coins.map((c) => (
              <tr key={c.s} className={c.s === active ? 'active' : ''} onClick={() => onPick(c.s)} onKeyDown={key(c.s)} tabIndex={0} aria-selected={c.s === active}>
                <td><strong>{base(c.s)}</strong></td>
                {c.error || !c.sig ? (
                  <td colSpan={6} className="muted">{c.error ?? 'Tidak ada data'}</td>
                ) : (
                  <>
                    <td className="num">{fmt(c.px)}</td>
                    <td>{c.sig[0]
                      ? <span className={`pill ${c.sig[1] === 'long' ? 'up' : 'down'}`}>{SIDE_LABEL[c.sig[1]]}</span>
                      : <span className="pill neutral">Tunggu</span>}</td>
                    <td className="num">{fmt(c.sig[2])}</td>
                    <td className="num down">{fmt(c.sig[3])}</td>
                    <td className="num up">{fmt(c.sig[4])}</td>
                    <td className="num">{c.sig[5]}<small>/6</small></td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const STAGE_LABEL: Record<Signal['stage'], string> = {
  forward_test: 'Tahap uji dengan uang kecil.',
  validated: 'Lolos uji sinyal otomatis.',
  rejected: 'Tidak disarankan.',
  research: 'Belum lolos uji sinyal otomatis.',
};

function stored(key: string, fallback: number) {
  try {
    const v = Number(localStorage.getItem(key));
    return v > 0 ? v : fallback;
  } catch {
    return fallback;
  }
}

function store(key: string, v: number) {
  try { localStorage.setItem(key, String(v)); } catch { /* private window */ }
}

function NumberField({ label, value, onChange, step, suffix }: { label: string; value: number; onChange: (v: number) => void; step: number; suffix: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      <span className="input-suffix">
        <input type="number" inputMode="decimal" min={step} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
        <small>{suffix}</small>
      </span>
    </label>
  );
}

function usd(n: number) {
  return `$${n < 10 ? n.toFixed(2) : n.toFixed(1)}`;
}

function Sizing({ sig }: { sig: Signal }) {
  const [equity, setEquity] = useState(() => stored('equity', 55));
  const [risk, setRisk] = useState(() => stored('risk_pct', 1));
  useEffect(() => store('equity', equity), [equity]);
  useEffect(() => store('risk_pct', risk), [risk]);
  if (!sig.sl_pct || equity <= 0 || risk <= 0) return null;

  const riskUsd = equity * risk / 100;
  const notional = riskUsd / (sig.sl_pct / 100);
  const maxLev = Math.max(1, Math.floor(100 / (2 * sig.sl_pct)));
  const lev = Math.min(maxLev, Math.max(1, Math.ceil(notional / (0.3 * equity))));
  const margin = notional / lev;
  const tooSmall = notional < sig.min_notional;
  const needRisk = (sig.min_notional * sig.sl_pct) / equity;

  return (
    <div className="sizing">
      <h3>Ukuran posisi</h3>
      <div className="fields">
        <NumberField label="Modal" value={equity} onChange={setEquity} step={1} suffix="USDT" />
        <NumberField label="Risiko per trade" value={risk} onChange={setRisk} step={0.5} suffix="%" />
      </div>
      <dl className="levels">
        <div><dt>Kalau kena SL, rugi</dt><dd className="down">{usd(riskUsd)}</dd></div>
        <div><dt>Kalau kena TP, untung</dt><dd className="up">{usd(riskUsd * sig.rr)}</dd></div>
        <div><dt>Nilai posisi</dt><dd>{usd(notional)}</dd><small>{fmt(notional / sig.entry)} {'koin'}</small></div>
        <div><dt>Leverage</dt><dd>{lev}x isolated</dd><small>margin {usd(margin)}, maks aman {maxLev}x</small></div>
      </dl>
      {tooSmall && (
        <p className="warn">
          Posisi {usd(notional)} di bawah minimal order Binance ({sig.min_notional} USDT). Butuh risiko {needRisk.toFixed(1)}% dari modal.
          {needRisk > 2 ? ' Itu terlalu besar, lewati koin ini.' : ' Naikkan risiko sedikit atau lewati.'}
        </p>
      )}
    </div>
  );
}

function SetupCard({ setup }: { setup: Setup }) {
  const sig = setup.signal;
  const passed = sig.checks.filter((c) => c.ok).length;
  const tone = sig.active ? (sig.side === 'long' ? 'up' : 'down') : 'neutral';
  return (
    <>
      <div className={`verdict ${tone}`}>
        <span className={`label ${tone}`}>{sig.active ? `Pasang limit ${SIDE_LABEL[sig.side].toLowerCase()}` : 'Tunggu'}</span>
        <span className="score-big">{passed}/{sig.checks.length}<small>syarat terpenuhi</small></span>
      </div>
      {!sig.active && (
        <p className="wait-note">Belum ada sinyal, jangan entry. Angka di bawah hanya gambaran kalau sinyal {SIDE_LABEL[sig.side].toLowerCase()} muncul di harga sekarang.</p>
      )}
      <dl className="levels">
        <div className="full"><dt>Entry ({SIDE_LABEL[sig.side].toLowerCase()}, order limit)</dt><dd>{fmt(sig.entry)}</dd><small>Berlaku {sig.valid_hours ?? 16} jam. Kalau tidak terisi, batalkan dan jangan kejar harga.</small></div>
        <div><dt>Stop loss</dt><dd className="down">{fmt(sig.stop_loss)}</dd><small>{sig.sl_pct ?? '–'}% dari entry</small></div>
        <div><dt>Take profit</dt><dd className="up">{fmt(sig.take_profit)}</dd><small>R:R 1:{sig.rr}, tutup semua di sini</small></div>
      </dl>
      <Sizing sig={sig} />
      <h3>Syarat sinyal</h3>
      <ul className="checklist">
        {sig.checks.map((c) => <li key={c.label} className={c.ok ? 'ok' : 'no'}>{c.label}</li>)}
      </ul>
      <p className="note">{STAGE_LABEL[sig.stage]} {sig.note}</p>
      <p className="source">Skor bias teknikal {signed(setup.score)} ({setup.rating}). Tren {setup.timeframes.trend}, entry {setup.timeframes.entry}. Candle terakhir {wib(setup.as_of)}.</p>
    </>
  );
}

function Context({ setup }: { setup: Setup }) {
  const d = setup.derivatives;
  const items: [string, string, string?][] = [
    ['Funding', d.funding_pct === null ? '–' : `${d.funding_pct.toFixed(4)}%`],
    ['OI 24 jam', d.oi_change_24h_pct === null ? '–' : `${signed(d.oi_change_24h_pct)}%`],
    ['Long/short', d.long_short_ratio === null ? '–' : String(d.long_short_ratio)],
    ['Fear & Greed', setup.fear_greed ? String(setup.fear_greed.value) : '–', setup.fear_greed?.label],
  ];
  return (
    <div className="strip">
      {items.map(([k, v, sub]) => <div key={k}><span>{k}</span><strong>{v}</strong>{sub && <small>{sub}</small>}</div>)}
    </div>
  );
}

function DebatePanel({ debate, coin, symbol, setup }: { debate: Debate | null; coin: DebateCoin | undefined; symbol: string; setup: Setup | null }) {
  const live = debate?.available ? debate : null;
  return (
    <section className="panel debate">
      <div className="panel-head">
        <h2>Debat AI<small>bull, bear, manajer risiko</small></h2>
        {live && <span className="source">Diperbarui {wib(live.updated_at)}</span>}
      </div>
      {live?.market_note && <p className="market-note">{live.market_note}</p>}
      {coin ? (
        <div className="debate-grid">
          <div className="side bull"><h3>Bull</h3><p>{coin.bull}</p></div>
          <div className="side bear"><h3>Bear</h3><p>{coin.bear}</p></div>
          <div className="side manager">
            <h3>Keputusan: {coin.verdict}{coin.confidence ? `, keyakinan ${coin.confidence}` : ''}</h3>
            <p>{coin.manager}</p>
            {coin.plan_note && <p className="note">{coin.plan_note}</p>}
          </div>
        </div>
      ) : (
        <>
          <p className="muted debate-empty">
            {live ? `Belum ada debat untuk ${base(symbol)}. Sementara ini, fakta otomatis dari data:` : 'Debat AI belum tersedia. Sementara ini, fakta otomatis dari data:'}
          </p>
          <div className="debate-grid two">
            <div className="side bull"><h3>Poin bull</h3><ul>{setup?.bull_points.map((p) => <li key={p}>{p}</li>)}</ul></div>
            <div className="side bear"><h3>Poin bear</h3><ul>{setup?.bear_points.map((p) => <li key={p}>{p}</li>)}</ul></div>
          </div>
        </>
      )}
      {live?.risk_note && <p className="note">{live.risk_note}</p>}
    </section>
  );
}

const STATUS_LABEL: Record<string, string> = {
  stop: 'Kena SL', breakeven: 'TP1 lalu BE', tp2: 'TP2', timeout: 'Waktu habis', timeout_after_tp1: 'TP1 lalu waktu habis',
  missed: 'Tidak terisi', pending: 'Menunggu entry', open: 'Berjalan', no_data: 'Tanpa data',
};

function r(n: number | null | undefined) {
  if (n === null || n === undefined) return '–';
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}R`;
}

function tone(n: number | null | undefined) {
  return n === null || n === undefined || n === 0 ? '' : n > 0 ? 'up' : 'down';
}

function StatGrid({ s }: { s: Stats }) {
  const items: [string, string, string?][] = [
    ['Trade', String(s.trades)],
    ['Winrate', s.winrate === null ? '–' : `${s.winrate}%`],
    ['Ekspektasi', r(s.expectancy_r), tone(s.expectancy_r)],
    ['Total', r(s.total_r), tone(s.total_r)],
    ['Profit factor', s.profit_factor === null ? '–' : String(s.profit_factor)],
    ['Max drawdown', s.max_drawdown_r ? `-${s.max_drawdown_r}R` : '0R'],
  ];
  return (
    <div className="strip stats">
      {items.map(([k, v, t]) => <div key={k}><span>{k}</span><strong className={t}>{v}</strong></div>)}
    </div>
  );
}

const YEARS: [string, number, number][] = [['2022', 80, 0.159], ['2023', 147, 0.147], ['2024', 130, 0.235], ['2025', 124, 0.198], ['2026', 96, 0.212]];

function ResearchPanel() {
  return (
    <section className="panel backtest wide-panel">
      <div className="panel-head">
        <h2>Hasil uji aturan sinyal<small>BTC, ETH, SOL, BNB, XRP, 4h, 2022 sampai Okt 2026</small></h2>
      </div>
      <StatGrid s={{ trades: 577, winrate: 39.5, expectancy_r: 0.19, total_r: 109.9, profit_factor: 1.32, max_drawdown_r: 32.1 }} />
      <h3>Per tahun (rata-rata per trade)</h3>
      <div className="strip folds">
        {YEARS.map(([y, n, e]) => (
          <div key={y}><span>{y}, {n} trade</span><strong className={tone(e)}>{r(e)}</strong></div>
        ))}
      </div>
      <p className="note">Entry limit 0,25 ATR lebih baik dari harga sinyal (berlaku 4 candle, dilewati kalau tidak terisi), SL 2 ATR, satu TP di 3R, satu posisi per koin, biaya 0,08% sudah dipotong. Dibanding entry market + SL struktur + TP 2,5R (571 trade, +0,12R, total +68,5R): jumlah trade hampir sama, hasil per trade naik. Winrate di bawah 40%, jadi kalah beruntun panjang itu normal dan risiko per trade harus kecil. Uji harga saja 2017–2026 (10 koin, 4260 trade, tanpa data OI): +0,06R per trade, rugi di tahun bear/sideways (2018, 2022, 2023). Hasil masa lalu tidak menjamin hasil berikutnya.</p>
    </section>
  );
}

function JournalPanel() {
  const [j, setJ] = useState<Journal | null>(null);
  useEffect(() => { api.journal().then(setJ, () => setJ(null)); }, []);
  if (!j) return null;
  return (
    <section className="panel journal wide-panel">
      <div className="panel-head">
        <h2>Jurnal keputusan AI<small>dinilai otomatis dari candle sesudah keputusan</small></h2>
      </div>
      {j.results.length === 0 ? (
        <p className="muted">Belum ada catatan. Setiap debat terjadwal akan mencatat keputusan long atau short, lalu hasilnya dinilai di sini.</p>
      ) : (
        <>
          <StatGrid s={j.summary} />
          <div className="table-wrap">
            <table>
              <thead><tr><th>Waktu</th><th>Koin</th><th>Arah</th><th className="num">Entry</th><th className="num">Stop loss</th><th className="num">TP1</th><th className="num">TP2</th><th>Hasil</th><th className="num">R</th></tr></thead>
              <tbody>
                {[...j.results].reverse().map((x) => (
                  <tr key={`${x.ts}-${x.symbol}`}>
                    <td>{wib(x.ts)}</td>
                    <td><strong>{base(x.symbol)}</strong></td>
                    <td><span className={`pill ${x.side === 'long' ? 'up' : 'down'}`}>{SIDE_LABEL[x.side]}</span></td>
                    <td className="num">{fmt(x.entry)}</td>
                    <td className="num down">{fmt(x.stop_loss)}</td>
                    <td className="num up">{fmt(x.tp1)}</td>
                    <td className="num up">{fmt(x.tp2)}</td>
                    <td>{STATUS_LABEL[x.status] ?? x.status}</td>
                    <td className={`num ${tone(x.r)}`}>{r(x.r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
