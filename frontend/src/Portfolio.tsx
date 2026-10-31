import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { api, fmt, type AccountRun, type LivePlan, type Portfolio, type PortfolioCoin } from './api';

const SIDE_LABEL = { long: 'Long', short: 'Short' } as const;
const PICKER_LABEL: Record<string, string> = {
  all: 'Semua koin yang muat di modal',
  past_positive: 'Hanya koin yang untung sebelumnya',
  top8: '8 koin terbaik sebelumnya',
};
const REASON_LABEL: Record<NonNullable<PortfolioCoin['reason']>, string> = {
  min_order: 'Order terlalu kecil untuk minimal Binance',
  few_trades: 'Riwayat trade terlalu sedikit',
  weak: 'Hasil lalu negatif',
};

function base(s: string) {
  return s.replace(/USDT$/, '');
}

function month(t: number) {
  return new Date(t * 1000).toLocaleDateString('id-ID', { month: 'short', year: 'numeric' });
}

function usd(n: number) {
  return `$${Math.abs(n) < 10 ? n.toFixed(2) : n.toFixed(1)}`;
}

function pct(n: number) {
  return `${n > 0 ? '+' : ''}${n.toFixed(0)}%`;
}

function r(n: number | null | undefined) {
  return n === null || n === undefined ? '–' : `${n > 0 ? '+' : ''}${n.toFixed(2)}R`;
}

function tone(n: number | null | undefined) {
  return !n ? '' : n > 0 ? 'up' : 'down';
}

function RunStrip({ run, start }: { run: AccountRun; start: number }) {
  const items: [string, string, string?][] = [
    ['Modal akhir', usd(run.final), tone(run.final - start)],
    ['Hasil', pct(run.return_pct), tone(run.return_pct)],
    ['Turun terdalam', `-${run.max_dd_pct}%`],
    ['Trade', String(run.trades)],
    ['Winrate', run.wr === null ? '–' : `${run.wr}%`],
    ['Rata-rata', r(run.exp), tone(run.exp)],
  ];
  return (
    <div className="strip stats">
      {items.map(([k, v, t]) => <div key={k}><span>{k}</span><strong className={t}>{v}</strong></div>)}
    </div>
  );
}

/** Account balance over time: one line, the start balance as a reference, and the
 *  date after which nothing was used to pick the settings. */
function EquityChart({ curve, start, cut }: { curve: [number, number][]; start: number; cut: number }) {
  const box = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(720);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const H = 200, PAD = { l: 44, r: 12, t: 12, b: 22 };
  const t0 = curve[0][0], t1 = curve[curve.length - 1][0];
  const vals = curve.map((p) => p[1]);
  const lo = Math.min(start, ...vals) * 0.95, hi = Math.max(start, ...vals) * 1.03;
  const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0 || 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo || 1)) * (H - PAD.t - PAD.b);
  const path = curve.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
  const ticks = [lo, (lo + hi) / 2, hi].map((v) => Math.round(v));
  const years = [] as number[];
  for (let yr = new Date(t0 * 1000).getUTCFullYear() + 1; Date.UTC(yr, 0, 1) / 1000 < t1; yr++) years.push(yr);

  const move = (e: PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const t = t0 + (e.clientX - rect.left - PAD.l) / (W - PAD.l - PAD.r) * (t1 - t0);
    let best = 0;
    curve.forEach((p, i) => { if (Math.abs(p[0] - t) < Math.abs(curve[best][0] - t)) best = i; });
    setHover(best);
  };
  const h = hover === null ? null : curve[hover];

  return (
    <div className="pf-equity" ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Saldo akun dari waktu ke waktu"
        onPointerMove={move} onPointerLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} className="grid" />
            <text x={PAD.l - 6} y={y(v) + 4} textAnchor="end">${v}</text>
          </g>
        ))}
        {years.map((yr) => {
          const t = Date.UTC(yr, 0, 1) / 1000;
          return <text key={yr} x={x(t)} y={H - 6} textAnchor="middle">{yr}</text>;
        })}
        <line x1={PAD.l} x2={W - PAD.r} y1={y(start)} y2={y(start)} className="base" />
        {cut > t0 && cut < t1 && (
          <g>
            <line x1={x(cut)} x2={x(cut)} y1={PAD.t} y2={H - PAD.b} className="cut" />
            {x(cut) < W - 120
              ? <text x={x(cut) + 6} y={PAD.t + 4} className="cut-label">uji tanpa lihat →</text>
              : <text x={x(cut) - 6} y={PAD.t + 4} textAnchor="end" className="cut-label">uji tanpa lihat sejak garis →</text>}
          </g>
        )}
        <path d={path} className="line" />
        {h && (
          <g>
            <line x1={x(h[0])} x2={x(h[0])} y1={PAD.t} y2={H - PAD.b} className="cross" />
            <circle cx={x(h[0])} cy={y(h[1])} r={4} className="dot" />
          </g>
        )}
      </svg>
      <p className="pf-equity-tip" aria-live="polite">{h ? `${month(h[0])}: saldo ${usd(h[1])}` : `Mulai ${usd(start)} pada ${month(t0)}. Arahkan kursor ke garis untuk melihat saldo.`}</p>
    </div>
  );
}

function LiveTable({ live, maxPos }: { live: LivePlan[]; maxPos: number }) {
  const active = live.filter((l) => l.active && !l.error);
  const waiting = live.filter((l) => !l.active && !l.error);
  return (
    <>
      <h3>Yang perlu ditrade sekarang</h3>
      {active.length === 0 ? (
        <p className="muted small">Belum ada sinyal di koin pilihan. Tunggu notif Telegram; maksimal {maxPos} posisi sekaligus.</p>
      ) : (
        <>
          <p className="muted small">{active.length} sinyal aktif. Pasang maksimal {maxPos} posisi sekaligus; kalau slot penuh, lewati sinyal baru sampai ada yang tutup.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Koin</th><th>Sinyal</th><th className="num">Entry limit</th><th className="num">Stop loss</th><th className="num">Take profit</th><th className="num">Posisi</th><th className="num">Rugi kalau SL</th></tr></thead>
              <tbody>
                {active.map((l) => (
                  <tr key={l.symbol}>
                    <td><strong>{base(l.symbol)}</strong></td>
                    <td>{l.side && <span className={`pill ${l.side === 'long' ? 'up' : 'down'}`}>{SIDE_LABEL[l.side]}</span>}</td>
                    <td className="num">{fmt(l.entry)}</td>
                    <td className="num down">{fmt(l.stop_loss)}</td>
                    <td className="num up">{fmt(l.take_profit)}</td>
                    <td className="num">{l.notional === undefined ? '–' : l.fits
                      ? <>{usd(l.notional)} <small>{l.leverage}x</small></>
                      : <small>di bawah min {l.min_notional} USDT, lewati</small>}</td>
                    <td className="num">{l.risk_usd === undefined ? '–' : usd(l.risk_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {waiting.length > 0 && (
        <p className="muted small waiting">
          Menunggu, syarat terpenuhi: {waiting.map((l) => `${base(l.symbol)} ${l.checks_ok}/${l.checks}`).join(', ')}
        </p>
      )}
    </>
  );
}

export function PortfolioPanel() {
  const [p, setP] = useState<Portfolio | null>(null);
  useEffect(() => { api.portfolio(60).then(setP, () => setP({ available: false })); }, []);
  if (!p || !p.available) return null;
  const st = p.settings;
  const isYears = `${st.first_year}–${new Date(st.is_end * 1000).getUTCFullYear() - 1}`;
  const oosFrom = new Date(st.is_end * 1000).getUTCFullYear();
  const skip = p.result.skipped ?? { slots: 0, min_order: 0, margin: 0 };

  return (
    <section className="panel portfolio wide-panel">
      <div className="panel-head">
        <h2>Engine portofolio<small>modal ${p.equity}, {p.recommended.length} dari {p.candidates} koin, maks {st.max_pos} posisi, risiko {st.risk_pct}% per trade</small></h2>
      </div>

      <LiveTable live={p.live} maxPos={st.max_pos} />

      <h3>Simulasi akun ${p.equity}, {month(p.result.curve[0][0])} sampai {month(p.data_until)}</h3>
      <RunStrip run={p.result} start={p.equity} />
      <EquityChart curve={p.result.curve} start={p.equity} cut={st.is_end} />

      <h3>Uji tanpa lihat: mulai lagi dari ${p.equity} pada Jan {oosFrom}</h3>
      <RunStrip run={p.oos} start={p.equity} />
      <p className="note">
        Setelan (risiko, jumlah posisi) dan cara memilih koin ditentukan hanya dari data {isYears}. Data sejak {oosFrom} tidak dipakai saat memilih, jadi angka di atas adalah perkiraan yang lebih jujur untuk ke depan.
      </p>

      <h3>Per tahun</h3>
      <div className="strip folds">
        {Object.entries(p.result.years).map(([y, v]) => (
          <div key={y}><span>{y}, {v.trades} trade</span><strong className={tone(v.pnl)}>{v.pnl > 0 ? '+' : '-'}{usd(Math.abs(v.pnl))}</strong><small className={tone(v.exp)}>{r(v.exp)} per trade</small></div>
        ))}
      </div>

      <h3>Cara memilih koin, dipilih ulang tiap 1 Januari dari data sebelumnya</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Cara</th><th className="num">{isYears}</th><th className="num">Sejak {oosFrom}</th><th className="num">Turun terdalam</th><th className="num">Trade</th><th className="num">Koin sekarang</th></tr></thead>
          <tbody>
            {p.methods.map((m) => (
              <tr key={m.name} className={m.name === st.picker ? 'active' : ''}>
                <td>{PICKER_LABEL[m.name] ?? m.name}{m.name === st.picker && <small> dipakai</small>}</td>
                <td className={`num ${tone(m.is.return_pct)}`}>{pct(m.is.return_pct)}</td>
                <td className={`num ${tone(m.oos.return_pct)}`}>{pct(m.oos.return_pct)}</td>
                <td className="num">-{m.all.max_dd_pct}%</td>
                <td className="num">{m.all.trades}</td>
                <td className="num">{m.coins_now}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <details className="coins">
        <summary>Semua {p.candidates} koin yang diuji</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Koin</th><th>Ditrade</th><th className="num">Trade</th><th className="num">Rata-rata</th><th className="num">Sejak {oosFrom}</th><th className="num">Order muat</th><th className="num">Min order</th></tr></thead>
            <tbody>
              {p.coins.map((c) => (
                <tr key={c.symbol}>
                  <td><strong>{base(c.symbol)}</strong></td>
                  <td>{c.trade ? <span className="pill up">Ya</span> : <span className="muted small">{c.reason ? REASON_LABEL[c.reason] : 'Tidak'}</span>}</td>
                  <td className="num">{c.trades}</td>
                  <td className={`num ${tone(c.exp)}`}>{r(c.exp)}</td>
                  <td className={`num ${tone(c.oos_exp)}`}>{r(c.oos_exp)}<small> {c.oos_trades}x</small></td>
                  <td className="num">{c.fit_pct}%</td>
                  <td className="num">{c.min_notional} USDT</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>

      <p className="note">
        Satu akun menjalankan sinyal v3 semua koin pilihan sesuai urutan waktu: maks {st.max_pos} posisi, satu posisi per koin, ukuran dibulatkan ke lot Binance, order di bawah minimal Binance dilewati, leverage di bawah 1/(2 × SL%) dan margin tidak boleh melebihi saldo. Sinyal terlewat selama simulasi: {skip.slots} karena slot penuh, {skip.min_order} karena di bawah minimal order, {skip.margin} karena margin kurang. Daftar koin dibuat hari ini, jadi koin yang sudah mati (LUNA, FTT) tidak ikut; hasil nyata kemungkinan lebih buruk. Slippage dan sinyal yang telat dipasang juga belum dihitung. Hasil masa lalu tidak menjamin hasil berikutnya.
      </p>
    </section>
  );
}
